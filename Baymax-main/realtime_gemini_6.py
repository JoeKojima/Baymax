"""
Gemini Live API - Realtime S2S + Video + Fall Detection
Optimised for low latency:
- Callback-based output stream (zero event loop blocking).
- Batch-drain mic queue (eliminates mic_queue_wait buildup).
- Interrupt flushes playback buffer immediately.
- Graceful camera fallback if hardware unavailable.
- first_audio_latency tracks perceived delay (server thinking time).
- No client-side VAD — Gemini handles voice activity detection.

Added features:
- Gemini built-in input/output audio transcription (no local ASR model needed).
- Memory retrieval: on each user turn, retrieves semantically similar memories
  from ChromaDB and injects them as context via send_client_content.
- On Ctrl+C: Gemini Flash summarises conversation, embeds summaries into ChromaDB.
- Fall detection: MediaPipe Pose runs in a dedicated thread, shares camera with
  Gemini video sender. On fall detection, alerts Gemini via the live session.
"""
import asyncio
import os
import stat
import sys
import time
import threading
import collections
import urllib.request
import requests as http_requests
import cv2
import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from dotenv import load_dotenv
from semantic_embedder import SemanticEmbedder

import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision

sys.path.insert(0, "/home/meowmax/fall_detection")
from detector import FallDetector

# Load API Key
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# ─── Web app endpoint ────────────────────────────────────────────────────────
WEBAPP_URL = "http://localhost:5000"

def _notify_webapp(endpoint: str, data: dict):
    """Fire-and-forget POST to the Baymax web app.  Runs in a thread."""
    def _post():
        try:
            http_requests.post(f"{WEBAPP_URL}{endpoint}", json=data, timeout=2)
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()

# ─── Resolve script directory for all relative paths ─────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DIR = os.path.join(SCRIPT_DIR, "chroma_store")
ONNX_MODEL_DIR = os.path.join(SCRIPT_DIR, "all-MiniLM-L6-v2-onnx")
TRANSCRIPT_DIR = SCRIPT_DIR

# Ensure chroma_store exists and is writable
os.makedirs(CHROMA_DIR, exist_ok=True)
try:
    os.chmod(CHROMA_DIR, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
except OSError:
    pass  # Best effort

# ─── Pose landmarker model ──────────────────────────────────────────────────
_POSE_MODEL_PATH = os.path.join(SCRIPT_DIR, "pose_landmarker_full.task")
_POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task"
)

def _ensure_pose_model():
    if not os.path.exists(_POSE_MODEL_PATH):
        print(f"[FALL] Downloading pose landmarker model → {_POSE_MODEL_PATH} (~12 MB) ...")
        urllib.request.urlretrieve(_POSE_MODEL_URL, _POSE_MODEL_PATH)
        print("[FALL] Model downloaded.")

# Configuration
def get_default_device_id():
    devices = sd.query_devices()
    microphone = None
    speaker = None
    for i, dev in enumerate(devices):
        if dev['name'] == 'pipewire':
            microphone = i
            print(f"FOUND PIPEWIRE AT {microphone}")
        if dev['name'] == 'default' and dev['max_output_channels'] > 0:
            speaker = i

    if speaker is None:
        speaker = 0
    return microphone, speaker

target_device_microphone, target_device_speaker = get_default_device_id()
print(f"[AUDIO] Mapping input to device ID: {target_device_microphone} ('pipewire'), and output to device ID: {target_device_speaker} ('default')")
sd.default.device = [target_device_microphone, target_device_speaker]

# ________________________________________________________________________________________________________________________________________________________________

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SUMMARY_MODEL = "gemini-2.5-flash"  # lighter model for summarisation on exit

# ─── Memory retrieval config ─────────────────────────────────────────────────
MEMORY_TOP_K = 3                 # how many memories to retrieve per turn
MEMORY_MAX_DISTANCE = 1.0        # cosine distance threshold (0=identical, 2=opposite)
MEMORY_WORD_WINDOW = 30          # use last N words of user speech for retrieval query

# ─── Interruption gate ───────────────────────────────────────────────────────
INTERRUPT_RMS_THRESHOLD = 1000

# ─── Fall detection config ───────────────────────────────────────────────────
FALL_DETECTION_FPS = 15          # pose inference rate (frames per second)
FALL_ANGLE_THRESHOLD = 45.0
FALL_ANG_VEL_THRESHOLD = 25.0
FALL_HIP_VEL_THRESHOLD = 0.12
FALL_CONFIRMATION_FRAMES = 2
FALL_COOLDOWN_SECONDS = 3.0

