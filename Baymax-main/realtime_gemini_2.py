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
- NVIDIA Parakeet running transcription of user speech.
- On Ctrl+C: Gemini Flash summarises conversation, embeds summaries into ChromaDB.
"""
import asyncio
import os
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

CONFIG = {
    "response_modalities": ["AUDIO"],
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
        "shorter and more human."
        "If you receive an input that sounds like background noise and is NOT new verbal input, do NOT respond again with your response to the last verbal input."
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
PARAKEET_SAMPLE_RATE = 16000  # Parakeet expects 16kHz mono float32

# ─── Queues & Buffers ─────────────────────────────────────────────────────────
audio_queue_mic = asyncio.Queue()

# Thread-safe playback buffer for callback-based output stream
_playback_buffer = b""
_playback_lock = threading.Lock()

_gemini_speaking = False
_gemini_speaking_lock = threading.Lock()

# ─── Parakeet Transcription State ─────────────────────────────────────────────
# Ring buffer that accumulates 16kHz mono float32 audio for Parakeet.
# A background thread periodically drains it and runs inference.
_parakeet_buffer = []           # list of np.float32 arrays (16kHz mono)
_parakeet_lock = threading.Lock()
_transcript_lines = []          # final running transcript (list of strings)
_transcript_lock = threading.Lock()
_parakeet_model = None          # loaded once at startup

PARAKEET_CHUNK_SECONDS = 30     # transcribe every N seconds of accumulated audio
PARAKEET_MODEL_NAME = "nvidia/parakeet-tdt-0.6b-v3"

def _load_parakeet():
    """Load the Parakeet ASR model once. Returns the model or None on failure."""
    global _parakeet_model
    try:
        import nemo.collections.asr as nemo_asr
        print(f"[PARAKEET] Loading {PARAKEET_MODEL_NAME} …")
        t0 = time.time()
        _parakeet_model = nemo_asr.models.ASRModel.from_pretrained(
            model_name=PARAKEET_MODEL_NAME
        )
        _parakeet_model.eval()
        print(f"[PARAKEET] Model loaded in {time.time()-t0:.1f}s")
        return _parakeet_model
    except Exception as e:
        print(f"[PARAKEET] WARNING — could not load model: {e}")
        print("[PARAKEET] Transcription will be disabled for this session.")
        return None


def _append_parakeet_audio(mono_int16: np.ndarray):
    """
    Accept a chunk of 48kHz int16 mono audio, downsample to 16kHz float32,
    and append to the Parakeet accumulation buffer.
    """
    # Convert int16 → float32 normalised to [-1, 1]
    audio_f32 = mono_int16.astype(np.float32) / 32768.0
    # Downsample 48kHz → 16kHz  (take every 3rd sample — simple decimation)
    downsampled = audio_f32[::3]
    with _parakeet_lock:
        _parakeet_buffer.append(downsampled)


def _parakeet_transcription_loop(stop_event: threading.Event):
    """
    Background thread: every PARAKEET_CHUNK_SECONDS seconds, drain the
    accumulation buffer and run Parakeet inference.  Results are appended
    to _transcript_lines.
    """
    import torch
    while not stop_event.is_set():
        stop_event.wait(timeout=PARAKEET_CHUNK_SECONDS)
        if _parakeet_model is None:
            continue
        # Drain buffer
        with _parakeet_lock:
            if not _parakeet_buffer:
                continue
            audio_concat = np.concatenate(_parakeet_buffer)
            _parakeet_buffer.clear()

        # Skip very short chunks (< 0.5s)
        if len(audio_concat) < PARAKEET_SAMPLE_RATE * 0.5:
            continue

        try:
            with torch.no_grad():
                hyps = _parakeet_model.transcribe(
                    [audio_concat], batch_size=1
                )
            text = hyps[0].text if hasattr(hyps[0], 'text') else str(hyps[0])
            text = text.strip()
            if text:
                with _transcript_lock:
                    _transcript_lines.append(text)
                print(f"[PARAKEET] {text}")
        except Exception as e:
            print(f"[PARAKEET] Transcription error: {e}")


def _parakeet_final_flush():
    """
    Drain whatever is left in the Parakeet buffer and run one last
    transcription pass.  Called from the shutdown path.
    """
    import torch
    with _parakeet_lock:
        if not _parakeet_buffer:
            return
        audio_concat = np.concatenate(_parakeet_buffer)
        _parakeet_buffer.clear()

    if _parakeet_model is None or len(audio_concat) < PARAKEET_SAMPLE_RATE * 0.3:
        return

    try:
        print("[PARAKEET] Final flush — transcribing remaining audio …")
        with torch.no_grad():
            hyps = _parakeet_model.transcribe([audio_concat], batch_size=1)
        text = hyps[0].text if hasattr(hyps[0], 'text') else str(hyps[0])
        text = text.strip()
        if text:
            with _transcript_lock:
                _transcript_lines.append(text)
            print(f"[PARAKEET] (final) {text}")
    except Exception as e:
        print(f"[PARAKEET] Final flush error: {e}")


# ─── Shutdown: Summarise & Embed ──────────────────────────────────────────────

def _summarise_and_embed():
    """
    Called on Ctrl+C after profiling.  Takes the full running transcript,
    sends it to Gemini Flash for summarisation of important user facts,
    then embeds each summary line into ChromaDB via SemanticEmbedder.
    """
    with _transcript_lock:
        full_transcript = "\n".join(_transcript_lines)

    if not full_transcript.strip():
        print("[SUMMARY] No transcript captured — skipping summarisation.")
        return

    print("\n" + "=" * 70)
    print("GENERATING MEMORY SUMMARIES")
    print("=" * 70)
    print(f"[SUMMARY] Transcript length: {len(full_transcript)} chars, "
          f"{len(_transcript_lines)} chunks")

    # ── Call Gemini Flash (non-streaming, sync) ──
    try:
        client = genai.Client(
            api_key=API_KEY, http_options={"api_version": "v1alpha"}
        )

        prompt = (
            "You are a memory extraction system.  Below is a transcript of "
            "everything a user said during a voice conversation.  Your job is "
            "to extract concise yet thorough summary lines of *important "
            "information about the user* that would be worth remembering for "
            "future conversations.\n\n"
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
        print("\n[EMBED] Initialising SemanticEmbedder …")
        embedder = SemanticEmbedder(
            chroma_dir="./chroma_store",
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
        transcript_file = f"transcript_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        with open(transcript_file, "w") as f:
            f.write(full_transcript)
        print(f"[TRANSCRIPT] Raw transcript saved to {transcript_file}")
    except Exception as e:
        print(f"[TRANSCRIPT] Could not save transcript file: {e}")


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
            transcript_chunks = len(_transcript_lines)
        print(
            f"[QUEUES] mic_queue={audio_queue_mic.qsize():>4d}  "
            f"playback_buf={pbuf_len:>6d} bytes  "
            f"transcript_chunks={transcript_chunks}"
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

        # ── Feed Parakeet buffer (runs in callback, keep fast) ──
        _append_parakeet_audio(mono_data)

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
    """Receives audio from Gemini, upsamples 24kHz -> 48kHz, and appends to buffer."""
    global _last_mic_send_ts
    _is_new_turn = True
    while True:
        try:
            async for response in session.receive():
                t0 = time.perf_counter()
                server_content = response.server_content
                if server_content is None:
                    continue

                model_turn = server_content.model_turn
                if model_turn:
                    for part in model_turn.parts:
                        if part.text:
                            print(f"[GEMINI] {part.text}", flush = True)
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
                if server_content.interrupted:
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _flush_playback()
                    _is_new_turn = True
        except Exception as e:
            print(f"Receive error: {e}")
            await asyncio.sleep(0.5)
            continue

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
                print(f"Parakeet transcription: {'ACTIVE' if _parakeet_model else 'DISABLED'}")
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
    # ── Load Parakeet model at startup ──
    _load_parakeet()

    # ── Start background transcription thread ──
    _parakeet_stop_event = threading.Event()
    _parakeet_thread = threading.Thread(
        target=_parakeet_transcription_loop,
        args=(_parakeet_stop_event,),
        daemon=True,
    )
    _parakeet_thread.start()

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\nInterrupted by user.")

            # ── Stop Parakeet background thread ──
            _parakeet_stop_event.set()
            _parakeet_thread.join(timeout=5)

            # ── Final Parakeet flush ──
            _parakeet_final_flush()

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
                if _transcript_lines:
                    print("\n" + "=" * 70)
                    print("FULL USER TRANSCRIPT")
                    print("=" * 70)
                    for line in _transcript_lines:
                        print(f"  {line}")
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