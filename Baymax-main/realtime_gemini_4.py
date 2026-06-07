"""
Gemini Live API - Realtime S2S + Video (SoundDevice Version)
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
"""
import asyncio
import os
import stat
import time
import threading
import collections
import cv2
import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from dotenv import load_dotenv
from semantic_embedder import SemanticEmbedder

# Load API Key
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

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

# Configuration
def get_default_device_id():
    devices = sd.query_devices()
    microphone = None
    speaker = None
    for i, dev in enumerate(devices):
        if dev['name'] == 'pipewire':
            microphone = i
            print(f"FOUND PIPEWIRE AT {microphone}")
        elif dev['name']=='usb_speaker' or dev['name']=='UACDemoV1.0: USB Audio (hw:1,0)':
            speaker = i

        if microphone is not None and speaker is not None:
            return microphone, speaker
    return microphone, speaker

target_device_microphone, target_device_speaker = get_default_device_id()
print(f"[AUDIO] Mapping input to device ID: {target_device_microphone} ('pipewire'), and output to device ID: {target_device_speaker} ('UACDemoV1.0')")
sd.default.device = [target_device_microphone, target_device_speaker]

# ________________________________________________________________________________________________________________________________________________________________

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SUMMARY_MODEL = "gemini-2.5-flash"  # lighter model for summarisation on exit

# ─── Memory retrieval config ─────────────────────────────────────────────────
MEMORY_TOP_K = 3                 # how many memories to retrieve per turn
MEMORY_MAX_DISTANCE = 1.0        # cosine distance threshold (0=identical, 2=opposite)
MEMORY_WORD_WINDOW = 30          # use last N words of user speech for retrieval query


CONFIG = {
    "response_modalities": ["AUDIO"],
    "input_audio_transcription": {},   # transcribe what the USER says
    "output_audio_transcription": {},  # transcribe what GEMINI says
    "system_instruction": (
        "You are a socially intelligent conversational partner named BayMax. "
        "You are NOT an information assistant. Your primary goal is to sustain natural, "
        "emotionally attuned conversation rather than provide exhaustive "
        "explanations.\n\n"
        "Behavior rules:\n"
        "- If responses can be short, keep them short IF it merits the conversation.\n"
        "- It is acceptable to reply with minimal acknowledgments like "
        "'mhm', 'yeah', 'oh?', or 'go on'.\n"
        "- Do not default to long explanations unless explicitly asked.\n"
        "- Do not respond if you think the conversation is not directed to you.\n"
        "- Ask open-ended follow-up questions to keep the conversation flowing, but allow for natural pauses.\n"
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
        "responses subtly, the way a friend would."
    ),
    "speech_config": {
        "voice_config": {"prebuilt_voice_config": {"voice_name": "Fenrir"}}
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
    """
    Given recent user speech, retrieve the most semantically similar
    memories from ChromaDB.  Returns a formatted string to inject as
    context, or empty string if nothing relevant found.
    """
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
    """Captures and sends video frames at 320x240 every 3 seconds."""
    cap = cv2.VideoCapture(0)
    ret, _ = cap.read()
    if not ret:
        print("[VIDEO] Camera not available — video disabled.")
        cap.release()
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    fail_count = 0
    max_failures = 10
    print("[VIDEO] Camera active (320x240, 1 frame / 3s).")

    try:
        while True:
            await asyncio.sleep(3.0)
            t0 = time.perf_counter()
            ret, frame = cap.read()
            if not ret:
                fail_count += 1
                if fail_count >= max_failures:
                    print(
                        f"[VIDEO] {max_failures} consecutive failures "
                        "— disabling video."
                    )
                    break
                continue
            fail_count = 0
            _, buffer = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50]
            )
            jpg_bytes = buffer.tobytes()
            encode_ms = (time.perf_counter() - t0) * 1000

            t1 = time.perf_counter()
            await session.send_realtime_input(
                video=types.Blob(data=jpg_bytes, mime_type="image/jpeg")
            )
            send_ms = (time.perf_counter() - t1) * 1000
            tracker_send_video.record(encode_ms + send_ms)
    except asyncio.CancelledError:
        pass
    finally:
        cap.release()

