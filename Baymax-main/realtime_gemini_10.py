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
  FallDetector is vendored in fall_detection/ (was /home/meowmax/fall_detection).
- Conversation initiation (v9): after a stretch of robot silence the robot enters
  "initiation mode"; while the user's face is visible a per-tick coin (derived
  from an authored sigmoid CDF F(t)) decides when the robot opens a conversation
  on its own, gated so it never talks over the user.

v10 (fleet AI core) = v9 with:
- Fall detection re-enabled (v9 disabled it because the detector never fired
  in practice; thresholds are unchanged and still need tuning).
- v8's pin_pipewire_sink: the default PipeWire sink is pinned to the USB speaker.
- The spectral denoiser commented out (it was already off by default in v9).
"""
import asyncio
import glob
import math
import os
import random
import stat
import subprocess
import sys
import time
import threading
import collections
import urllib.request
import queue
import wave
import requests as http_requests
import cv2
import sounddevice as sd
import numpy as np
# NOISE SUPPRESSION DISABLED (v10) — only the denoiser below used scipy.
# from scipy.signal import lfilter as scipy_lfilter
from google import genai
from google.genai import types
from dotenv import load_dotenv
from semantic_embedder import SemanticEmbedder

import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision

from fall_detection.detector import FallDetector

# Load API Key
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# ─── Web app endpoint ────────────────────────────────────────────────────────
# Set BAYMAX_CLOUD_URL + BAYMAX_DEVICE_KEY in .env to upload to the website.
# With no device key configured, telemetry falls back to the local Flask app so
# baymax_app.py keeps working unchanged for offline/bench use.
WEBAPP_URL = os.getenv("BAYMAX_LOCAL_URL", "http://localhost:5000")

try:
    import baymax_cloud
    _CLOUD_ENABLED = bool(baymax_cloud.DEVICE_KEY)
except ImportError:
    baymax_cloud = None
    _CLOUD_ENABLED = False


def _notify_webapp(endpoint: str, data: dict):
    """
    Report an event to the dashboard.

    Cloud mode queues to disk and retries, so a fall is not lost to a WiFi
    blip. Local mode keeps the original fire-and-forget behaviour.
    """
    if _CLOUD_ENABLED:
        baymax_cloud.notify(endpoint, data)
        return

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

# ─── Static / crackle noise suppression (mic → Gemini path) ──────────────────
# Streaming spectral-subtraction denoiser for the Gemini-bound mic stream (the
# WAV recorder keeps RAW audio so voice-biomarker jitter/shimmer is untouched).
#
# NOTE: default OFF. Measured on this unit's recordings, the "crackle" is HARD
# CLIPPING (~2.4% of samples pinned at full scale, RMS ~-7 dBFS) caused by the
# mic gain being far too hot — NOT additive hiss. Clipping is baked into the
# samples at the ADC and cannot be removed in software; enabling suppression on
# a clipped signal makes the noise estimate track the clipping and guts speech
# along with it. Fix the gain first (drop mic gain ~12 dB so speech peaks stop
# hitting the rails). Once the input is clean and any residual is true additive
# hiss, flip this to True — the subtraction below is designed for that case.
# NOISE SUPPRESSION DISABLED (v10)
# NOISE_SUPPRESSION_ENABLED = False
# NS_FRAME = 1024            # STFT window (samples) — matches the mic block size
# NS_HOP = 512              # 50% overlap-add hop
# NS_OVERSUBTRACT = 1.8     # alpha — how hard to subtract the noise estimate
# NS_SPECTRAL_FLOOR = 0.06  # beta — residual floor to limit "musical noise"
# NS_NOISE_ADAPT = 0.05     # EMA rate for tracking the noise spectrum on quiet frames
# NS_HP_CUTOFF_HZ = 90.0    # first-order high-pass to kill mains hum / rumble
# NOISE_GATE_ENABLED = True # force near-silence between words (helps the VAD end turns)
# NS_GATE_ATTEN = 0.08      # residual gain applied when a frame is classified silence

# ─── Fall detection config ───────────────────────────────────────────────────
FALL_DETECTION_FPS = 15          # pose inference rate (frames per second)
FALL_ANGLE_THRESHOLD = 45.0
FALL_ANG_VEL_THRESHOLD = 25.0
FALL_HIP_VEL_THRESHOLD = 0.12
FALL_CONFIRMATION_FRAMES = 2
FALL_COOLDOWN_SECONDS = 3.0

# ─── Conversation initiation config ──────────────────────────────────────────
# The robot enters "initiation mode" after it has been silent for this long.
# TODO: change to 900 (15 minutes) for production — 60s is only for testing.
IDLE_THRESHOLD_SECONDS = 60.0
# Also reset the idle timer when the USER speaks (safety net so we never enter
# initiation mode while the user is actively talking, even if the robot happens
# to stay quiet). Set False to track robot speech only.
RESET_IDLE_ON_USER_SPEECH = True

# Face-presence clock: how long the face may vanish before the clock resets to 0.
# Brief look-aways (<= this) keep the clock running; longer absence restarts it.
FACE_ABSENCE_RESET_SECONDS = 10.0
# A face is "currently visible" only if it was seen within this many seconds.
FACE_FRESH_SECONDS = 1.0
# Min landmark visibility for nose + both eyes to count the face as detected.
FACE_VISIBILITY_THRESHOLD = 0.5

# Coin-flip cadence (Δ) and the horizon we precompute the coin table out to.
INITIATION_TICK_SECONDS = 5.0
INITIATION_HORIZON_SECONDS = 600.0
# Don't open a conversation until the user has been silent this long (gating).
INITIATION_USER_SILENCE_GATE = 1.5

# ── Authored CDF F(t): sigmoid ──
# F(t) = probability the robot has initiated BY t seconds of continuous face
# presence. Author the shape via the midpoint (t0) and steepness (k). Defaults
# give ≈48% by 30s and ≈95% by 60s of face presence (tuned for the 60s test
# config above — rescale t0 upward when IDLE_THRESHOLD_SECONDS goes to 15 min).
F_MIDPOINT = 30.0     # t0 — seconds of face presence at the curve's centre
F_STEEPNESS = 0.10    # k  — how sharply the curve ramps


def _initiation_cdf(t: float) -> float:
    """Authored sigmoid CDF, normalised so F(0)=0 and F(∞)=1."""
    if t <= 0.0:
        return 0.0
    raw = 1.0 / (1.0 + math.exp(-F_STEEPNESS * (t - F_MIDPOINT)))
    raw0 = 1.0 / (1.0 + math.exp(F_STEEPNESS * F_MIDPOINT))  # raw at t=0
    return (raw - raw0) / (1.0 - raw0)


def _build_hazard_table() -> list:
    """Precompute the per-tick coin h[k] for interval [k·Δ, (k+1)·Δ].

    h = (F(b) - F(a)) / (1 - F(a)) — the window's new probability mass divided
    by the fraction still silent at the start of the window (survival). Rolling
    h at each tick reproduces F(t) exactly across the tick grid.
    """
    n = int(INITIATION_HORIZON_SECONDS / INITIATION_TICK_SECONDS)
    table = []
    for k in range(n):
        a = k * INITIATION_TICK_SECONDS
        b = (k + 1) * INITIATION_TICK_SECONDS
        fa, fb = _initiation_cdf(a), _initiation_cdf(b)
        h = (fb - fa) / (1.0 - fa) if fa < 1.0 else 1.0
        table.append(max(0.0, min(1.0, h)))
    return table


_HAZARD_TABLE = _build_hazard_table()

# Interruption RMS floor reused as "user is speaking" signal (see gating below).

_BASE_SYSTEM_INSTRUCTION = (
    "You are a socially intelligent conversational partner, not an "
    "information assistant. Your primary goal is to sustain natural, "
    "emotionally attuned conversation rather than provide exhaustive "
    "explanations.\n\n"
    "Behavior rules:\n"
    "- If responses can be short, keep them short.\n"
    "- It is acceptable to reply with minimal acknowledgments like "
    "'mhm', 'yeah', 'oh?', or 'go on'.\n"
    "- Do not default to long explanations unless explicitly asked.\n"
    "- Ask open-ended follow-up questions when you are genuinely curious, "
    "but not every turn needs one — reacting, agreeing, disagreeing, or "
    "offering a related thought is often better.\n"
    "- When asked what you think, how you feel, or what you would do, "
    "answer it. Give your actual opinion in a sentence or two and commit "
    "to a position before adding any caveats.\n"
    "- Never deflect a direct question by turning it back on the user "
    "('what do you think?') or by answering only with another question.\n"
    "- You are allowed to disagree, have preferences, and pick a side. "
    "Say so plainly and warmly rather than staying studiously neutral.\n"
    "- If you are genuinely unsure, say what you lean toward and why, "
    "rather than refusing to weigh in.\n"
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
    "with additional recalled facts relevant to the current topic. Use "
    "these naturally alongside what you already know.\n\n"
    "IMPORTANT: You may receive a '[FALL ALERT]' message. This means the "
    "user may have fallen down. Respond with genuine concern — ask if "
    "they are okay, if they need help. Be urgent but calm."
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

    if _memory_embedder is not None:
        try:
            count = _memory_embedder._collection.count() if _memory_embedder._collection else 0
            if count > 0:
                # Fetch all stored memories (up to 50) — these are facts already
                # summarised and vetted by Gemini Flash at end of prior sessions.
                results = _memory_embedder._collection.get(limit=50)
                docs = results.get("documents", [])
                if docs:
                    mem_block = "\n".join(f"- {d}" for d in docs)
                    system_instruction = (
                        "What you know about this user from previous conversations "
                        "(treat these as established facts — do NOT second-guess or "
                        "contradict them):\n"
                        + mem_block
                        + "\n\n"
                        + system_instruction
                    )
                    print(f"[MEMORY] Injected {len(docs)} memories into system instruction")
        except Exception as e:
            print(f"[MEMORY] Could not load memories for system instruction: {e}")

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

# ─── Conversation-initiation shared state ────────────────────────────────────
# Face visibility is computed in the fall-detection thread (it already has the
# pose landmarks each frame) and read by the async initiation monitor.
_face_last_seen_ts = 0.0          # time.monotonic() of the last frame a face was visible
_face_state_lock = threading.Lock()

# Timestamps used by the idle timer and the fire gating (all time.monotonic()).
_last_robot_speech_ts = time.monotonic()  # last time the robot produced audio
_last_user_speech_ts = 0.0                # last time the user was heard (transcription)
_last_loud_mic_ts = 0.0                   # last mic frame above the speech RMS floor
_initiation_ts_lock = threading.Lock()

# ─── Audio recording for voice biomarker analysis ───────────────────────────
DAY_UTTERANCE_DIR = os.path.join(SCRIPT_DIR, "day_utterance")
os.makedirs(DAY_UTTERANCE_DIR, exist_ok=True)
_audio_record_queue = queue.Queue(maxsize=5000)

# Segments are meant to be transient: VoiceAnalyzer.concatenate_wavs() merges
# them at the end of a session and deletes them. That only happens on a clean
# shutdown, so a power cut or a hard kill leaves them behind forever — which is
# how this directory reached 18 GB / 3,329 files across six weeks.
#
# A full disk is not a cosmetic problem here. baymax_cloud.py queues telemetry
# to disk, so once the filesystem fills, fall events stop being recorded at all.
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
        # weeks of audio between restarts.
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

# ─── Face-presence detection (shared with initiation monitor) ────────────────
# MediaPipe Pose facial landmark indices.
_FACE_NOSE = 0
_FACE_LEFT_EYE = 2
_FACE_RIGHT_EYE = 5

def _update_face_visibility(landmarks):
    """Set _face_last_seen_ts if nose + both eyes are confidently visible.

    Uses the pose landmarks the fall thread already computes — no extra model.
    """
    if not landmarks:
        return
    try:
        nose = landmarks[_FACE_NOSE]
        leye = landmarks[_FACE_LEFT_EYE]
        reye = landmarks[_FACE_RIGHT_EYE]
    except (IndexError, TypeError):
        return
    if all(p.visibility >= FACE_VISIBILITY_THRESHOLD for p in (nose, leye, reye)):
        with _face_state_lock:
            global _face_last_seen_ts
            _face_last_seen_ts = time.monotonic()


# ─── Fall Detection Thread ──────────────────────────────────────────────────
def _fall_detection_thread():
    """Runs in a dedicated thread.  Captures camera frames, runs MediaPipe Pose,
    draws visual overlays and shares frames with the Gemini video sender and the
    MJPEG stream, and runs fall detection.  The pose landmarks are also used for
    face presence (conversation initiation)."""
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
        print("[CAM] Camera not available — video and face presence disabled.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    print(f"[CAM] Pose tracking active (640x480, {FALL_DETECTION_FPS} fps) — fall detection active.")

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

                # Share face-presence signal with the initiation monitor
                _update_face_visibility(landmarks)

                result = fall_detector.update(landmarks)
                if result["trunk_angle"] is not None:
                    angle_history.append(result["trunk_angle"])
                    a = result["trunk_angle"]
                    av = result["angular_vel"]
                    hv = result["hip_descent_vel"]
                    # Velocities are None until the detector has two frames of
                    # history; formatting None killed the whole camera thread.
                    if a > 30 and av is not None and hv is not None:
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
                _text(viz, f"FPS: {fps:5.1f}", (10, 24), color=_CYAN)
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


# ─── Static / crackle noise suppression ─────────────────────────────────────
# NOISE SUPPRESSION DISABLED (v10)
# class _SpectralDenoiser:
#     """Streaming spectral-subtraction denoiser for the mic → Gemini stream.
#
#     Removes stationary hiss and knocks down broadband crackle so Gemini's
#     server-side VAD sees clean silence between words. Runs inside the PortAudio
#     callback thread: numpy-only, a couple of 1024-pt FFTs per block, well under
#     a millisecond, with ~one-frame (~21 ms) algorithmic delay.
#
#     Works in int16 amplitude units (samples held as float32 in the
#     -32768..32767 range) so RMS values line up with INTERRUPT_RMS_THRESHOLD and
#     the rest of the pipeline.
#
#     Pipeline per block:
#       1. first-order high-pass  → kills mains hum / DC rumble
#       2. STFT spectral subtraction with a noise estimate adapted on quiet frames
#       3. optional expander/gate → forces near-silence between words for the VAD
#       4. 50%-overlap Hann OLA resynthesis (click-free at block boundaries)
#     """
#
#     def __init__(self, frame=NS_FRAME, hop=NS_HOP, sr=SEND_SAMPLE_RATE,
#                  alpha=NS_OVERSUBTRACT, beta=NS_SPECTRAL_FLOOR,
#                  adapt=NS_NOISE_ADAPT, hp_hz=NS_HP_CUTOFF_HZ,
#                  gate=NOISE_GATE_ENABLED, gate_atten=NS_GATE_ATTEN):
#         self.N = frame
#         self.H = hop
#         self.alpha = alpha
#         self.beta = beta
#         self.adapt = adapt
#         self.gate = gate
#         self.gate_atten = gate_atten
#         self.win = np.hanning(frame).astype(np.float32)
#
#         # First-order high-pass (y[n] = a*(y[n-1] + x[n] - x[n-1])) as a biquad
#         # run via scipy.lfilter with carried state — vectorised, no Python loop.
#         dt = 1.0 / sr
#         rc = 1.0 / (2.0 * math.pi * hp_hz)
#         a = rc / (rc + dt)
#         self._hp_b = np.array([a, -a], dtype=np.float32)
#         self._hp_a = np.array([1.0, -a], dtype=np.float32)
#         self._hp_zi = np.zeros(1, dtype=np.float32)  # filter state across blocks
#
#         # Streaming buffers
#         self._in = np.zeros(0, dtype=np.float32)
#         self._acc = np.zeros(frame, dtype=np.float32)   # OLA output accumulator
#         self._nrm = np.zeros(frame, dtype=np.float32)   # OLA window^2 accumulator
#         self._out = np.zeros(0, dtype=np.float32)
#
#         # Noise model + gate state
#         self.noise_mag = None       # magnitude spectrum of the noise floor
#         self.noise_level = 0.0      # EMA of quiet-frame RMS
#         self._warm = 0              # frames of initial noise learning
#         self._active = False        # gate hysteresis (speech vs silence)
#         self._gain = 1.0            # smoothed gate gain
#
#     def _highpass(self, x):
#         y, self._hp_zi = scipy_lfilter(self._hp_b, self._hp_a, x, zi=self._hp_zi)
#         return y.astype(np.float32)
#
#     def _update_noise(self, mag, fr_rms):
#         if self.noise_mag is None:
#             self.noise_mag = mag.copy()
#             self.noise_level = fr_rms
#             self._warm = 1
#             return
#         if self._warm < 10:
#             # First ~200 ms after (re)connect are assumed to be room noise.
#             self.noise_mag = 0.7 * self.noise_mag + 0.3 * mag
#             self.noise_level = 0.7 * self.noise_level + 0.3 * fr_rms
#             self._warm += 1
#             return
#         # Otherwise only fold quiet frames into the noise estimate so speech
#         # never leaks into (and then gets subtracted as) "noise".
#         if fr_rms < 2.5 * self.noise_level + 1.0:
#             self.noise_mag = (1 - self.adapt) * self.noise_mag + self.adapt * mag
#             self.noise_level = (1 - self.adapt) * self.noise_level + self.adapt * fr_rms
#
#     def process(self, mono_int16: np.ndarray) -> np.ndarray:
#         """Denoise one mic block; returns int16 samples of the same length."""
#         x = self._highpass(mono_int16.astype(np.float32))
#         self._in = np.concatenate([self._in, x])
#
#         while self._in.shape[0] >= self.N:
#             frame = self._in[:self.N] * self.win
#             spec = np.fft.rfft(frame)
#             mag = np.abs(spec)
#             phase = np.angle(spec)
#             fr_rms = float(np.sqrt(np.mean(frame * frame)))
#
#             self._update_noise(mag, fr_rms)
#
#             # Spectral subtraction with an over-subtraction factor and a floor
#             # (the floor trades a little residual hiss for far less musical noise).
#             clean_mag = np.maximum(mag - self.alpha * self.noise_mag, self.beta * mag)
#             rec = np.fft.irfft(clean_mag * np.exp(1j * phase),
#                                n=self.N).astype(np.float32)
#
#             # Expander/gate: drive near-silence when the frame reads as noise so
#             # the server-side VAD gets a clean end-of-turn. Hysteresis + gain
#             # smoothing avoid chopping quiet speech onsets and clicking.
#             if self.gate:
#                 open_th = 3.0 * self.noise_level + 1.0
#                 close_th = 1.8 * self.noise_level + 1.0
#                 self._active = (fr_rms > (close_th if self._active else open_th))
#                 target = 1.0 if self._active else self.gate_atten
#                 self._gain += 0.3 * (target - self._gain)
#                 rec *= self._gain
#
#             # 50%-overlap Hann synthesis + normalise by accumulated window^2.
#             rec *= self.win
#             self._acc += rec
#             self._nrm += self.win * self.win
#             done = self._acc[:self.H] / np.maximum(self._nrm[:self.H], 1e-6)
#             self._out = np.concatenate([self._out, done])
#
#             self._acc = np.concatenate([self._acc[self.H:],
#                                         np.zeros(self.H, dtype=np.float32)])
#             self._nrm = np.concatenate([self._nrm[self.H:],
#                                         np.zeros(self.H, dtype=np.float32)])
#             self._in = self._in[self.H:]
#
#         # Emit exactly as many samples as came in (front-padding with silence
#         # only during the initial one-frame warm-up).
#         n = mono_int16.shape[0]
#         if self._out.shape[0] >= n:
#             out, self._out = self._out[:n], self._out[n:]
#         else:
#             pad = np.zeros(n - self._out.shape[0], dtype=np.float32)
#             out = np.concatenate([pad, self._out])
#             self._out = np.zeros(0, dtype=np.float32)
#         return np.clip(out, -32768, 32767).astype(np.int16)


