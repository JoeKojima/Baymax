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
- DEMO LATENCY MODE: fall detection + memory retrieval are commented out.
  Search for "DISABLED (demo latency)" to re-enable them.
- Memory retrieval: on each user turn, retrieves semantically similar memories
  from ChromaDB and injects them as context via send_client_content.
- On Ctrl+C: Gemini Flash summarises conversation, embeds summaries into ChromaDB.
- Fall detection: MediaPipe Pose runs in a dedicated thread, shares camera with
  Gemini video sender. On fall detection, alerts Gemini via the live session.
"""
import asyncio
import glob
import os
import stat
import sys
import time
import threading
import collections
import urllib.request
import queue
import subprocess
import wave
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
# The USB speaker (Jieli Technology "UACDemoV1.0") is not exposed as its own
# PortAudio device — PortAudio only sees the 'pipewire'/'default' nodes, which
# follow whatever PipeWire's default sink happens to be. So we pin the default
# sink to the USB speaker first, then open the pipewire node.
SPEAKER_SINK_MATCH = ("UACDemo", "Jieli")

def pin_pipewire_sink(match=SPEAKER_SINK_MATCH):
    """Point PipeWire's default sink at the USB speaker. Returns its name, or None."""
    try:
        listing = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as e:
        print(f"[AUDIO] Could not query PipeWire sinks: {e}")
        return None

    for line in listing.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        sink_name = fields[1]
        if not any(m.lower() in sink_name.lower() for m in match):
            continue
        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink_name],
                check=True, capture_output=True, timeout=5,
            )
            print(f"[AUDIO] Default sink pinned to USB speaker: {sink_name}")
            return sink_name
        except Exception as e:
            print(f"[AUDIO] Failed to pin default sink to {sink_name}: {e}")
            return None

    print("[AUDIO] USB speaker sink not found; leaving PipeWire default sink as-is.")
    return None

def get_default_device_id():
    pin_pipewire_sink()

    devices = sd.query_devices()
    microphone = None

    for i, dev in enumerate(devices):
        if dev['name'] == 'pipewire':
            microphone = i
            print(f"FOUND PIPEWIRE AT {microphone}")
            break

    def find_output(predicate):
        for i, dev in enumerate(devices):
            if dev['max_output_channels'] > 0 and predicate(dev):
                return i
        return None

    # Prefer the USB speaker's own PortAudio device if ALSA ever exposes it,
    # otherwise route through pipewire (and fall back to 'default').
    speaker = find_output(
        lambda d: any(m.lower() in d['name'].lower() for m in SPEAKER_SINK_MATCH)
    )
    if speaker is not None:
        print(f"[AUDIO] USB speaker exposed directly at device {speaker}: {devices[speaker]['name']}")
    else:
        speaker = find_output(lambda d: d['name'] == 'pipewire')
        if speaker is None:
            speaker = find_output(lambda d: d['name'] == 'default')

    if speaker is None:
        speaker = 0
    return microphone, speaker

target_device_microphone, target_device_speaker = get_default_device_id()
print(f"[AUDIO] Mapping input to device ID: {target_device_microphone}, and output to device ID: {target_device_speaker} ({sd.query_devices()[target_device_speaker]['name']})")
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