CONFIG = {
    "response_modalities": ["AUDIO"],
    "input_audio_transcription": {},   # transcribe what the USER says
    "output_audio_transcription": {},  # transcribe what GEMINI says
    "system_instruction": (
        "You are a socially intelligent conversational partner, not an "
        "information assistant. Your primary goal is to sustain natural, "
        "emotionally attuned conversation rather than provide exhaustive "
        "explanations.\n\n"
        "Behavior rules:\n"
        "- If responses canbe short, keep them short.\n"
        "- It is acceptable to reply with minimal acknowledgments like "
        "'mhm', 'yeah', 'oh?', or 'go on'.\n"
        "- Do not default to long explanations unless explicitly asked.\n"
        "- Ask open-ended follow-up questions frequently.\n"
        "- Mirror the user's tone and energy.\n"
        "- Avoid assistant-like phrasing (no structured lists, no "
        "over-formal tone).\n"
        "- Do not volunteer excessive facts.\n"
        "- Prioritize curiosity, warmth, and conversational flow over "
        "completeness.\n"
        "- When the user vents, validate before analyzing.\n"
        "- When presence is enough, stay brief.\n\n"
        "If a response sounds like an article or lecture, rewrite it "
        "shorter and more human.\n"
        "If you receive an input that sounds like background noise and is NOT "
        "new verbal input, do NOT respond again with your response to the "
        "last verbal input.\n\n"
        "IMPORTANT: You may occasionally receive a '[MEMORY CONTEXT]' message "
        "containing facts remembered from previous conversations with this "
        "user. Use these naturally — don't announce that you 'remember' "
        "unless it fits the conversation. Let the knowledge inform your "
        "responses subtly, the way a friend would.\n\n"
        "IMPORTANT: You may receive a '[FALL ALERT]' message. This means the "
        "user may have fallen down. Respond with genuine concern — ask if "
        "they are okay, if they need help. Be urgent but calm."
    ),
    "speech_config": {
        "voice_config": {"prebuilt_voice_config": {"voice_name": "Fenrir"}}
    },
    "thinking_config": {
        "thinking_budget": 0  # disable thinking entirely
    },
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False, # default
            "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
            "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
            "prefix_padding_ms": 20,
            "silence_duration_ms": 100,
        }
    }

}

# Audio Config
SEND_SAMPLE_RATE = 48000
RECEIVE_SAMPLE_RATE = 48000
INPUT_CHANNELS = 2  # Hardware demands 2 channels
OUTPUT_CHANNELS = 1 # Gemini returns mono
CHUNK_SIZE = 1024

# ─── Queues & Buffers ─────────────────────────────────────────────────────────
audio_queue_mic = asyncio.Queue()

# Thread-safe playback buffer for callback-based output stream
_playback_buffer = b""
_playback_lock = threading.Lock()

_gemini_speaking = False
_gemini_speaking_lock = threading.Lock()

# ─── Transcript Accumulation ──────────────────────────────────────────────────
_transcript_user = []      # list of user utterance strings
_transcript_gemini = []    # list of gemini utterance strings
_transcript_lock = threading.Lock()

# Rolling buffer of recent user words for memory retrieval queries
_recent_user_words = []
_recent_user_words_lock = threading.Lock()

# ─── Shared camera frame (fall detection thread → Gemini video sender) ──────
_latest_frame = None
_latest_frame_lock = threading.Lock()

# ─── Fall detection event (thread → async monitor) ──────────────────────────
_fall_detected_event = threading.Event()
_shutdown_event = threading.Event()

# ─── Memory Retrieval Embedder (loaded once at startup) ──────────────────────
_memory_embedder: SemanticEmbedder = None  # set in __main__

def _load_memory_embedder():
    """Load SemanticEmbedder for memory retrieval.  Returns None on failure."""
    try:
        embedder = SemanticEmbedder(
            model_dir=ONNX_MODEL_DIR,
            chroma_dir=CHROMA_DIR,
            collection_name="user_memories",
            verbose=True,
        )
        count = embedder._collection.count() if embedder._collection else 0
        print(f"[MEMORY] Embedder ready — {count} memories in store")
        return embedder
    except Exception as e:
        print(f"[MEMORY] WARNING — could not load embedder: {e}")
        print("[MEMORY] Memory retrieval will be disabled this session.")
        return None


def _retrieve_memories(query_text: str) -> str:
    if _memory_embedder is None:
        return ""
    if not query_text.strip():
        return ""

    try:
        results = _memory_embedder.search(
            query_text,
            n_results=MEMORY_TOP_K,
        )
    except Exception as e:
        print(f"[MEMORY] Search error: {e}")
        return ""

    if not results:
        return ""

    # Filter by distance threshold
    relevant = [r for r in results if r["distance"] <= MEMORY_MAX_DISTANCE]
    if not relevant:
        return ""

    # Build context string
    memory_lines = []
    for r in relevant:
        memory_lines.append(f"- {r['document']} (relevance: {1 - r['distance']:.2f})")

    context = (
        "[MEMORY CONTEXT] Here are things you remember about this user "
        "from previous conversations:\n"
        + "\n".join(memory_lines)
    )
    print(f"[MEMORY] Retrieved {len(relevant)} memories for context")
    for line in memory_lines:
        print(f"  {line}")
    return context


# ─── Latency Profiling ────────────────────────────────────────────────────────
PROFILE_WINDOW = 50

