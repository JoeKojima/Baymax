"""The Gemini Live runtime: one object instead of realtime_gemini_8.py's module globals.

Each method is the version-8 function of the same role, with the same order of
operations, log lines, monitor events and timing:

    output_callback      _output_callback            speaker callback (playback + cue + mixing)
    start_output_stream  start_output_stream
    listen_audio         listen_audio                mic callback -> cleanup -> queue
    send_audio_realtime  send_audio_realtime         batch-drain queue -> Gemini
    receive_audio        receive_audio               transcripts, audio, tools, turn ends
    monitor_queues       monitor_queues
    run                  run                         connect / reconnect / PortAudio reset
    main                 if __name__ == "__main__"   boot threads, shutdown summary

Nothing touches a device, the network or the disk until prepare()/main() runs,
so the runtime can be built in tests with virtual devices and a fake Gemini.
"""
import asyncio
import os
import threading
import time

import numpy as np

import runtime_monitor as monitor
from ember.devices.hardware import CHUNK_SIZE, OUTPUT_CHANNELS, RECEIVE_SAMPLE_RATE, SEND_SAMPLE_RATE
from ember.session import config as session_config
from ember.session.metrics import Trackers
from ember.session.recording import Recorder
from ember.session.video import Video

# ─── Interruption gate ───────────────────────────────────────────────────────
INTERRUPT_RMS_THRESHOLD = 1000
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Runtime:
    def __init__(self, settings, devices, client_factory=None, http=None, fall_worker=None, types=None,
                 script_dir=ROOT, build_config=None, sleep=time.sleep):
        if types is None:
            from google.genai import types
        if fall_worker is None:
            from features.toolkits.fall_detection import worker as fall_worker
        if http is None:
            import requests as http
        self.settings, self.devices, self.types, self.http = settings, devices, types, http
        self.client_factory = client_factory
        self.fall_worker = fall_worker
        self.script_dir = script_dir
        self.build_config = build_config
        self.sleep = sleep
        self.model = session_config.MODEL
        self.base_config = None

        self.audio_queue_mic = None  # asyncio.Queue, bound to the running loop in run()
        self.playback = b""
        self.playback_lock = threading.Lock()
        self.gemini_speaking = False
        self.gemini_speaking_lock = threading.Lock()
        self.transcript_user, self.transcript_gemini = [], []
        self.transcript_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.last_mic_send_ts = 0.0
        self.enhancer = self.waiting_cue = None
        self.trackers = Trackers(self._queues)
        self.recorder = Recorder(os.path.join(script_dir, "day_utterance"), SEND_SAMPLE_RATE,
                                 settings.audio_retention_days, self.shutdown_event)
        self.video = Video(devices, self.fall_worker, self.shutdown_event, self.trackers.send_video, types=types)

    # ── Startup that v8 did at import time ───────────────────────────────────
    def prepare(self):
        """Select devices, then build the audio cleanup and the waiting cue (v8 import order)."""
        self.devices.select()
        self.base_config = session_config.base_config()
        from features.conversational_ability.processing import create as create_audio_enhancer
        self.enhancer = create_audio_enhancer(SEND_SAMPLE_RATE)
        from features.conversational_ability.waiting import create as create_waiting_cue
        self.waiting_cue = create_waiting_cue(self.enhancer is not None and self.enhancer.full_duplex)
        monitor.waiting_audio = self.waiting_cue
        return self

    def session_config(self):
        if self.build_config is not None:
            return self.build_config()
        return session_config.build_config(self.base_config if self.base_config is not None else None)

    def _queues(self):
        with self.playback_lock:
            pbuf = len(self.playback)
        size = self.audio_queue_mic.qsize() if self.audio_queue_mic is not None else 0
        return size, pbuf

    # ── Dashboard notifications ──────────────────────────────────────────────
    def notify_webapp(self, endpoint, data):
        """Fire-and-forget POST to the Baymax web app.  Runs in a thread."""
        def _post():
            try:
                self.http.post(f"{self.settings.webapp_url}{endpoint}", json=data, timeout=2)
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    def boot_status(self, stage, message, ready=False, error=False):
        self.notify_webapp("/api/boot-status", {"stage": stage, "message": message, "ready": ready, "error": error})

    # ── Speaker ──────────────────────────────────────────────────────────────
    def append_playback(self, data):
        with self.playback_lock:
            self.playback += data

    def flush_playback(self):
        with self.playback_lock:
            self.playback = b""

    def output_callback(self, outdata, frames, time_info, status):
        n_bytes = frames * 2
        with self.playback_lock:
            chunk = self.playback[:n_bytes]
            self.playback = self.playback[n_bytes:]
        enhancer, cue = self.enhancer, self.waiting_cue
        if cue is not None:
            if enhancer is not None and enhancer.full_duplex:
                chunk = cue.render(chunk, n_bytes)
            else:
                cue.cancel()
        if len(chunk) < n_bytes:
            chunk += b"\x00" * (n_bytes - len(chunk))
        if enhancer is not None:
            chunk = enhancer.mix_output(chunk)
        outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, OUTPUT_CHANNELS)
        if enhancer is not None:
            enhancer.rendered(chunk, time_info)

    def start_output_stream(self):
        stream = self.devices.output_stream(self.output_callback)
        if self.enhancer is not None:
            self.enhancer.output_latency_ms = float(stream.latency) * 1000
        stream.start()
        return stream

    # ── Microphone ───────────────────────────────────────────────────────────
    def mic_callback(self, loop):
        channels = self.devices.input_channels
        count = [0]  # Per stream, like v8's audio_callback._count (restarts with each connection).

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"[MIC STATUS] {status}", flush=True)

            # Downmix stereo to mono for Gemini if hardware requires 2 channels
            if channels > 1:
                mono_data = np.mean(indata, axis=1).astype(np.int16)
                data_bytes = mono_data.tobytes()
            else:
                mono_data = indata.flatten().astype(np.int16)
                data_bytes = bytes(indata)

            # ── Record audio for voice biomarker analysis ──
            self.recorder.offer(data_bytes)

            if self.enhancer is not None:
                data_bytes = self.enhancer.process(mono_data, time_info).tobytes()

            # ── Volume check ──
            rms = np.sqrt(np.mean(mono_data.astype(np.float32) ** 2))
            peak = np.max(np.abs(mono_data))
            if self.waiting_cue is not None:
                self.waiting_cue.microphone(data_bytes, INTERRUPT_RMS_THRESHOLD)
            count[0] += 1
            if count[0] % 50 == 0:
                print(f"[MIC LEVEL] rms={rms:.0f}  peak={peak}  (max=32767)", flush=True)

            loop.call_soon_threadsafe(
                self.audio_queue_mic.put_nowait,
                {"data": data_bytes, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}", "ts": time.perf_counter()},
            )
        return audio_callback

    async def listen_audio(self):
        loop = asyncio.get_running_loop()
        if self.enhancer is not None:
            self.enhancer.reset()
        stream = self.devices.input_stream(self.mic_callback(loop))
        if self.enhancer is not None:
            self.enhancer.input_latency_ms = float(stream.latency) * 1000
        with stream:
            while True:
                await asyncio.sleep(1)

    async def send_audio_realtime(self, session):
        """Sends all mic audio to Gemini with batch-drain. No VAD filtering."""
        types, queue = self.types, self.audio_queue_mic
        while True:
            msg = await queue.get()
            batch = [msg]
            while not queue.empty():
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            for msg in batch:
                queue_wait_ms = (time.perf_counter() - msg.get("ts", time.perf_counter())) * 1000
                self.trackers.mic_queue.record(queue_wait_ms)
                enhancer = self.enhancer
                if enhancer is not None and enhancer.stale(queue_wait_ms):
                    continue

                with self.playback_lock:
                    buffer_empty = len(self.playback) == 0
                if not buffer_empty and not (enhancer is not None and enhancer.full_duplex):
                    continue

                t1 = time.perf_counter()
                try:
                    await session.send_realtime_input(audio=types.Blob(data=msg["data"], mime_type=msg["mime_type"]))
                except Exception as e:
                    print(f"Error sending audio: {e}")
                    err_str = str(e)
                    if any(x in err_str for x in ("1011", "1006", "1000", "CANCELLED", "closed")):
                        raise
                    continue
                send_ms = (time.perf_counter() - t1) * 1000
                self.trackers.send_audio.record(send_ms)
                self.last_mic_send_ts = time.perf_counter()

    # ── Gemini replies ───────────────────────────────────────────────────────
    async def receive_audio(self, session):
        """Receives audio from Gemini, upsamples 24kHz -> 48kHz, appends to buffer.
        Also captures input_transcription and output_transcription side-channel data."""
        is_new_turn = True
        trackers = self.trackers
        while True:
            try:
                async for response in session.receive():
                    t0 = time.perf_counter()
                    if response.session_resumption_update or response.go_away:
                        from features.conversational_ability.session import on_server_message
                        on_server_message(response)
                    if response.tool_call:
                        from features.toolkits.gemini_tools import handle_tool_call
                        await handle_tool_call(session, response.tool_call)
                    server_content = response.server_content
                    if server_content is None:
                        continue

                    # ── Input transcription (what the USER said) ──
                    if server_content.input_transcription:
                        text = server_content.input_transcription.text
                        monitor.transcribe("user", text)
                        if text and text.strip():
                            monitor.emit("thinking", "User speech received; waiting for Gemini")
                            with self.transcript_lock:
                                self.transcript_user.append(text.strip())
                            print(f"[USER] {text.strip()}", flush=True)
                            self.notify_webapp("/api/transcript", {"speaker": "user", "text": text.strip()})

                    # ── Output transcription (what GEMINI said) ──
                    if server_content.output_transcription:
                        text = server_content.output_transcription.text
                        monitor.transcribe("ember", text)
                        if text and text.strip():
                            with self.transcript_lock:
                                self.transcript_gemini.append(text.strip())
                            print(f"[GEMINI TXT] {text.strip()}", flush=True)
                            self.notify_webapp("/api/transcript", {"speaker": "gemini", "text": text.strip()})

                    model_turn = server_content.model_turn
                    if model_turn:
                        for part in model_turn.parts:
                            if part.text:
                                print(f"[GEMINI] {part.text}", flush=True)
                            if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                                monitor.emit("speaking", "Gemini is producing reply audio")
                                with self.gemini_speaking_lock:
                                    self.gemini_speaking = True

                                # --- AUDIO UPSAMPLING (24kHz -> 48kHz) ---
                                audio_array = np.frombuffer(part.inline_data.data, dtype=np.int16)
                                self.append_playback(np.repeat(audio_array, 2).tobytes())

                                trackers.receive.record((time.perf_counter() - t0) * 1000)

                                if is_new_turn and self.last_mic_send_ts > 0:
                                    trackers.first_audio.record((time.perf_counter() - self.last_mic_send_ts) * 1000)
                                    is_new_turn = False

                                if self.last_mic_send_ts > 0:
                                    trackers.roundtrip.record((time.perf_counter() - self.last_mic_send_ts) * 1000)

                    if server_content.turn_complete:
                        monitor.finish_transcripts()
                        monitor.emit("listening", "Reply complete; listening (buffered audio may still play)")
                        with self.gemini_speaking_lock:
                            self.gemini_speaking = False
                        is_new_turn = True

                    if server_content.interrupted:
                        monitor.finish_transcripts("ember")
                        monitor.emit("listening", "Reply interrupted; listening")
                        with self.gemini_speaking_lock:
                            self.gemini_speaking = False
                        self.flush_playback()
                        is_new_turn = True

            except Exception as e:
                print(f"Receive error: {e}")
                monitor.emit("reconnecting", "Gemini receive connection needs recovery")
                # WebSocket session died — re-raise so TaskGroup can reconnect
                err_str = str(e)
                if any(x in err_str for x in ("1011", "1006", "1000", "CANCELLED", "closed")):
                    raise
                await asyncio.sleep(0.5)
                continue

    async def monitor_queues(self, interval: float = 3.0):
        while True:
            await asyncio.sleep(interval)
            with self.playback_lock:
                pbuf_len = len(self.playback)
            with self.transcript_lock:
                user_count = len(self.transcript_user)
                gemini_count = len(self.transcript_gemini)
            print(
                f"[QUEUES] mic_queue={self.audio_queue_mic.qsize():>4d}  "
                f"playback_buf={pbuf_len:>6d} bytes  "
                f"transcripts: user={user_count} gemini={gemini_count}"
            )

    # ── Connection loop ──────────────────────────────────────────────────────
    def metrics(self):
        enhancer = self.enhancer
        return {"playback_bytes": len(self.playback), "gemini_generating_audio": self.gemini_speaking,
                "audio_input": dict(enhancer.metrics) if enhancer is not None else None}

    def client(self):
        if self.client_factory is not None:
            return self.client_factory()
        from google import genai
        return genai.Client(api_key=self.settings.api_key, http_options={"api_version": "v1alpha"})

    async def run(self, connections=None):
        """Connect, run the session tasks, reconnect on failure (forever unless `connections` is set)."""
        monitor.start(self.metrics)
        from features.toolkits.gemini_tools import prewarm
        prewarm()
        client = self.client()
        self.audio_queue_mic = asyncio.Queue()
        attempts = 0
        while connections is None or attempts < connections:
            attempts += 1
            try:
                monitor.finish_transcripts()
                monitor.emit("connecting", "Connecting to Gemini Live")
                print(f"Connecting to {self.model}...")
                self.boot_status("connecting", "Connecting to Gemini model...")
                async with client.aio.live.connect(model=self.model, config=self.session_config()) as live_session:
                    print("Connected. System ready.")
                    monitor.emit("listening", "Connected to Gemini; listening")
                    self.boot_status("ready", "Baymax is ready.", ready=True)
                    print("=" * 70)
                    print("No client-side VAD — all audio sent to Gemini")
                    print("Interrupts handled server-side")
                    print("Transcription: input + output (Gemini built-in)")
                    print("Memory retrieval: DISABLED (demo latency)")
                    print("Fall detection:   " + self.fall_worker.status()["state"])
                    print("=" * 70)
                    output_stream = self.start_output_stream()

                    try:
                        async with asyncio.TaskGroup() as tg:
                            tg.create_task(self.listen_audio())
                            tg.create_task(self.send_audio_realtime(live_session))
                            tg.create_task(self.video.send(live_session))
                            tg.create_task(self.receive_audio(live_session))
                            tg.create_task(self.fall_worker.alert_monitor(live_session, self.shutdown_event))
                            tg.create_task(self.monitor_queues(interval=3.0))
                    except asyncio.CancelledError:
                        pass
                    finally:
                        output_stream.stop()
                        output_stream.close()
                        # Give ALSA/PipeWire time to release the device before reconnect
                        await asyncio.sleep(2)
            except Exception as e:
                print(f"connection failed {e}. retrying...")
                self.boot_status("connecting", f"Connection failed: {e}", error=True)
                # Reset PortAudio to recover from ALSA/PipeWire device errors
                self.devices.reset(sleep=self.sleep)

    # ── Process lifetime ─────────────────────────────────────────────────────
    def start_threads(self):
        self.boot_status("audio", "Configuring audio devices...")
        self.boot_status("memory", "Memory disabled for demo.")

        # ── Start camera thread (it feeds Gemini video + MJPEG) ──
        self.boot_status("camera", "Starting camera...")
        threading.Thread(target=self.video.camera_thread, daemon=True).start()
        print("[CAM] Camera thread started (fall detection disabled).")

        # ── Start MJPEG video stream server ──
        self.boot_status("video_stream", "Starting video stream server...")
        threading.Thread(target=self.video.mjpeg_server, args=(self.settings.mjpeg_port,), daemon=True).start()

        # ── Start audio recording thread ──
        self.recording_thread = threading.Thread(target=self.recorder.run, daemon=True)
        self.recording_thread.start()
        print("[RECORD] Audio recording thread started.")

    def shutdown(self):
        monitor.emit("stopping", "Stopping conversation and finishing local session analysis")
        print("\nInterrupted by user.")
        self.shutdown_event.set()

        # Stop recording thread gracefully
        self.recorder.stop()
        self.recording_thread.join(timeout=5)

        # ── Print profiling summary ──
        print("\n" + "=" * 70)
        print("FINAL PROFILING SUMMARY")
        print("=" * 70)
        for t in self.trackers.final_order():
            if t.samples:
                t._print_summary()
            else:
                print(f"[PROFILE] {t.name:.<30s} (no samples)")

        # ── Print full transcript ──
        with self.transcript_lock:
            if self.transcript_user or self.transcript_gemini:
                print("\n" + "=" * 70)
                print("FULL CONVERSATION TRANSCRIPT")
                print("=" * 70)
                ui, gi = 0, 0
                while ui < len(self.transcript_user) or gi < len(self.transcript_gemini):
                    if ui < len(self.transcript_user):
                        print(f"  [USER]   {self.transcript_user[ui]}")
                        ui += 1
                    if gi < len(self.transcript_gemini):
                        print(f"  [GEMINI] {self.transcript_gemini[gi]}")
                        gi += 1
            else:
                print("\n[TRANSCRIPT] No speech was transcribed.")

        # ── Run voice biomarker analysis ──
        # Off for MVP: it processes the whole session and delays every restart.
        # Unanalysed segments are still pruned by BAYMAX_AUDIO_RETENTION_DAYS.
        if self.settings.voice_analysis:
            self.run_voice_analysis()
        else:
            print("[VOICE] Session analysis disabled (BAYMAX_VOICE_ANALYSIS=0).")

    def run_voice_analysis(self):
        """Post-session: concatenate recorded audio, run biomarker analysis,
        save results, check for alerts, notify webapp, delete audio files."""
        from voice_analyzer import VoiceAnalyzer

        print("\n" + "=" * 70)
        print("RUNNING VOICE BIOMARKER ANALYSIS")
        print("=" * 70)

        analyzer = VoiceAnalyzer()

        session_wav = os.path.join(self.script_dir, f"session_{time.strftime('%Y%m%d_%H%M%S')}.wav")
        merged = VoiceAnalyzer.concatenate_wavs(self.recorder.directory, session_wav)
        if not merged:
            print("[VOICE] No audio recorded — skipping analysis.")
            return

        with self.transcript_lock:
            user_lines = list(self.transcript_user)
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

            self.notify_webapp("/api/voice-analysis", {"session": results, "summary": summary})

        except Exception as e:
            print(f"[VOICE] Analysis failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            try:
                os.remove(session_wav)
            except OSError:
                pass

    def main(self):
        """Process entry point: v8's `if __name__ == "__main__"` block."""
        self.start_threads()
        while True:
            try:
                asyncio.run(self.run())
            except KeyboardInterrupt:
                self.shutdown()
                break  # Exit the loop permanently
            except Exception as e:
                print(f"\n[!] CRITICAL SYSTEM OR HARDWARE ERROR: {e}")
                monitor.emit("reconnecting", "Runtime error; restarting connection")
                print("[!] Restarting the entire Gemini process in 5 seconds to recover...")
                self.sleep(5)