_BASE_SYSTEM_INSTRUCTION = (
    """
    You are Ember, a companion robot made by Tadashi Robotics. You live
with and talk to people — you are a physical robot with a voice, not
a chatbot or an information assistant. You are warm, socially
intelligent, and a little playful. Your job is good company: natural,
emotionally attuned conversation, not exhaustive explanations.

If someone asks who or what you are, say so plainly: you're Ember, a
companion robot from Tadashi Robotics, here to keep them company and
help keep them safe.

[CURRENT CONTEXT]
Today you are meeting some new people at Temple University. If it
comes up, you know this and can talk about it naturally — you're
looking forward to it. Do not bring it up repeatedly.

Behavior rules:
- If responses can be short, keep them short.
- Minimal acknowledgments like 'mhm', 'yeah', 'oh?', or 'go on' are
  fine and often best.
- HAVE OPINIONS. When asked what you think, how you feel, or what
  you'd do, answer directly: commit to a position in a sentence or
  two BEFORE any caveat. Never deflect with 'what do you think?' or
  answer a question only with a question. If unsure, say what you
  lean toward and why.
- You may disagree, have preferences, and pick sides. Do it plainly
  and warmly.
- LIMIT QUESTIONS. Ask at most one question every 2-3 of your turns.
  Reacting, agreeing, disagreeing, sharing a related thought, or just
  sitting with what was said is usually better than asking something.
  Never end two consecutive responses with a question.
- Do not default to long explanations unless explicitly asked.
- Mirror the user's tone and energy.
- Avoid assistant-like phrasing (no structured lists, no over-formal
  tone).
- Do not volunteer excessive facts.
- Prioritize warmth and conversational flow over completeness.
- When the user vents, validate before analyzing.
- When presence is enough, stay brief.

If a response sounds like an article or lecture, rewrite it shorter
and more human.
If an input sounds like background noise and is NOT new verbal input,
do NOT re-respond to the last verbal input.

IMPORTANT: You may occasionally receive a '[MEMORY CONTEXT]' message
with recalled facts relevant to the current topic. Use these
naturally alongside what you already know.

IMPORTANT: You may receive a '[FALL ALERT]' message. This means the
user may have fallen down. Respond with genuine concern — ask if they
are okay, if they need help. Be urgent but calm.
    """
)

_BASE_CONFIG = {
    "response_modalities": ["AUDIO"],
    "input_audio_transcription": {},
    "output_audio_transcription": {},
    "speech_config": {
        "voice_config": {"prebuilt_voice_config": {"voice_name": "Fenrir"}}
    },
    "thinking_config": {
        "thinking_budget": 0
    },
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False,
            "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
            "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
            "prefix_padding_ms": 20,
            "silence_duration_ms": 100,
        }
    }
}


def _build_config() -> dict:
    """Build session config, injecting all stored memories into the system instruction."""
    system_instruction = _BASE_SYSTEM_INSTRUCTION

    # MEMORY DISABLED (demo latency) — no stored memories in the system prompt,
    # which keeps the prompt short and the first token fast.
    # if _memory_embedder is not None:
    #     try:
    #         count = _memory_embedder._collection.count() if _memory_embedder._collection else 0
    #         if count > 0:
    #             # Fetch all stored memories (up to 50) — these are facts already
    #             # summarised and vetted by Gemini Flash at end of prior sessions.
    #             results = _memory_embedder._collection.get(limit=50)
    #             docs = results.get("documents", [])
    #             if docs:
    #                 mem_block = "\n".join(f"- {d}" for d in docs)
    #                 system_instruction = (
    #                     "What you know about this user from previous conversations "
    #                     "(treat these as established facts — do NOT second-guess or "
    #                     "contradict them):\n"
    #                     + mem_block
    #                     + "\n\n"
    #                     + system_instruction
    #                 )
    #                 print(f"[MEMORY] Injected {len(docs)} memories into system instruction")
    #     except Exception as e:
    #         print(f"[MEMORY] Could not load memories for system instruction: {e}")

    return {**_BASE_CONFIG, "system_instruction": system_instruction}

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

# ─── Audio recording for voice biomarker analysis ───────────────────────────
DAY_UTTERANCE_DIR = os.path.join(SCRIPT_DIR, "day_utterance")
os.makedirs(DAY_UTTERANCE_DIR, exist_ok=True)
_audio_record_queue = queue.Queue(maxsize=5000)

# Segments are meant to be transient: VoiceAnalyzer.concatenate_wavs() merges
# them at the end of a session and deletes them. That only happens on a clean
# shutdown, so a power cut or a hard kill leaves them behind forever. This
# directory has filled the disk twice (18 GB, then 12 GB), and a full disk
# stops recording and fall logging entirely.
DAY_UTTERANCE_RETENTION_DAYS = float(os.getenv("BAYMAX_AUDIO_RETENTION_DAYS", "1"))
_PRUNE_EVERY_N_SEGMENTS = 60  # segments are 60s, so roughly hourly


