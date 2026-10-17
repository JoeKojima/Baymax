"""
The speed/compute changes in realtime_gemini_10.py change nothing the robot
does. Each check compares against the previous code's result:
  1. mic: the cheaper downmix / loudness math gives the same recording, the
     same "someone is talking" times and the same [MIC LEVEL] lines;
  2. the 48 -> 16 kHz filter streamed block by block equals filtering the
     whole recording at once, passes speech (to 7 kHz) and blocks what would
     alias;
  3. mic blocks go out as exactly the text the SDK builds, and still not while
     Ember's reply is playing;
  4. the dashboard picture's boxes, blended over fewer rows, give the same
     pixels;
  5. when Gemini is cut off, Ember goes quiet before the transcript is posted;
  6. the memory embedder's threads stop spinning after each use, with the
     same embeddings (needs the ONNX model; skipped otherwise);
  7. the playback buffer plays exactly what the previous code played;
  8. the live-view stream follows new pictures, re-sends an unchanged one
     byte for byte, and a viewer arriving after a skipped (or the camera's
     last) frame still gets that frame's picture;
  9. the [LATENCY] / [TOOL] lines appear once per reply and never stale.
Also: with no device.toml the end-of-speech settings are the old values, and
a bad value there falls back to them instead of stopping the robot.
Prints PASS on success.
"""
import asyncio, contextlib, importlib, importlib.util, io, os, sys, tempfile, time
from unittest import mock
import numpy as np
import cv2
HERE = os.path.dirname(os.path.abspath(__file__))
BAYMAX = os.path.join(HERE, "..", "Baymax-main")
sys.path.insert(0, BAYMAX)
for name in ["mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision",
             "semantic_embedder"]:
    sys.modules[name] = mock.MagicMock(name=name)
sd = mock.MagicMock()
sd.query_devices.return_value = [{"name": "pipewire", "max_output_channels": 0, "max_input_channels": 2},
                                 {"name": "default", "max_output_channels": 2, "max_input_channels": 0}]
sys.modules["sounddevice"] = sd
os.environ.pop("BAYMAX_DEVICE_KEY", None)
tmp = tempfile.mkdtemp()
os.environ["EMBER_DEVICE_CONFIG"] = os.path.join(tmp, "no-device.toml")    # defaults, whatever this machine has
import ember_self, person_memory
ember_self.PROFILE_PATH = os.path.join(tmp, "p.json"); person_memory.PENDING_PATH = os.path.join(tmp, "c.jsonl")
with mock.patch("subprocess.run"), mock.patch("subprocess.check_output", return_value=b""):
    R = importlib.import_module("realtime_gemini_10")
from google import genai
from google.genai import types
from google.genai.live import AsyncSession
rng = np.random.default_rng(11)


def whole_signal_16k(x):
    """Reference: the same filter over the whole recording, every third sample."""
    m = np.arange(R._To16k.TAPS) - (R._To16k.TAPS - 1) / 2
    h = np.sinc(m / 3) * np.kaiser(R._To16k.TAPS, 7.9); h /= h.sum()
    y = np.convolve(x.astype(np.float64), h)[:x.size][::3]
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