class LatencyTracker:
    def __init__(self, name: str, window: int = PROFILE_WINDOW):
        self.name = name
        self.samples = collections.deque(maxlen=window)
        self._count = 0
        self._report_every = window

    def record(self, duration_ms: float):
        self.samples.append(duration_ms)
        self._count += 1
        if self._count % self._report_every == 0:
            self._print_summary()

    def _print_summary(self):
        arr = np.array(self.samples)
        with _playback_lock:
            pbuf = len(_playback_buffer)
        print(
            f"[PROFILE] {self.name:.<30s} "
            f"n={len(arr):>4d}  "
            f"avg={arr.mean():7.1f} ms  "
            f"p50={np.percentile(arr, 50):7.1f} ms  "
            f"p95={np.percentile(arr, 95):7.1f} ms  "
            f"max={arr.max():7.1f} ms  "
            f"queue_mic={audio_queue_mic.qsize():>4d}  "
            f"pbuf={pbuf:>6d}"
        )

tracker_mic_queue = LatencyTracker("mic_queue_wait")
tracker_send_audio = LatencyTracker("send_audio_to_gemini")
tracker_send_video = LatencyTracker("send_video_to_gemini")
tracker_receive = LatencyTracker("receive_from_gemini")
tracker_roundtrip = LatencyTracker("roundtrip_estimate")
tracker_first_audio = LatencyTracker("first_audio_latency")
_last_mic_send_ts: float = 0.0

# ─── Playback buffer helpers ─────────────────────────────────────────────────
def _append_playback(data: bytes):
    global _playback_buffer
    with _playback_lock:
        _playback_buffer += data

def _flush_playback():
    global _playback_buffer
    with _playback_lock:
        _playback_buffer = b""

# ─── Callback-based output stream ────────────────────────────────────────────
def _output_callback(outdata, frames, time_info, status):
    global _playback_buffer
    n_bytes = frames * 2
    with _playback_lock:
        chunk = _playback_buffer[:n_bytes]
        _playback_buffer = _playback_buffer[n_bytes:]
    if len(chunk) < n_bytes:
        chunk += b"\x00" * (n_bytes - len(chunk))
    outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, OUTPUT_CHANNELS)

def start_output_stream() -> sd.OutputStream:
    stream = sd.OutputStream(
        samplerate=RECEIVE_SAMPLE_RATE,
        channels=OUTPUT_CHANNELS,
        dtype="int16",
        blocksize=1024,
        callback=_output_callback,
    )
    stream.start()
    return stream

# ─── Queue monitor ────────────────────────────────────────────────────────────
async def monitor_queues(interval: float = 3.0):
    while True:
        await asyncio.sleep(interval)
        with _playback_lock:
            pbuf_len = len(_playback_buffer)
        with _transcript_lock:
            user_count = len(_transcript_user)
            gemini_count = len(_transcript_gemini)
        print(
            f"[QUEUES] mic_queue={audio_queue_mic.qsize():>4d}  "
            f"playback_buf={pbuf_len:>6d} bytes  "
            f"transcripts: user={user_count} gemini={gemini_count}"
        )

# ─── Fall Detection Visualisation ────────────────────────────────────────────
_POSE_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    (11, 12), (11, 13), (13, 15),
    (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (27, 31), (29, 31),
    (24, 26), (26, 28), (28, 30), (28, 32), (30, 32),
]
_WHITE  = (255, 255, 255)
_GREEN  = (0, 220, 0)
_YELLOW = (0, 200, 255)
_RED    = (30,  30, 220)
_DARK   = (20,  20,  20)
_CYAN   = (255, 220, 0)

def _text(img, text, pos, scale=0.65, color=_WHITE, thickness=2):
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX,
                scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, thickness, cv2.LINE_AA)

def _draw_skeleton(frame, landmarks):
    if not landmarks:
        return
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
    for a, b in _POSE_CONNECTIONS:
        if a < len(pts) and b < len(pts):
            if landmarks[a].visibility > 0.4 and landmarks[b].visibility > 0.4:
                cv2.line(frame, pts[a], pts[b], _GREEN, 2, cv2.LINE_AA)
    for i, (x, y) in enumerate(pts):
        if landmarks[i].visibility > 0.4:
            cv2.circle(frame, (x, y), 3, _WHITE, -1, cv2.LINE_AA)

def _draw_trunk_line(frame, landmarks):
    if not landmarks:
        return
    h, w = frame.shape[:2]
    ls, rs = landmarks[11], landmarks[12]
    lh, rh = landmarks[23], landmarks[24]
    sm = (int((ls.x + rs.x) / 2 * w), int((ls.y + rs.y) / 2 * h))
    hm = (int((lh.x + rh.x) / 2 * w), int((lh.y + rh.y) / 2 * h))
    cv2.line(frame, hm, sm, _YELLOW, 3, cv2.LINE_AA)
    cv2.circle(frame, sm, 7, _YELLOW, -1, cv2.LINE_AA)
    cv2.circle(frame, hm, 7, _YELLOW, -1, cv2.LINE_AA)