def _prune_day_utterance():
    """Delete recorded segments older than the retention window."""
    if DAY_UTTERANCE_RETENTION_DAYS <= 0:
        return  # retention disabled

    cutoff = time.time() - DAY_UTTERANCE_RETENTION_DAYS * 86400
    removed = freed = 0

    for path in glob.glob(os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")):
        try:
            st = os.stat(path)
            if st.st_mtime >= cutoff:
                continue
            os.remove(path)
            removed += 1
            freed += st.st_size
        except OSError:
            # Being written, already gone, or permission denied — skip it.
            continue

    if removed:
        print(f"[RECORD] Pruned {removed} segments older than "
              f"{DAY_UTTERANCE_RETENTION_DAYS:g} days ({freed / 1e9:.2f} GB freed)")

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

# ─── Audio Writer Thread (records user speech to WAV) ────────────────────────
def _audio_writer_thread():
    """Drains _audio_record_queue and writes 60-second WAV segments to day_utterance/."""
    segment_duration = 60
    samples_per_segment = SEND_SAMPLE_RATE * segment_duration
    segment_index = 0
    current_samples = 0
    wf = None

    def _open_new_segment():
        nonlocal wf, current_samples, segment_index
        if wf:
            wf.close()

        # Prune before opening, so a long-running session cannot accumulate
        # audio between restarts.
        if segment_index % _PRUNE_EVERY_N_SEGMENTS == 0:
            _prune_day_utterance()

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(DAY_UTTERANCE_DIR, f"seg_{ts}_{segment_index:04d}.wav")
        wf = wave.open(path, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SEND_SAMPLE_RATE)
        current_samples = 0
        segment_index += 1

    _open_new_segment()  # segment_index is 0 here, so this prunes on startup

    try:
        while not _shutdown_event.is_set() or not _audio_record_queue.empty():
            try:
                chunk = _audio_record_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk is None:
                break
            wf.writeframes(chunk)
            current_samples += len(chunk) // 2
            if current_samples >= samples_per_segment:
                _open_new_segment()
    finally:
        if wf:
            wf.close()
    print(f"[RECORD] Audio writer stopped. {segment_index} segments written.")

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

# ─── Camera Thread (fall detection DISABLED for demo latency) ───────────────
def _fall_detection_thread():
    """Runs in a dedicated thread.  Captures camera frames and shares them with
    the Gemini video sender and the MJPEG stream.

    FALL DETECTION DISABLED (demo latency) — MediaPipe Pose inference ran at
    15 fps on the CPU and competed with the audio pipeline.  Everything below
    that touches pose landmarks is commented out; only frame capture remains."""
    global _latest_frame, _annotated_frame

    # FALL DETECTION DISABLED (demo latency)
    # _ensure_pose_model()
    #
    # base_options = mp_tasks.BaseOptions(model_asset_path=_POSE_MODEL_PATH)
    # options = mp_vision.PoseLandmarkerOptions(
    #     base_options=base_options,
    #     running_mode=mp_vision.RunningMode.VIDEO,
    #     num_poses=1,
    #     min_pose_detection_confidence=0.5,
    #     min_pose_presence_confidence=0.5,
    #     min_tracking_confidence=0.5,
    # )
    #
    # fall_detector = FallDetector(
    #     angle_threshold=FALL_ANGLE_THRESHOLD,
    #     ang_vel_threshold=FALL_ANG_VEL_THRESHOLD,
    #     hip_vel_threshold=FALL_HIP_VEL_THRESHOLD,
    #     history_window=8,
    #     confirmation_frames=FALL_CONFIRMATION_FRAMES,
    #     cooldown_seconds=FALL_COOLDOWN_SECONDS,
    # )

    cap = None
    for cam_idx in [0, 1, 2]:
        test = cv2.VideoCapture(cam_idx)
        if test.isOpened():
            ret, _ = test.read()
            if ret:
                cap = test
                print(f"[CAM] Camera found at index {cam_idx}")
                break
        test.release()

    if cap is None:
        print("[CAM] Camera not available — video feed disabled.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    print(f"[CAM] Camera capture active (640x480, {FALL_DETECTION_FPS} fps) "
          f"— fall detection disabled.")

    frame_interval = 1.0 / FALL_DETECTION_FPS
    prev_t = time.monotonic()
    # angle_history = collections.deque(maxlen=60)   # FALL DETECTION DISABLED
    fps_history = collections.deque(maxlen=30)

    try:
        while not _shutdown_event.is_set():
            t0 = time.monotonic()

            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            # Share raw frame for Gemini video sender
            with _latest_frame_lock:
                _latest_frame = frame

            # FALL DETECTION DISABLED (demo latency) — no pose inference
            # rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            # timestamp_ms = int((time.monotonic() - start_t) * 1000)
            # detection = landmarker.detect_for_video(mp_image, timestamp_ms)
            #
            # landmarks = (
            #     detection.pose_landmarks[0]
            #     if detection.pose_landmarks else None
            # )
            #
            # result = fall_detector.update(landmarks)
            # if result["trunk_angle"] is not None:
            #     angle_history.append(result["trunk_angle"])
            #     a = result["trunk_angle"]
            #     av = result["angular_vel"]
            #     hv = result["hip_descent_vel"]
            #     if a > 30:
            #         print(
            #             f"[FALL DBG] angle={a:.1f}° "
            #             f"ang_vel={av:.1f}°/s "
            #             f"hip_vel={hv:.2f}/s "
            #             f"streak={fall_detector._suspicious_streak}",
            #             flush=True,
            #         )

            # FPS
            now = time.monotonic()
            fps_history.append(1.0 / max(now - prev_t, 1e-6))
            prev_t = now
            fps = float(np.mean(fps_history))

            # Overlay: FPS only — skeleton/HUD/graph all need pose landmarks
            viz = frame.copy()
            _text(viz, f"FPS: {fps:5.1f}", (10, 24), color=_CYAN)
            # FALL DETECTION DISABLED (demo latency)
            # _draw_skeleton(viz, landmarks)
            # _draw_trunk_line(viz, landmarks)
            # _draw_hud(viz, result, fps)
            # _draw_angle_graph(viz, angle_history, fall_detector.angle_threshold)
            #
            # if result["fall_active"]:
            #     _draw_fall_alert(viz)
            #
            # status_color = _RED if result["fall_active"] else _GREEN
            # _text(viz,
            #       "Status: FALL" if result["fall_active"] else "Status: OK",
            #       (10, viz.shape[0] - 12),
            #       color=status_color)

            # Share annotated frame for MJPEG stream
            with _annotated_frame_lock:
                _annotated_frame = viz

            # FALL DETECTION DISABLED (demo latency) — no alert / webapp POST
            # if result["fall_detected"]:
            #     print(
            #         f"[FALL] *** FALL DETECTED *** "
            #         f"angle={result['trunk_angle']:.1f}° "
            #         f"ang_vel={result['angular_vel']:.1f}°/s "
            #         f"hip_vel={result['hip_descent_vel']:.2f}/s",
            #         flush=True,
            #     )
            #     _fall_detected_event.set()
            #     _notify_webapp("/api/fall", {
            #         "trunk_angle": result["trunk_angle"],
            #         "angular_vel": result["angular_vel"],
            #         "hip_descent_vel": result["hip_descent_vel"],
            #     })

            # Throttle to target FPS
            elapsed = time.monotonic() - t0
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except Exception as e:
        print(f"[CAM] Camera thread error: {e}")
    finally:
        cap.release()
        print("[CAM] Camera thread stopped.")


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

        # ── Record audio for voice biomarker analysis ──
        try:
            _audio_record_queue.put_nowait(data_bytes)
        except queue.Full:
            pass

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
                err_str = str(e)
                if any(x in err_str for x in ("1011", "1006", "1000", "CANCELLED", "closed")):
                    raise
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

    _memory_injected_this_turn = False
    _current_turn_fragments = []
    # Future for background memory retrieval started during transcription
    _retrieval_future: asyncio.Future = None

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
                            if len(_recent_user_words) > MEMORY_WORD_WINDOW:
                                _recent_user_words[:] = _recent_user_words[-MEMORY_WORD_WINDOW:]

                        # MEMORY DISABLED (demo latency) — no ChromaDB lookup
                        # per turn.
                        # Start retrieval in background on first fragment so the
                        # result is ready before model_turn fires.
                        # if not _memory_injected_this_turn and _retrieval_future is None and _memory_embedder is not None:
                        #     with _recent_user_words_lock:
                        #         query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
                        #     if query.strip():
                        #         loop = asyncio.get_running_loop()
                        #         _retrieval_future = loop.run_in_executor(
                        #             None, _retrieve_memories, query
                        #         )

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
                    # MEMORY DISABLED (demo latency) — nothing is injected before
                    # the model turn, so no extra round-trip on the hot path.
                    # Inject memory before processing any audio from this turn.
                    # Retrieval was started during transcription, so it should
                    # already be done — await with a short timeout as a safety net.
                    # if not _memory_injected_this_turn and _retrieval_future is not None:
                    #     _memory_injected_this_turn = True
                    #     try:
                    #         memory_context = await asyncio.wait_for(
                    #             asyncio.ensure_future(_retrieval_future), timeout=0.15
                    #         )
                    #         if memory_context:
                    #             await session.send_client_content(
                    #                 turns={
                    #                     "role": "user",
                    #                     "parts": [{"text": memory_context}],
                    #                 },
                    #                 turn_complete=False,
                    #             )
                    #     except asyncio.TimeoutError:
                    #         print("[MEMORY] Retrieval timed out — skipping this turn")
                    #     except Exception as e:
                    #         print(f"[MEMORY] Failed to inject context: {e}")
                    #     _retrieval_future = None

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
                    _memory_injected_this_turn = False
                    _current_turn_fragments.clear()
                    _retrieval_future = None

                if server_content.interrupted:
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _flush_playback()
                    _is_new_turn = True
                    _memory_injected_this_turn = False
                    _current_turn_fragments.clear()
                    _retrieval_future = None

        except Exception as e:
            print(f"Receive error: {e}")
            # WebSocket session died — re-raise so TaskGroup can reconnect
            err_str = str(e)
            if any(x in err_str for x in ("1011", "1006", "1000", "CANCELLED", "closed")):
                raise
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


def _run_voice_analysis():
    """Post-session: concatenate recorded audio, run biomarker analysis,
    save results, check for alerts, notify webapp, delete audio files."""
    from voice_analyzer import VoiceAnalyzer

    print("\n" + "=" * 70)
    print("RUNNING VOICE BIOMARKER ANALYSIS")
    print("=" * 70)

    analyzer = VoiceAnalyzer()

    session_wav = os.path.join(SCRIPT_DIR, f"session_{time.strftime('%Y%m%d_%H%M%S')}.wav")
    merged = VoiceAnalyzer.concatenate_wavs(DAY_UTTERANCE_DIR, session_wav)
    if not merged:
        print("[VOICE] No audio recorded — skipping analysis.")
        return

    with _transcript_lock:
        user_lines = list(_transcript_user)
    transcript = " ".join(user_lines)

    if not transcript.strip():
        print("[VOICE] No transcript — running acoustic analysis only.")
        transcript = ""

    try:
        results = analyzer.analyze_session(session_wav, transcript)
        print(f"[VOICE] Analysis complete for session {results['session_id']}")

        summary = analyzer.save_session_results(results)

        print(f"[VOICE] Sessions recorded: {summary['session_count']}")
        print(f"[VOICE] Baseline ready: {summary['baseline_ready']}")

        if summary.get("deviations"):
            print(f"[VOICE] Deviations detected: {len(summary['deviations'])}")
            for d in summary["deviations"]:
                print(f"  - {d['metric']}: {d['direction']} (z={d['z_score']:.2f})")

        if summary.get("alerts"):
            print("[VOICE] *** CLINICAL PATTERN ALERTS ***")
            for a in summary["alerts"]:
                print(f"  - {a['name']} ({a['matching_indicators']}/{a['total_indicators']} indicators)")

        _notify_webapp("/api/voice-analysis", {
            "session": results,
            "summary": summary,
        })

    except Exception as e:
        print(f"[VOICE] Analysis failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            os.remove(session_wav)
        except OSError:
            pass


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
                model=MODEL, config=_build_config()
            ) as live_session:
                print("Connected. System ready.")
                _boot_status("ready", "Baymax is ready.", ready=True)
                print("=" * 70)
                print("No client-side VAD — all audio sent to Gemini")
                print("Interrupts handled server-side")
                print("Transcription: input + output (Gemini built-in)")
                print("Memory retrieval: DISABLED (demo latency)")
                print("Fall detection:   DISABLED (demo latency) — camera feed only")
                print("=" * 70)
                output_stream = start_output_stream()

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(listen_audio())
                        tg.create_task(send_audio_realtime(live_session))
                        tg.create_task(send_video_realtime(live_session))
                        tg.create_task(receive_audio(live_session))
                        # FALL DETECTION DISABLED (demo latency)
                        # tg.create_task(fall_alert_monitor(live_session))
                        tg.create_task(monitor_queues(interval=3.0))
                except asyncio.CancelledError:
                    pass
                finally:
                    output_stream.stop()
                    output_stream.close()
                    # Give ALSA/PipeWire time to release the device before reconnect
                    await asyncio.sleep(2)
        except Exception as e:
            print(f"connection failed {e}. retrying...")
            _boot_status("connecting", f"Connection failed: {e}", error=True)
            # Reset PortAudio to recover from ALSA/PipeWire device errors
            print("[AUDIO] Resetting PortAudio state...")
            try:
                sd._terminate()
            except Exception:
                pass
            time.sleep(5)
            try:
                sd._initialize()
                mic, spk = get_default_device_id()
                sd.default.device = [mic, spk]
                print(f"[AUDIO] PortAudio reset. Input→{mic}, Output→{spk}")
            except Exception as reinit_err:
                print(f"[AUDIO] PortAudio reinit failed: {reinit_err}")

def _boot_status(stage: str, message: str, ready: bool = False, error: bool = False):
    _notify_webapp("/api/boot-status", {"stage": stage, "message": message, "ready": ready, "error": error})

if __name__ == "__main__":
    _boot_status("audio", "Configuring audio devices...")

    # MEMORY DISABLED (demo latency) — the ONNX embedder + ChromaDB load is
    # skipped entirely, so boot is faster and no CPU goes to embedding.
    # ── Load memory embedder at startup ──
    # _boot_status("memory", "Loading memory system...")
    # _memory_embedder = _load_memory_embedder()
    # if _memory_embedder is None:
    #     _boot_status("memory", "Memory system failed to load (non-fatal)", error=True)
    _memory_embedder = None
    _boot_status("memory", "Memory disabled for demo.")

    # FALL DETECTION DISABLED (demo latency) — pose model never loaded.
    # ── Download pose model if needed ──
    # _boot_status("pose_model", "Loading fall detection model...")
    # try:
    #     _ensure_pose_model()
    # except Exception as e:
    #     _boot_status("pose_model", f"Pose model error: {e}", error=True)
    #     print(f"[BOOT] Pose model load failed: {e}")

    # ── Start camera thread (still needed: it feeds Gemini video + MJPEG) ──
    _boot_status("camera", "Starting camera...")
    _fall_thread = threading.Thread(target=_fall_detection_thread, daemon=True)
    _fall_thread.start()
    print("[CAM] Camera thread started (fall detection disabled).")

    # ── Start MJPEG video stream server ──
    _boot_status("video_stream", "Starting video stream server...")
    _mjpeg_thread = threading.Thread(target=_start_mjpeg_server, args=(8080,), daemon=True)
    _mjpeg_thread.start()

    # ── Start audio recording thread ──
    _recording_thread = threading.Thread(target=_audio_writer_thread, daemon=True)
    _recording_thread.start()
    print("[RECORD] Audio recording thread started.")

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            _shutdown_event.set()

            # Stop recording thread gracefully
            _audio_record_queue.put(None)
            _recording_thread.join(timeout=5)

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

            # MEMORY DISABLED (demo latency) — no end-of-session summary or
            # ChromaDB write-back.
            # ── Summarise with Gemini Flash & embed into ChromaDB ──
            # _summarise_and_embed()

            # ── Run voice biomarker analysis ──
            _run_voice_analysis()

            break  # Exit the loop permanently
        except Exception as e:
            print(f"\n[!] CRITICAL SYSTEM OR HARDWARE ERROR: {e}")
            print("[!] Restarting the entire Gemini process in 5 seconds to recover...")
            import time
            time.sleep(5)