async def main():
    # ── 0. Turn-taking settings: the old values by default; a typo falls back to them ──
    vad = R._BASE_CONFIG["realtime_input_config"]["automatic_activity_detection"]
    assert vad == {"disabled": False,
                   "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
                   "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
                   "prefix_padding_ms": 20, "silence_duration_ms": 100}, vad
    def settings(values):
        def get(key):
            if isinstance(values, Exception):
                raise values
            return values[key.split(".")[1]]
        with mock.patch.object(R.device_config, "get", get), contextlib.redirect_stderr(io.StringIO()):
            return R._turn_end_settings()
    HIGH, LOW = types.EndSensitivity.END_SENSITIVITY_HIGH, types.EndSensitivity.END_SENSITIVITY_LOW
    assert settings({"end_of_speech": "HIGH", "silence_ms": 300}) == (HIGH, 300)
    assert settings({"end_of_speech": "high", "silence_ms": "250"}) == (HIGH, 250)
    for bad in ({"end_of_speech": "fast", "silence_ms": 100}, {"end_of_speech": "high", "silence_ms": "300ms"},
                {"end_of_speech": "high", "silence_ms": True}, {"end_of_speech": "high", "silence_ms": 0},
                TypeError("string indices must be integers")):
        assert settings(bad) == (LOW, 100), bad
    print("0 end of speech: old values by default; bad device.toml values fall back to them")

    # ── 1. The mic callback, fed stereo blocks like the robot's mic ──
    captured = {}
    class FakeInput:
        def __init__(self, *a, callback=None, **k): captured["cb"] = callback
        def __enter__(self): return self
        def __exit__(self, *a): return False
    R.sd.InputStream = FakeInput
    listener = asyncio.create_task(R.listen_audio())
    await asyncio.sleep(0.05)
    blocks = [(rng.normal(0, s, (R.CHUNK_SIZE, 2))).clip(-32768, 32767).astype(np.int16)
              for s in [30, 300, 3000, 20000] * 30]
    blocks.append(np.full((R.CHUNK_SIZE, 2), [-32768, 32767], np.int16))
    out = io.StringIO()
    loud_expected = []
    with contextlib.redirect_stdout(out):
        for b in blocks:
            mono = np.mean(b, axis=1).astype(np.int16)                 # previous code
            rms = np.sqrt(np.mean(mono.astype(np.float32) ** 2))
            loud_expected.append(rms > R.INTERRUPT_RMS_THRESHOLD)
            R._last_loud_mic_ts = 0.0
            captured["cb"](b, R.CHUNK_SIZE, None, None)
            assert (R._last_loud_mic_ts > 0) == loud_expected[-1]
    await asyncio.sleep(0.05)
    recorded = [R._audio_record_queue.get_nowait() for _ in range(R._audio_record_queue.qsize())]
    assert recorded == [np.mean(b, axis=1).astype(np.int16).tobytes() for b in blocks], "recording changed"
    levels = [ln for ln in out.getvalue().splitlines() if ln.startswith("[MIC LEVEL]")]
    want = []
    for i, b in enumerate(blocks, 1):
        if i % 50 == 0:
            mono = np.mean(b, axis=1).astype(np.int16)
            rms = np.sqrt(np.mean(mono.astype(np.float32) ** 2))
            want.append(f"[MIC LEVEL] rms={rms:.0f}  peak={int(np.max(np.abs(mono)))}  (max=32767)")
    assert levels == want, (levels, want)
    sent = []
    while not R.audio_queue_mic.empty():
        sent.append(R.audio_queue_mic.get_nowait())
    assert all(m["mime_type"] == "audio/pcm;rate=16000" for m in sent)
    whole = np.concatenate([np.mean(b, axis=1).astype(np.int16) for b in blocks])
    assert b"".join(m["data"] for m in sent) == whole_signal_16k(whole).tobytes(), "16 kHz stream differs"
    listener.cancel()
    print("1 mic: same recording, loudness, [MIC LEVEL] lines; 16 kHz stream as expected")

    # ── 2. The 16 kHz filter: block by block == whole signal; passes speech, blocks aliases ──
    x = rng.integers(-20000, 20000, 48000 * 2, dtype=np.int16)
    for sizes in ([1024], [1000, 7, 1, 2, 2048, 333]):
        f, parts, i, k = R._To16k(), [], 0, 0
        while i < x.size:
            n = sizes[k % len(sizes)]; parts.append(f(x[i:i + n])); i += n; k += 1
        assert np.array_equal(np.concatenate(parts), whole_signal_16k(x)), sizes
    def gain_db(freq):
        t = np.arange(48000 * 2) / 48000
        tone = (10000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
        f = R._To16k(); y = np.concatenate([f(tone[i:i + 1024]) for i in range(0, tone.size, 1024)])
        y = y[4000:].astype(np.float64)                     # past the start-up
        return 20 * np.log10((np.sqrt(np.mean(y ** 2)) + 1e-9) / (10000 / np.sqrt(2)))
    passband = [gain_db(f) for f in (300, 1000, 3000, 6000, 7000)]
    assert all(abs(g) < 0.1 for g in passband), passband
    assert all(gain_db(f) < -70 for f in (9500, 11000, 15000, 20000))
    print("2 16 kHz filter: streamed == whole; flat to 7 kHz (%s dB); aliases < -70 dB"
          % ", ".join(f"{g:+.2f}" for g in passband))

    # ── 3. Mic blocks sent as the SDK's exact text; still held back while Ember speaks ──
    class WS:
        def __init__(self): self.sent = []
        async def send(self, m): self.sent.append(m)
    api = genai.Client(api_key="not-used", http_options={"api_version": "v1alpha"}).aio._api_client
    sdk_ws, fast_ws = WS(), WS()
    sdk, session = AsyncSession(api, sdk_ws), AsyncSession(api, fast_ws)
    assert await R._fast_audio_sender(session, "audio/pcm;rate=16000") is not None, \
        "mic fast path is off with this SDK (it falls back correctly, but the CPU saving is gone)"
    sender = asyncio.create_task(R.send_audio_realtime(session))
    await asyncio.sleep(0.05)
    datas = [rng.integers(0, 256, n, dtype=np.uint8).tobytes() for n in (682, 684, 0, 1, 2, 3) * 30]
    for d in datas:
        R.audio_queue_mic.put_nowait({"data": d, "mime_type": "audio/pcm;rate=16000", "ts": time.perf_counter()})
        await sdk.send_realtime_input(audio=types.Blob(data=d, mime_type="audio/pcm;rate=16000"))
    await asyncio.sleep(0.1)
    assert fast_ws.sent == sdk_ws.sent and len(fast_ws.sent) == len(datas), "frames differ from the SDK's"
    R._append_playback(b"\x01\x00" * 4800)                              # Ember is speaking
    R.audio_queue_mic.put_nowait({"data": b"xx", "mime_type": "audio/pcm;rate=16000", "ts": time.perf_counter()})
    await asyncio.sleep(0.1)
    assert len(fast_ws.sent) == len(datas), "mic sent while Ember's reply was playing"
    R._flush_playback()
    sender.cancel()
    class NotTheSdk:
        async def send_realtime_input(self, **kw): pass
    assert await R._fast_audio_sender(NotTheSdk(), "audio/pcm;rate=16000") is None   # → the SDK's own path
    print(f"3 {len(datas)} mic blocks sent exactly as the SDK would; none while Ember speaks")

    # ── 4. Dashboard picture: boxes blended over fewer rows, same pixels ──
    def old_hud_box(frame):
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (310, 115), R._DARK, -1)
        cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    def old_graph_box(frame):
        h, w = frame.shape[:2]; gw, gh = 200, 80; gx, gy = w - gw - 10, h - gh - 10
        overlay = frame.copy()
        cv2.rectangle(overlay, (gx, gy), (gx + gw, gy + gh), R._DARK, -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    result = dict(trunk_angle=None, angular_vel=None, hip_descent_vel=None)
    no_text = mock.patch.object(R, "_text", lambda *a, **k: None)
    no_lines = mock.patch.object(R.cv2, "line", lambda *a, **k: None)
    compared = 0
    with no_text, no_lines:
        for w, h in [(640, 480), (320, 240), (1280, 720), (641, 479), (640, 120), (640, 9), (640, 3), (640, 1)]:
            frame = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
            a, b = frame.copy(), frame.copy()
            old_hud_box(a); R._draw_hud(b, result, 15.0)
            assert np.array_equal(a, b), ("hud", w, h)
            a, b = frame.copy(), frame.copy()
            old_graph_box(a); R._draw_angle_graph(b, [10.0, 20.0, 30.0], 45)
            assert np.array_equal(a, b), ("graph", w, h)
            compared += 2
    print(f"4 dashboard boxes: {compared} pictures identical, including tiny frames")

    # ── 5. Cut off: quiet first, then the transcript is posted ──
    posted = []
    def notify(endpoint, data):
        posted.append((endpoint, data.get("text"), len(R._playback_buffer)))
    def msg(**kw): return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))
    audio = types.Part(inline_data=types.Blob(data=b"\x01\x00" * 2400, mime_type="audio/pcm;rate=24000"))
    class FakeSession:
        async def receive(self):
            for m in [msg(model_turn=types.Content(parts=[audio])),
                      msg(output_transcription=types.Transcription(text="Let me tell you about")),
                      msg(model_turn=types.Content(parts=[audio])),
                      msg(interrupted=True)]:
                await asyncio.sleep(0.02)
                yield m
            await asyncio.sleep(3600)
        async def send_client_content(self, **kw): pass
        async def send_tool_response(self, **kw): pass
    with mock.patch.object(R, "_notify_webapp", notify), contextlib.redirect_stdout(io.StringIO()):
        receiver = asyncio.create_task(R.receive_audio(FakeSession()))
        await asyncio.sleep(0.5)
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver
    cut = [p for p in posted if p[1] == "Let me tell you about"]
    assert cut and cut[0][2] == 0, f"Ember still had audio queued when the cut-off line was posted: {posted}"
    assert len(R._playback_buffer) == 0 and not R._gemini_speaking
    print("5 cut off: playback emptied before the transcript was posted")

    # ── 6. Memory embedder: threads stop spinning; same embeddings ──
    model_dir = os.path.join(BAYMAX, "all-MiniLM-L6-v2-onnx")
    if not (os.path.exists(os.path.join(model_dir, "model.onnx")) and importlib.util.find_spec("optimum")):
        print("6 skipped (no ONNX embedding model)")
    else:
        del sys.modules["semantic_embedder"]
        import semantic_embedder
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        with contextlib.redirect_stdout(io.StringIO()):
            emb = semantic_embedder.SemanticEmbedder(model_dir=model_dir, verbose=False, chroma_dir=None)
        options = emb.model.model.get_session_options()
        assert options.get_session_config_entry("session.force_spinning_stop") == "1"
        texts = ["did Margaret plant the tulips", "I have a dentist appointment on Friday"]
        new = emb.embed(texts)
        emb.model = ORTModelForFeatureExtraction.from_pretrained(model_dir)        # previous options
        assert np.array_equal(new, emb.embed(texts)), "embeddings changed"
        print("6 memory embedder: spinning stopped, embeddings bit-identical")

    # ── 7. Playback buffer: the speaker gets exactly what the previous code gave it ──
    old_buf, out_new, out_old = b"", [], []
    R._flush_playback()
    for step in range(3000):
        if rng.random() < 0.5:
            data = rng.integers(0, 256, int(rng.choice([0, 1, 2, 3, 2048, 4800, 9601])), dtype=np.uint8).tobytes()
            R._append_playback(data); old_buf += data
        else:
            frames = int(rng.choice([1, 7, 1023, 1024, 3000]))
            outdata = np.zeros((frames, R.OUTPUT_CHANNELS), np.int16)
            R._output_callback(outdata, frames, None, None); out_new.append(outdata.tobytes())
            chunk, old_buf = old_buf[:frames * 2], old_buf[frames * 2:]          # previous code
            out_old.append(chunk + b"\x00" * (frames * 2 - len(chunk)))
    assert out_new == out_old and bytes(R._playback_buffer) == old_buf
    R._flush_playback()
    print(f"7 playback: {len(out_new)} speaker blocks identical to the previous code")

    # ── 8. Live-view stream ──
    import socket, threading, urllib.request
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    threading.Thread(target=R._start_mjpeg_server, args=(port,), daemon=True).start()
    await asyncio.sleep(0.3)
    def picture(value):
        return np.full((480, 640, 3), value, np.uint8)
    def read_part(stream):
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            byte = stream.read(1)
            assert byte, "live-view stream closed"
            header += byte
        data = stream.read(int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0]))
        stream.read(2)
        return data
    def stream_parts(n, between=None):
        stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=10)
        parts = []
        for i in range(n):
            if between and i in between:
                between[i]()
            parts.append(read_part(stream))
        stream.close()
        return parts
    def show(value):
        with R._annotated_frame_lock:
            R._annotated_frame = picture(value)
    show(30)
    parts = await asyncio.to_thread(stream_parts, 8, {3: lambda: show(200)})
    means = [int(cv2.imdecode(np.frombuffer(p, np.uint8), cv2.IMREAD_COLOR).mean()) for p in parts]
    assert parts[0] == parts[1] == parts[2] and abs(means[0] - 30) <= 2, means
    assert abs(means[-1] - 200) <= 2, f"stream froze on the first picture: {means}"
    # A skipped frame (nobody watching; or the camera's last before it stopped)
    result = dict(fall_detected=False, fall_active=False, fall_pending=False,
                  trunk_angle=12.0, angular_vel=1.0, hip_descent_vel=0.1)
    inputs = (picture(90), None, result, (15.0, 14.0), (10.0, 12.0, 11.0))
    with R._annotated_frame_lock:
        R._annotated_frame, R._annotated_inputs = None, inputs
    parts = await asyncio.to_thread(stream_parts, 2)
    expected = cv2.imencode(".jpg", R._draw_annotated(*inputs), [int(cv2.IMWRITE_JPEG_QUALITY), 70])[1].tobytes()
    assert parts == [expected, expected], "a viewer didn't get the last frame's picture"
    await asyncio.sleep(0.3)
    with R._annotated_frame_lock:
        viewers, R._annotated_frame, R._annotated_inputs = R._mjpeg_viewers, None, None
    assert viewers == 0, viewers
    print("8 live view: follows new pictures, re-sends unchanged ones; a skipped/last frame is still shown")

    # ── 9. [LATENCY] / [TOOL] lines: once per reply, never stale ──
    def heard(text): return msg(input_transcription=types.Transcription(text=text))
    def spoken(): return msg(model_turn=types.Content(parts=[audio]))
    tool = types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
        types.FunctionCall(id="1", name="no_such_tool", args={})]))
    async def run_session(messages):
        out = io.StringIO()
        class Session(FakeSession):
            async def receive(self):
                for m in messages:
                    await asyncio.sleep(0.05)
                    yield m
                await asyncio.sleep(3600)
        with mock.patch.object(R, "_notify_webapp", lambda *a, **k: None), contextlib.redirect_stdout(out):
            task = asyncio.create_task(R.receive_audio(Session()))
            await asyncio.sleep(0.05 * len(messages) + 0.3)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        R._flush_playback()
        return [ln for ln in out.getvalue().splitlines() if ln.startswith(("[LATENCY]", "[TOOL] Gemini spoke"))]
    lines = await run_session([heard("what's the"), spoken(), heard(" weather"), spoken(),
                               msg(turn_complete=True), heard("thanks"), spoken(), msg(turn_complete=True)])
    assert len(lines) == 2 and all(ln.startswith("[LATENCY]") for ln in lines), lines
    lines = await run_session([heard("hello"), msg(turn_complete=True), spoken(), msg(turn_complete=True)])
    assert lines == [], lines                                       # words nobody answered
    lines = await run_session([tool, msg(interrupted=True), heard("never mind"), spoken()])
    assert len(lines) == 1 and lines[0].startswith("[LATENCY]"), lines      # not "after the tool call"
    lines = await run_session([tool, spoken()])
    assert len(lines) == 1 and lines[0].startswith("[TOOL] Gemini spoke again"), lines
    print("9 timing lines: once per reply, none for unanswered words or an abandoned tool call")
    print("PASS")

asyncio.run(main())
