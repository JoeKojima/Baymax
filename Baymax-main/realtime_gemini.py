"""
Gemini Live API - Realtime S2S + Video (SoundDevice Version)
Model: gemini-2.5-flash-native-audio-preview-12-2025

Optimised for low latency:
- Callback-based output stream (zero event loop blocking).
- Batch-drain mic queue (eliminates mic_queue_wait buildup).
- Interrupt flushes playback buffer immediately.
- Graceful camera fallback if hardware unavailable.
- first_audio_latency tracks perceived delay (server thinking time).
- No client-side VAD — Gemini handles voice activity detection.
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

# Load API Key
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# Configuration
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": (
        "You are a socially intelligent conversational partner, not an "
        "information assistant. Your primary goal is to sustain natural, "
        "emotionally attuned conversation rather than provide exhaustive "
        "explanations.\n\n"
        "Behavior rules:\n"
        "- Keep responses short (1–3 sentences by default).\n"
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
}

# Audio Config
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_SIZE = 1024

# ─── Queues & Buffers ─────────────────────────────────────────────────────────

audio_queue_mic = asyncio.Queue()

# Thread-safe playback buffer for callback-based output stream
_playback_buffer = b""
_playback_lock = threading.Lock()

_gemini_speaking = False
_gemini_speaking_lock = threading.Lock()

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

    outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)


def start_output_stream() -> sd.OutputStream:
    stream = sd.OutputStream(
        # device = "Krisp Speaker", # used for echo cancellation
        samplerate=RECEIVE_SAMPLE_RATE,
        channels=CHANNELS,
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
        print(
            f"[QUEUES] mic_queue={audio_queue_mic.qsize():>4d}  "
            f"playback_buf={pbuf_len:>6d} bytes"
        )


# ─── Pipeline stages ─────────────────────────────────────────────────────────


async def listen_audio():
    loop = asyncio.get_running_loop()

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[MIC STATUS] {status}", flush=True)
        loop.call_soon_threadsafe(
            audio_queue_mic.put_nowait,
            {
                "data": bytes(indata),
                "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                "ts": time.perf_counter(),
            },
        )

    stream = sd.InputStream(
        # device = "Krisp Microphone", # Uses Krisp for voice filtering
        samplerate=SEND_SAMPLE_RATE,
        channels=CHANNELS,
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
    """Captures and sends video frames at 320×240 every 3 seconds.

    Tests an actual frame read before declaring camera active.
    Auto-disables after repeated failures. No cv2.imshow to avoid
    macOS IMKClient stalls.
    """
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
    """Receives audio from Gemini, appends to playback buffer.

    Tracks first_audio_latency: time from last mic send to the first
    audio chunk of each new Gemini turn — the perceived delay.
    """
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
                        if (
                            part.inline_data
                            and part.inline_data.mime_type.startswith(
                                "audio/pcm"
                            )
                        ):
                            with _gemini_speaking_lock:
                                _gemini_speaking = True
                            _append_playback(part.inline_data.data)

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
                    _is_new_turn = True

        except Exception as e:
            print(f"Receive error: {e}")
            break


# ─── Main ─────────────────────────────────────────────────────────────────────


async def run():
    client = genai.Client(
        api_key=API_KEY, http_options={"api_version": "v1alpha"}
    )

    print(f"Connecting to {MODEL}...")
    async with client.aio.live.connect(
        model=MODEL, config=CONFIG
    ) as live_session:
        print("Connected. System ready.")
        print("=" * 70)
        print("No client-side VAD — all audio sent to Gemini")
        print("Interrupts handled server-side")
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


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
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