def _draw_hud(frame, result, fps):
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (310, 115), _DARK, -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    angle = result["trunk_angle"]
    ang_v = result["angular_vel"]
    hip_v = result["hip_descent_vel"]
    _text(frame, f"FPS:          {fps:5.1f}",                                         (10, 24),  color=_CYAN)
    _text(frame, f"Trunk angle:  {angle:.1f} deg" if angle is not None else "Trunk angle:  --", (10, 50))
    _text(frame, f"Angular vel:  {ang_v:.1f} deg/s" if ang_v is not None else "Angular vel:  --", (10, 76))
    _text(frame, f"Hip descent:  {hip_v:.2f} /s" if hip_v is not None else "Hip descent:  --", (10, 102))

def _draw_fall_alert(frame):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), _RED, -1)
    cv2.addWeighted(overlay, 0.35, frame, 0.65, 0, frame)
    label = "FALL DETECTED"
    font, scale, thick = cv2.FONT_HERSHEY_DUPLEX, 2.0, 3
    (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
    x, y = (w - tw) // 2, (h + th) // 2
    cv2.putText(frame, label, (x + 4, y + 4), font, scale, (0, 0, 0), thick + 4, cv2.LINE_AA)
    cv2.putText(frame, label, (x, y),          font, scale, _WHITE,    thick,     cv2.LINE_AA)

def _draw_angle_graph(frame, angle_history, threshold):
    if len(angle_history) < 2:
        return
    h, w = frame.shape[:2]
    gw, gh = 200, 80
    gx, gy = w - gw - 10, h - gh - 10
    overlay = frame.copy()
    cv2.rectangle(overlay, (gx, gy), (gx + gw, gy + gh), _DARK, -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    ty = gy + gh - int(threshold / 90.0 * gh)
    cv2.line(frame, (gx, ty), (gx + gw, ty), _RED, 1)
    vals = list(angle_history)
    n = len(vals)
    for i in range(1, n):
        x0 = gx + int((i - 1) / (n - 1) * gw)
        x1 = gx + int(i       / (n - 1) * gw)
        y0 = gy + gh - int(min(vals[i - 1], 90) / 90.0 * gh)
        y1 = gy + gh - int(min(vals[i],     90) / 90.0 * gh)
        cv2.line(frame, (x0, y0), (x1, y1), _GREEN, 2, cv2.LINE_AA)
    _text(frame, "Angle (0-90)", (gx + 4, gy + 12), scale=0.40, color=_CYAN, thickness=1)

# ─── Annotated frame for MJPEG stream ───────────────────────────────────────
_annotated_frame = None
_annotated_frame_lock = threading.Lock()

def _start_mjpeg_server(port=8080):
    """Lightweight MJPEG server in a daemon thread — serves annotated fall detection frames."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while not _shutdown_event.is_set():
                        with _annotated_frame_lock:
                            frame = _annotated_frame
                        if frame is None:
                            time.sleep(0.05)
                            continue
                        _, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                        data = jpg.tobytes()
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        time.sleep(0.066)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_response(301)
                self.send_header("Location", "/stream")
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"[MJPEG] Video stream available at http://0.0.0.0:{port}/stream")
    server.serve_forever()

# ─── Fall Detection Thread ──────────────────────────────────────────────────
def _fall_detection_thread():
    """Runs in a dedicated thread.  Captures camera frames, runs MediaPipe Pose,
    draws visual overlays, shares frames, and sets _fall_detected_event on fall."""
    global _latest_frame, _annotated_frame

    _ensure_pose_model()

    base_options = mp_tasks.BaseOptions(model_asset_path=_POSE_MODEL_PATH)
    options = mp_vision.PoseLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    fall_detector = FallDetector(
        angle_threshold=FALL_ANGLE_THRESHOLD,
        ang_vel_threshold=FALL_ANG_VEL_THRESHOLD,
        hip_vel_threshold=FALL_HIP_VEL_THRESHOLD,
        history_window=8,
        confirmation_frames=FALL_CONFIRMATION_FRAMES,
        cooldown_seconds=FALL_COOLDOWN_SECONDS,
    )

    cap = None
    for cam_idx in [0, 1, 2]:
        test = cv2.VideoCapture(cam_idx)
        if test.isOpened():
            ret, _ = test.read()
            if ret:
                cap = test
                print(f"[FALL] Camera found at index {cam_idx}")
                break
        test.release()

    if cap is None:
        print("[FALL] Camera not available — fall detection disabled.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    print(f"[FALL] Fall detection active (640x480, {FALL_DETECTION_FPS} fps pose inference).")

    frame_interval = 1.0 / FALL_DETECTION_FPS
    start_t = time.monotonic()
    prev_t = start_t
    angle_history = collections.deque(maxlen=60)
    fps_history = collections.deque(maxlen=30)

    try:
        with mp_vision.PoseLandmarker.create_from_options(options) as landmarker:
            while not _shutdown_event.is_set():
                t0 = time.monotonic()

                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.1)
                    continue

                # Share raw frame for Gemini video sender
                with _latest_frame_lock:
                    _latest_frame = frame

                # Run pose detection
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = int((time.monotonic() - start_t) * 1000)
                detection = landmarker.detect_for_video(mp_image, timestamp_ms)

                landmarks = (
                    detection.pose_landmarks[0]
                    if detection.pose_landmarks else None
                )

                result = fall_detector.update(landmarks)
                if result["trunk_angle"] is not None:
                    angle_history.append(result["trunk_angle"])
                    a = result["trunk_angle"]
                    av = result["angular_vel"]
                    hv = result["hip_descent_vel"]
                    if a > 30:
                        print(
                            f"[FALL DBG] angle={a:.1f}° "
                            f"ang_vel={av:.1f}°/s "
                            f"hip_vel={hv:.2f}/s "
                            f"streak={fall_detector._suspicious_streak}",
                            flush=True,
                        )

                # FPS
                now = time.monotonic()
                fps_history.append(1.0 / max(now - prev_t, 1e-6))
                prev_t = now
                fps = float(np.mean(fps_history))

                # Draw overlays on a copy
                viz = frame.copy()
                _draw_skeleton(viz, landmarks)
                _draw_trunk_line(viz, landmarks)
                _draw_hud(viz, result, fps)
                _draw_angle_graph(viz, angle_history, fall_detector.angle_threshold)

                if result["fall_active"]:
                    _draw_fall_alert(viz)

                status_color = _RED if result["fall_active"] else _GREEN
                _text(viz,
                      "Status: FALL" if result["fall_active"] else "Status: OK",
                      (10, viz.shape[0] - 12),
                      color=status_color)

                # Share annotated frame for MJPEG stream
                with _annotated_frame_lock:
                    _annotated_frame = viz

                if result["fall_detected"]:
                    print(
                        f"[FALL] *** FALL DETECTED *** "
                        f"angle={result['trunk_angle']:.1f}° "
                        f"ang_vel={result['angular_vel']:.1f}°/s "
                        f"hip_vel={result['hip_descent_vel']:.2f}/s",
                        flush=True,
                    )
                    _fall_detected_event.set()
                    _notify_webapp("/api/fall", {
                        "trunk_angle": result["trunk_angle"],
                        "angular_vel": result["angular_vel"],
                        "hip_descent_vel": result["hip_descent_vel"],
                    })

                # Throttle to target FPS
                elapsed = time.monotonic() - t0
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
    except Exception as e:
        print(f"[FALL] Fall detection thread error: {e}")
    finally:
        cap.release()
        print("[FALL] Fall detection thread stopped.")


# ─── Pipeline stages ─────────────────────────────────────────────────────────
async def listen_audio():
    loop = asyncio.get_running_loop()

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[MIC STATUS] {status}", flush=True)

        # Downmix stereo to mono for Gemini if hardware requires 2 channels
        if INPUT_CHANNELS > 1:
            mono_data = np.mean(indata, axis=1).astype(np.int16)
            data_bytes = mono_data.tobytes()
        else:
            mono_data = indata.flatten().astype(np.int16)
            data_bytes = bytes(indata)

        # ── Volume check ──
        rms = np.sqrt(np.mean(mono_data.astype(np.float32) ** 2))
        peak = np.max(np.abs(mono_data))
        if not hasattr(audio_callback, '_count'):
            audio_callback._count = 0
        audio_callback._count += 1
        if audio_callback._count % 50 == 0:
            print(f"[MIC LEVEL] rms={rms:.0f}  peak={peak}  (max=32767)", flush=True)

        loop.call_soon_threadsafe(
            audio_queue_mic.put_nowait,
            {
                "data": data_bytes,
                "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                "ts": time.perf_counter(),
            },
        )

    stream = sd.InputStream(
        samplerate=SEND_SAMPLE_RATE,
        channels=INPUT_CHANNELS,
        dtype="int16",
        blocksize=CHUNK_SIZE,
        callback=audio_callback,
    )
    with stream:
        while True:
            await asyncio.sleep(1)

async def send_audio_realtime(session):
    """Sends all mic audio to Gemini with batch-drain. No VAD filtering."""
    global _last_mic_send_ts
    while True:
        msg = await audio_queue_mic.get()
        batch = [msg]
        while not audio_queue_mic.empty():
            try:
                batch.append(audio_queue_mic.get_nowait())
            except asyncio.QueueEmpty:
                break

        for msg in batch:
            queue_wait_ms = (
                time.perf_counter() - msg.get("ts", time.perf_counter())
            ) * 1000
            tracker_mic_queue.record(queue_wait_ms)

            with _playback_lock:
                buffer_empty = len(_playback_buffer) == 0
            if not buffer_empty:
                continue

            t1 = time.perf_counter()
            try:
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=msg["data"], mime_type=msg["mime_type"]
                    )
                )
            except Exception as e:
                print(f"Error sending audio: {e}")
                continue
            send_ms = (time.perf_counter() - t1) * 1000
            tracker_send_audio.record(send_ms)
            _last_mic_send_ts = time.perf_counter()

async def send_video_realtime(session):
    """Reads shared frames from the fall detection thread and sends to Gemini every 3s."""
    # Wait for fall detection thread to start capturing
    for _ in range(50):
        with _latest_frame_lock:
            if _latest_frame is not None:
                break
        await asyncio.sleep(0.1)

    with _latest_frame_lock:
        if _latest_frame is None:
            print("[VIDEO] No camera frames available — video disabled.")
            return

    print("[VIDEO] Sending shared camera frames to Gemini (1 frame / 3s).")

    while True:
        await asyncio.sleep(3.0)
        t0 = time.perf_counter()

        with _latest_frame_lock:
            frame = _latest_frame
        if frame is None:
            continue

        # Resize to 320x240 for Gemini (fall detection runs at 640x480)
        small = cv2.resize(frame, (320, 240))
        _, buffer = cv2.imencode(
            ".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 50]
        )
        jpg_bytes = buffer.tobytes()
        encode_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        try:
            await session.send_realtime_input(
                video=types.Blob(data=jpg_bytes, mime_type="image/jpeg")
            )
        except Exception as e:
            print(f"[VIDEO] Error sending frame: {e}")
            continue
        send_ms = (time.perf_counter() - t1) * 1000
        tracker_send_video.record(encode_ms + send_ms)

async def fall_alert_monitor(session):
    """Watches the fall detection event and injects an alert into the Gemini session."""
    loop = asyncio.get_running_loop()
    while True:
        triggered = await loop.run_in_executor(
            None, _fall_detected_event.wait, 2.0
        )
        if not triggered:
            continue
        _fall_detected_event.clear()

        print("[FALL] Injecting fall alert into Gemini session...", flush=True)
        try:
            await session.send_client_content(
                turns={
                    "role": "user",
                    "parts": [{
                        "text": (
                            "[FALL ALERT] The camera has detected that the user "
                            "may have just fallen down. Please immediately check "
                            "on them — ask if they are okay and if they need help."
                        )
                    }],
                },
                turn_complete=True,
            )
        except Exception as e:
            print(f"[FALL] Failed to inject alert: {e}")

async def receive_audio(session):
    """Receives audio from Gemini, upsamples 24kHz -> 48kHz, appends to buffer.
    Also captures input_transcription and output_transcription side-channel data.
    On each completed user turn, retrieves relevant memories and injects them."""
    global _last_mic_send_ts
    _is_new_turn = True

    # Track whether we already injected memory for the current user turn
    _memory_injected_this_turn = False
    # Accumulate input transcription fragments within a single user turn
    _current_turn_fragments = []

    while True:
        try:
            async for response in session.receive():
                t0 = time.perf_counter()
                server_content = response.server_content
                if server_content is None:
                    continue

                # ── Input transcription (what the USER said) ──
                if server_content.input_transcription:
                    text = server_content.input_transcription.text
                    if text and text.strip():
                        with _transcript_lock:
                            _transcript_user.append(text.strip())
                        print(f"[USER] {text.strip()}", flush=True)
                        _notify_webapp("/api/transcript", {"speaker": "user", "text": text.strip()})

                        # Accumulate words for memory retrieval
                        _current_turn_fragments.append(text.strip())
                        with _recent_user_words_lock:
                            _recent_user_words.extend(text.strip().split())
                            # Keep only last N words
                            if len(_recent_user_words) > MEMORY_WORD_WINDOW:
                                _recent_user_words[:] = _recent_user_words[-MEMORY_WORD_WINDOW:]

                # ── Output transcription (what GEMINI said) ──
                if server_content.output_transcription:
                    text = server_content.output_transcription.text
                    if text and text.strip():
                        with _transcript_lock:
                            _transcript_gemini.append(text.strip())
                        print(f"[GEMINI TXT] {text.strip()}", flush=True)
                        _notify_webapp("/api/transcript", {"speaker": "gemini", "text": text.strip()})

                model_turn = server_content.model_turn
                if model_turn:
                    # Model is starting to respond — if we haven't injected
                    # memory yet for this turn, do it now before audio arrives
                    if not _memory_injected_this_turn and _current_turn_fragments:
                        _memory_injected_this_turn = True
                        # Build query from recent user words
                        with _recent_user_words_lock:
                            query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
                        if query.strip():
                            memory_context = _retrieve_memories(query)
                            if memory_context:
                                try:
                                    await session.send_client_content(
                                        turns={
                                            "role": "user",
                                            "parts": [{"text": memory_context}],
                                        },
                                        turn_complete=False,
                                    )
                                except Exception as e:
                                    print(f"[MEMORY] Failed to inject context: {e}")

                    for part in model_turn.parts:
                        if part.text:
                            print(f"[GEMINI] {part.text}", flush=True)
                        if (
                            part.inline_data
                            and part.inline_data.mime_type.startswith(
                                "audio/pcm"
                            )
                        ):
                            with _gemini_speaking_lock:
                                _gemini_speaking = True

                            # --- AUDIO UPSAMPLING MAGIC (24kHz -> 48kHz) ---
                            audio_array = np.frombuffer(part.inline_data.data, dtype=np.int16)
                            upsampled_array = np.repeat(audio_array, 2)
                            upsampled_bytes = upsampled_array.tobytes()
                            _append_playback(upsampled_bytes)
                            # -----------------------------------------------

                            recv_ms = (time.perf_counter() - t0) * 1000
                            tracker_receive.record(recv_ms)

                            if _is_new_turn and _last_mic_send_ts > 0:
                                first_ms = (
                                    time.perf_counter() - _last_mic_send_ts
                                ) * 1000
                                tracker_first_audio.record(first_ms)
                                _is_new_turn = False

                            if _last_mic_send_ts > 0:
                                rt_ms = (
                                    time.perf_counter() - _last_mic_send_ts
                                ) * 1000
                                tracker_roundtrip.record(rt_ms)

                if server_content.turn_complete:
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _is_new_turn = True
                    # Reset per-turn state for next user turn
                    _memory_injected_this_turn = False
                    _current_turn_fragments.clear()

                if server_content.interrupted:
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _flush_playback()
                    _is_new_turn = True
                    _memory_injected_this_turn = False
                    _current_turn_fragments.clear()

        except Exception as e:
            print(f"Receive error: {e}")
            await asyncio.sleep(0.5)
            continue

# ─── Shutdown: Summarise & Embed ──────────────────────────────────────────────

def _summarise_and_embed():
    with _transcript_lock:
        user_lines = list(_transcript_user)
        gemini_lines = list(_transcript_gemini)

    if not user_lines and not gemini_lines:
        print("[SUMMARY] No transcript captured — skipping summarisation.")
        return

    # Build a conversation transcript with speaker labels
    conversation_parts = []
    ui, gi = 0, 0
    while ui < len(user_lines) or gi < len(gemini_lines):
        if ui < len(user_lines):
            conversation_parts.append(f"User: {user_lines[ui]}")
            ui += 1
        if gi < len(gemini_lines):
            conversation_parts.append(f"Gemini: {gemini_lines[gi]}")
            gi += 1
    full_transcript = "\n".join(conversation_parts)

    print("\n" + "=" * 70)
    print("GENERATING MEMORY SUMMARIES")
    print("=" * 70)
    print(f"[SUMMARY] Transcript: {len(user_lines)} user fragments, "
          f"{len(gemini_lines)} gemini fragments")

    # ── Call Gemini Flash (non-streaming, sync) ──
    try:
        client = genai.Client(
            api_key=API_KEY, http_options={"api_version": "v1alpha"}
        )

        prompt = (
            "You are a memory extraction system.  Below is a transcript of "
            "a voice conversation between a user and an AI assistant.  Your "
            "job is to extract concise yet thorough summary lines of "
            "*important information about the user* that would be worth "
            "remembering for future conversations.\n\n"
            "Focus on:\n"
            "- Personal facts (name, age, location, occupation, family)\n"
            "- Preferences and opinions\n"
            "- Goals, plans, and aspirations\n"
            "- Problems or concerns they mentioned\n"
            "- Emotional states and what triggered them\n"
            "- Specific requests or topics they care about\n"
            "- Relationships and people they mentioned\n\n"
            "Output ONLY the summary lines, one per line.  No numbering, no "
            "bullets, no preamble.  Each line should be a self-contained fact "
            "or observation.  If there is nothing meaningful to extract, "
            "output exactly: NOTHING_TO_REMEMBER\n\n"
            "--- TRANSCRIPT START ---\n"
            f"{full_transcript}\n"
            "--- TRANSCRIPT END ---"
        )

        response = client.models.generate_content(
            model=SUMMARY_MODEL,
            contents=prompt,
        )
        summary_text = response.text.strip()
    except Exception as e:
        print(f"[SUMMARY] Gemini summarisation failed: {e}")
        return

    if not summary_text or summary_text == "NOTHING_TO_REMEMBER":
        print("[SUMMARY] Nothing worth remembering was found.")
        return

    summary_lines = [
        line.strip() for line in summary_text.splitlines() if line.strip()
    ]
    print(f"[SUMMARY] Extracted {len(summary_lines)} memory lines:")
    for i, line in enumerate(summary_lines):
        print(f"  {i+1}. {line}")

    # ── Embed into ChromaDB via SemanticEmbedder ──
    try:
        print("\n[EMBED] Saving to ChromaDB …")
        embedder = _memory_embedder
        if embedder is None:
            embedder = SemanticEmbedder(
                model_dir=ONNX_MODEL_DIR,
                chroma_dir=CHROMA_DIR,
                collection_name="user_memories",
            )

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        ids = [f"memory_{timestamp}_{i}" for i in range(len(summary_lines))]
        metadatas = [
            {
                "source": "conversation_summary",
                "timestamp": timestamp,
                "line_index": str(i),
            }
            for i in range(len(summary_lines))
        ]

        embedder.save(summary_lines, ids=ids, metadatas=metadatas)
        print(f"[EMBED] ✓ Saved {len(summary_lines)} memories to ChromaDB")
    except Exception as e:
        print(f"[EMBED] Embedding failed: {e}")

    # ── Also dump raw transcript to a file for reference ──
    try:
        transcript_file = os.path.join(
            TRANSCRIPT_DIR,
            f"transcript_{time.strftime('%Y%m%d_%H%M%S')}.txt",
        )
        with open(transcript_file, "w") as f:
            f.write(full_transcript)
        print(f"[TRANSCRIPT] Raw transcript saved to {transcript_file}")
    except Exception as e:
        print(f"[TRANSCRIPT] Could not save transcript file: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────
async def run():
    client = genai.Client(
        api_key=API_KEY, http_options={"api_version": "v1alpha"}
    )
    while True:
        try:
            print(f"Connecting to {MODEL}...")
            _boot_status("connecting", f"Connecting to Gemini model...")
            async with client.aio.live.connect(
                model=MODEL, config=CONFIG
            ) as live_session:
                print("Connected. System ready.")
                _boot_status("ready", "Baymax is ready.", ready=True)
                print("=" * 70)
                print("No client-side VAD — all audio sent to Gemini")
                print("Interrupts handled server-side")
                print("Transcription: input + output (Gemini built-in)")
                mem_count = (
                    _memory_embedder._collection.count()
                    if _memory_embedder and _memory_embedder._collection
                    else 0
                )
                print(f"Memory retrieval: {'ACTIVE' if _memory_embedder else 'DISABLED'}"
                      f" ({mem_count} memories)")
                print(f"Fall detection:   ACTIVE (thread)")
                print("=" * 70)
                output_stream = start_output_stream()

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(listen_audio())
                        tg.create_task(send_audio_realtime(live_session))
                        tg.create_task(send_video_realtime(live_session))
                        tg.create_task(receive_audio(live_session))
                        tg.create_task(fall_alert_monitor(live_session))
                        tg.create_task(monitor_queues(interval=3.0))
                except asyncio.CancelledError:
                    pass
                finally:
                    output_stream.stop()
                    output_stream.close()
        except Exception as e:
            print(f"connection failed {e}. retrying...")
            time.sleep(1)

def _boot_status(stage: str, message: str, ready: bool = False):
    _notify_webapp("/api/boot-status", {"stage": stage, "message": message, "ready": ready})

if __name__ == "__main__":
    _boot_status("audio", "Configuring audio devices...")

    # ── Load memory embedder at startup ──
    _boot_status("memory", "Loading memory system...")
    _memory_embedder = _load_memory_embedder()

    # ── Download pose model if needed ──
    _boot_status("pose_model", "Loading fall detection model...")
    _ensure_pose_model()

    # ── Start fall detection in a dedicated thread ──
    _boot_status("camera", "Starting camera & fall detection...")
    _fall_thread = threading.Thread(target=_fall_detection_thread, daemon=True)
    _fall_thread.start()
    print("[FALL] Fall detection thread started.")

    # ── Start MJPEG video stream server ──
    _boot_status("video_stream", "Starting video stream server...")
    _mjpeg_thread = threading.Thread(target=_start_mjpeg_server, args=(8080,), daemon=True)
    _mjpeg_thread.start()

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            _shutdown_event.set()

            # ── Print profiling summary ──
            print("\n" + "=" * 70)
            print("FINAL PROFILING SUMMARY")
            print("=" * 70)
            for t in [
                tracker_mic_queue,
                tracker_send_audio,
                tracker_send_video,
                tracker_receive,
                tracker_first_audio,
                tracker_roundtrip,
            ]:
                if t.samples:
                    t._print_summary()
                else:
                    print(f"[PROFILE] {t.name:.<30s} (no samples)")

            # ── Print full transcript ──
            with _transcript_lock:
                if _transcript_user or _transcript_gemini:
                    print("\n" + "=" * 70)
                    print("FULL CONVERSATION TRANSCRIPT")
                    print("=" * 70)
                    ui, gi = 0, 0
                    while ui < len(_transcript_user) or gi < len(_transcript_gemini):
                        if ui < len(_transcript_user):
                            print(f"  [USER]   {_transcript_user[ui]}")
                            ui += 1
                        if gi < len(_transcript_gemini):
                            print(f"  [GEMINI] {_transcript_gemini[gi]}")
                            gi += 1
                else:
                    print("\n[TRANSCRIPT] No speech was transcribed.")

            # ── Summarise with Gemini Flash & embed into ChromaDB ──
            _summarise_and_embed()

            break  # Exit the loop permanently
        except Exception as e:
            print(f"\n[!] CRITICAL SYSTEM OR HARDWARE ERROR: {e}")
            print("[!] Restarting the entire Gemini process in 5 seconds to recover...")
            import time
            time.sleep(5)