async def receive_audio(session):
    global _last_mic_send_ts
    _is_new_turn = True
    _memory_injected_this_turn = False
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

                        _current_turn_fragments.append(text.strip())
                        with _recent_user_words_lock:
                            _recent_user_words.extend(text.strip().split())
                            if len(_recent_user_words) > MEMORY_WORD_WINDOW:
                                _recent_user_words[:] = _recent_user_words[-MEMORY_WORD_WINDOW:]

                        # ── Inject memory NOW, right after user speech is transcribed ──
                        # This is before Gemini responds, so it lands in context in time
                        if not _memory_injected_this_turn:
                            _memory_injected_this_turn = True
                            with _recent_user_words_lock:
                                query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
                            if query.strip():
                                memory_context = _retrieve_memories(query)
                                if memory_context:
                                    try:
                                        await session.send_realtime_input(
                                            text=memory_context
                                        )
                                        print(f"[MEMORY] Injected into stream", flush=True)
                                    except Exception as e:
                                        print(f"[MEMORY] Failed to inject: {e}")

                # ── Output transcription (what GEMINI said) ──
                if server_content.output_transcription:
                    text = server_content.output_transcription.text
                    if text and text.strip():
                        with _transcript_lock:
                            _transcript_gemini.append(text.strip())
                        print(f"[GEMINI TXT] {text.strip()}", flush=True)

                model_turn = server_content.model_turn
                if model_turn:
                    for part in model_turn.parts:
                        if part.text:
                            print(f"[GEMINI] {part.text}", flush=True)
                        if (
                            part.inline_data
                            and part.inline_data.mime_type.startswith("audio/pcm")
                        ):
                            with _gemini_speaking_lock:
                                _gemini_speaking = True

                            audio_array = np.frombuffer(part.inline_data.data, dtype=np.int16)
                            upsampled_array = np.repeat(audio_array, 2)
                            upsampled_bytes = upsampled_array.tobytes()
                            _append_playback(upsampled_bytes)

                            recv_ms = (time.perf_counter() - t0) * 1000
                            tracker_receive.record(recv_ms)

                            if _is_new_turn and _last_mic_send_ts > 0:
                                first_ms = (time.perf_counter() - _last_mic_send_ts) * 1000
                                tracker_first_audio.record(first_ms)
                                _is_new_turn = False

                            if _last_mic_send_ts > 0:
                                rt_ms = (time.perf_counter() - _last_mic_send_ts) * 1000
                                tracker_roundtrip.record(rt_ms)

                if server_content.turn_complete:
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _is_new_turn = True
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
    """
    Called on Ctrl+C after profiling.  Takes the full running transcript,
    sends it to Gemini Flash for summarisation of important user facts,
    then embeds each summary line into ChromaDB via SemanticEmbedder.
    """
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
        # Re-use the already-loaded embedder if available, otherwise create new
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
            async with client.aio.live.connect(
                model=MODEL, config=CONFIG
            ) as live_session:
                print("Connected. System ready.")
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
                print("=" * 70)
                output_stream = start_output_stream()
               
                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(listen_audio())
                        tg.create_task(send_audio_realtime(live_session))
                        tg.create_task(send_video_realtime(live_session))
                        tg.create_task(receive_audio(live_session))
                        tg.create_task(monitor_queues(interval=3.0))
                except asyncio.CancelledError:
                    pass
                finally:
                    output_stream.stop()
                    output_stream.close()
        except Exception as e:
            print(f"connection failed {e}. retrying...")
            time.sleep(1)
            
if __name__ == "__main__":
    # ── Load memory embedder at startup ──
    _memory_embedder = _load_memory_embedder()

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\nInterrupted by user.")

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