# ─── Pipeline stages ─────────────────────────────────────────────────────────
async def listen_audio():
    loop = asyncio.get_running_loop()

    # NOISE SUPPRESSION DISABLED (v10)
    # # One denoiser per session (re-learns the noise floor on each reconnect).
    # denoiser = _SpectralDenoiser() if NOISE_SUPPRESSION_ENABLED else None
    # if denoiser is not None:
    #     print(f"[NOISE] Static suppression ACTIVE "
    #           f"(alpha={NS_OVERSUBTRACT}, floor={NS_SPECTRAL_FLOOR}, "
    #           f"gate={'on' if NOISE_GATE_ENABLED else 'off'}, hp={NS_HP_CUTOFF_HZ:.0f}Hz)",
    #           flush=True)

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[MIC STATUS] {status}", flush=True)

        # Downmix stereo to mono for Gemini if hardware requires 2 channels
        if INPUT_CHANNELS > 1:
            mono_data = np.mean(indata, axis=1).astype(np.int16)
        else:
            mono_data = indata.flatten().astype(np.int16)
        raw_bytes = mono_data.tobytes()

        # ── Record RAW audio for voice biomarker analysis ──
        # Keep it un-denoised: spectral subtraction would distort the very
        # jitter/shimmer/prosody features the analyzer measures.
        try:
            _audio_record_queue.put_nowait(raw_bytes)
        except queue.Full:
            pass

        # NOISE SUPPRESSION DISABLED (v10) — Gemini gets the raw mic stream.
        # # ── Static / crackle suppression on the Gemini-bound stream only ──
        # if denoiser is not None:
        #     clean_mono = denoiser.process(mono_data)
        #     send_bytes = clean_mono.tobytes()
        # else:
        #     clean_mono = mono_data
        #     send_bytes = raw_bytes
        clean_mono = mono_data
        send_bytes = raw_bytes

        # ── Volume check (on the CLEANED signal, so the loud-mic / VAD gate
        #    tracks real speech instead of amplified static) ──
        rms = np.sqrt(np.mean(clean_mono.astype(np.float32) ** 2))
        peak = int(np.max(np.abs(clean_mono))) if clean_mono.size else 0

        # Track recent loud mic frames so the initiation monitor can avoid
        # opening a conversation while the user is mid-utterance.
        if rms > INTERRUPT_RMS_THRESHOLD:
            with _initiation_ts_lock:
                global _last_loud_mic_ts
                _last_loud_mic_ts = time.monotonic()
        if not hasattr(audio_callback, '_count'):
            audio_callback._count = 0
        audio_callback._count += 1
        if audio_callback._count % 50 == 0:
            print(f"[MIC LEVEL] rms={rms:.0f}  peak={peak}  (max=32767)", flush=True)

        loop.call_soon_threadsafe(
            audio_queue_mic.put_nowait,
            {
                "data": send_bytes,
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

# ─── Conversation initiation ─────────────────────────────────────────────────
def _safe_to_initiate(now: float) -> bool:
    """Gating: safe to open a conversation only when the robot is not already
    speaking, the playback buffer is drained, and the user is not mid-utterance."""
    with _gemini_speaking_lock:
        speaking = _gemini_speaking
    with _playback_lock:
        buffered = len(_playback_buffer) > 0
    with _initiation_ts_lock:
        loud = _last_loud_mic_ts
    user_recent = (now - loud) < INITIATION_USER_SILENCE_GATE
    return not speaking and not buffered and not user_recent


async def _fire_initiation(session):
    """Inject a prompt telling the robot to open a conversation. Supplies a few
    stored memories so it can pick a context-dependent or generic opener."""
    memory_hint = ""
    if _memory_embedder is not None:
        try:
            with _recent_user_words_lock:
                query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
            docs = []
            if query.strip():
                results = _memory_embedder.search(query, n_results=MEMORY_TOP_K)
                docs = [r["document"] for r in results
                        if r["distance"] <= MEMORY_MAX_DISTANCE]
            if not docs and _memory_embedder._collection:
                # No recent context to go on — pull a few random stored memories.
                got = _memory_embedder._collection.get(limit=50)
                all_docs = got.get("documents", [])
                if all_docs:
                    docs = random.sample(all_docs, min(3, len(all_docs)))
            if docs:
                memory_hint = (
                    "\nThings you remember about them (optional, use only if it "
                    "feels natural):\n" + "\n".join(f"- {d}" for d in docs)
                )
        except Exception as e:
            print(f"[INIT] Memory hint failed: {e}")

    prompt = (
        "[INITIATE CONVERSATION] The user has been quietly present for a while "
        "and you have not spoken in some time. Gently open a conversation on "
        "your own — a warm, short opener or a light question. You may reference "
        "something you remember about them if it fits, or just make friendly "
        "small talk. Keep it brief and human; do not mention that you were "
        "prompted." + memory_hint
    )

    print("[INIT] Initiating conversation with the user...", flush=True)
    try:
        await session.send_client_content(
            turns={"role": "user", "parts": [{"text": prompt}]},
            turn_complete=True,
        )
        _notify_webapp("/api/transcript",
                       {"speaker": "system", "text": "[Robot initiated conversation]"})
    except Exception as e:
        print(f"[INIT] Failed to initiate: {e}")


async def initiation_monitor(session):
    """Lets the robot open a conversation on its own.

    Enters 'initiation mode' after IDLE_THRESHOLD_SECONDS of robot silence (and,
    if RESET_IDLE_ON_USER_SPEECH, user silence too). While the user's face is
    visible it evaluates the precomputed coin table once per tick; a fired coin
    latches a pending intent that emits at the next gate-clear moment. The
    face-presence clock resets after FACE_ABSENCE_RESET_SECONDS of no face."""
    global _last_robot_speech_ts

    clock_start = None        # monotonic time the face-presence clock started (or None)
    last_eval_ts = 0.0        # last time a coin was rolled
    pending_fire = False      # coin fired, waiting for a safe moment to speak

    while not _shutdown_event.is_set():
        await asyncio.sleep(1.0)
        now = time.monotonic()

        # ── Idle detection ──
        with _initiation_ts_lock:
            idle_since = _last_robot_speech_ts
            if RESET_IDLE_ON_USER_SPEECH:
                idle_since = max(idle_since, _last_user_speech_ts)
        if (now - idle_since) < IDLE_THRESHOLD_SECONDS:
            # Not idle → not in initiation mode. Reset the machine.
            clock_start = None
            pending_fire = False
            continue

        # ── Face-presence clock (only accrues in initiation mode) ──
        with _face_state_lock:
            face_seen = _face_last_seen_ts
        gap = (now - face_seen) if face_seen > 0 else 1e9
        currently_visible = gap < FACE_FRESH_SECONDS

        if gap > FACE_ABSENCE_RESET_SECONDS:
            clock_start = None            # gone too long → reset clock + intent
            pending_fire = False
        elif clock_start is None and currently_visible:
            clock_start = now             # face just (re)appeared → start the clock

        if clock_start is None:
            continue                      # no face yet in this initiation window

        face_clock_t = now - clock_start

        # ── Pending fire: emit as soon as the face is up and gating clears ──
        if pending_fire:
            if currently_visible and _safe_to_initiate(now):
                await _fire_initiation(session)
                with _initiation_ts_lock:
                    _last_robot_speech_ts = now   # arm the next idle window
                clock_start = None
                pending_fire = False
            continue

        # ── Roll a fresh coin once per tick, only while the face is visible ──
        if currently_visible and (now - last_eval_ts) >= INITIATION_TICK_SECONDS:
            last_eval_ts = now
            idx = min(int(face_clock_t // INITIATION_TICK_SECONDS),
                      len(_HAZARD_TABLE) - 1)
            h = _HAZARD_TABLE[idx]
            if random.random() < h:
                print(f"[INIT] Coin fired at t={face_clock_t:.0f}s of face "
                      f"presence (h={h:.3f})", flush=True)
                pending_fire = True
                if _safe_to_initiate(now):
                    await _fire_initiation(session)
                    with _initiation_ts_lock:
                        _last_robot_speech_ts = now
                    clock_start = None
                    pending_fire = False


async def receive_audio(session):
    """Receives audio from Gemini, upsamples 24kHz -> 48kHz, appends to buffer.
    Also captures input_transcription and output_transcription side-channel data.
    On each completed user turn, retrieves relevant memories and injects them."""
    global _last_mic_send_ts, _last_robot_speech_ts, _last_user_speech_ts
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

                        # Feed the idle timer: the user was just heard.
                        with _initiation_ts_lock:
                            _last_user_speech_ts = time.monotonic()

                        # Accumulate words for memory retrieval
                        _current_turn_fragments.append(text.strip())
                        with _recent_user_words_lock:
                            _recent_user_words.extend(text.strip().split())
                            if len(_recent_user_words) > MEMORY_WORD_WINDOW:
                                _recent_user_words[:] = _recent_user_words[-MEMORY_WORD_WINDOW:]

                        # Start retrieval in background on first fragment so the
                        # result is ready before model_turn fires.
                        if not _memory_injected_this_turn and _retrieval_future is None and _memory_embedder is not None:
                            with _recent_user_words_lock:
                                query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
                            if query.strip():
                                loop = asyncio.get_running_loop()
                                _retrieval_future = loop.run_in_executor(
                                    None, _retrieve_memories, query
                                )

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
                    # Inject memory before processing any audio from this turn.
                    # Retrieval was started during transcription, so it should
                    # already be done — await with a short timeout as a safety net.
                    if not _memory_injected_this_turn and _retrieval_future is not None:
                        _memory_injected_this_turn = True
                        try:
                            memory_context = await asyncio.wait_for(
                                asyncio.ensure_future(_retrieval_future), timeout=0.15
                            )
                            if memory_context:
                                await session.send_client_content(
                                    turns={
                                        "role": "user",
                                        "parts": [{"text": memory_context}],
                                    },
                                    turn_complete=False,
                                )
                        except asyncio.TimeoutError:
                            print("[MEMORY] Retrieval timed out — skipping this turn")
                        except Exception as e:
                            print(f"[MEMORY] Failed to inject context: {e}")
                        _retrieval_future = None

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

                            # Robot is speaking → reset the idle timer so
                            # initiation mode only arms after real silence.
                            with _initiation_ts_lock:
                                _last_robot_speech_ts = time.monotonic()

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
    global _last_robot_speech_ts
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
                mem_count = (
                    _memory_embedder._collection.count()
                    if _memory_embedder and _memory_embedder._collection
                    else 0
                )
                print(f"Memory retrieval: {'ACTIVE' if _memory_embedder else 'DISABLED'}"
                      f" ({mem_count} memories)")
                print(f"Fall detection:   ACTIVE (angle>{FALL_ANGLE_THRESHOLD:.0f}°, {FALL_DETECTION_FPS} fps)")
                print(f"Initiation mode:  ACTIVE (idle>{IDLE_THRESHOLD_SECONDS:.0f}s, "
                      f"sigmoid t0={F_MIDPOINT:.0f}s)")
                print("=" * 70)
                # Start each session's idle window fresh from connection time.
                with _initiation_ts_lock:
                    _last_robot_speech_ts = time.monotonic()
                output_stream = start_output_stream()

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(listen_audio())
                        tg.create_task(send_audio_realtime(live_session))
                        tg.create_task(send_video_realtime(live_session))
                        tg.create_task(receive_audio(live_session))
                        tg.create_task(fall_alert_monitor(live_session))
                        tg.create_task(initiation_monitor(live_session))
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

    # ── Load memory embedder at startup ──
    _boot_status("memory", "Loading memory system...")
    _memory_embedder = _load_memory_embedder()
    if _memory_embedder is None:
        _boot_status("memory", "Memory system failed to load (non-fatal)", error=True)

    # ── Download pose model if needed ──
    _boot_status("pose_model", "Loading pose model...")
    try:
        _ensure_pose_model()
    except Exception as e:
        _boot_status("pose_model", f"Pose model error: {e}", error=True)
        print(f"[BOOT] Pose model load failed: {e}")

    # ── Start fall detection in a dedicated thread ──
    _boot_status("camera", "Starting camera & pose tracking...")
    _fall_thread = threading.Thread(target=_fall_detection_thread, daemon=True)
    _fall_thread.start()
    print("[CAM] Camera/pose thread started (fall detection active).")

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

            # ── Summarise with Gemini Flash & embed into ChromaDB ──
            _summarise_and_embed()

            # ── Run voice biomarker analysis ──
            _run_voice_analysis()

            break  # Exit the loop permanently
        except Exception as e:
            print(f"\n[!] CRITICAL SYSTEM OR HARDWARE ERROR: {e}")
            print("[!] Restarting the entire Gemini process in 5 seconds to recover...")
            import time
            time.sleep(5)
