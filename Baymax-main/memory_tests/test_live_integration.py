"""Run the real v10 receiver/callback/shutdown functions with fake devices."""
import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from ember_memory import MemoryService, MemorySettings, PatientContext
from ember_timing import AudioTimeline
from conftest import Events, FakeEmbedder


@pytest.fixture
def core(monkeypatch, tmp_path):
    # No SDK, hardware probing, model loading, or disk creation at import.
    for name in ("cv2", "sounddevice", "requests", "google", "google.genai", "google.genai.types",
                 "mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision",
                 "dotenv", "fall_detection", "fall_detection.detector"):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    monkeypatch.setitem(sys.modules, "baymax_cloud", types.SimpleNamespace(DEVICE_KEY=""))
    monkeypatch.setenv("BAYMAX_DATA_DIR", str(tmp_path))
    path = Path(__file__).parents[1] / "realtime_gemini_10.py"
    spec = importlib.util.spec_from_file_location("test_live_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._notify_webapp = lambda *a, **k: None
    module._timeline = Events()
    module._audio_timeline = AudioTimeline(module._timeline)
    module._live_session_id = "a"*32
    module._audio_timeline.reset(module._live_session_id)
    assert not list(tmp_path.iterdir())
    return module


def message(text=None, audio=False, output=None, complete=False, interrupted=False, activity=False):
    part = types.SimpleNamespace(text=None, inline_data=types.SimpleNamespace(
        mime_type="audio/pcm", data=np.array([0, 100, 200, 300], dtype=np.int16).tobytes()))
    content = types.SimpleNamespace(input_transcription=types.SimpleNamespace(text=text) if text else None,
        output_transcription=types.SimpleNamespace(text=output) if output else None,
        model_turn=types.SimpleNamespace(parts=[part]) if audio else None,
        turn_complete=complete, interrupted=interrupted)
    return types.SimpleNamespace(server_content=None if activity else content,
        voice_activity=types.SimpleNamespace(voice_activity_type="ACTIVITY_START") if activity else None)


class Session:
    def __init__(self, messages):
        self.messages, self.sent = messages, []
        self.finished = asyncio.Event()

    async def receive(self):
        for item in self.messages:
            yield item
            await asyncio.sleep(0)
        self.finished.set()
        await asyncio.Event().wait()

    async def send_client_content(self, **kwargs):
        self.sent.append(kwargs)


def test_receiver_preserves_trigger_delivery_and_150ms_deadline(core):
    async def run(delay):
        core._recent_user_words[:] = [f"word{i}" for i in range(40)]
        embedder = FakeEmbedder()
        original = embedder.embed
        def embed(query):
            time.sleep(delay)
            return original(query)
        embedder.embed = embed
        service = MemoryService(embedder, "alpha", "fake", timing=core._timeline)
        core._memory_service = service
        await service.start_session(core._live_session_id)
        session = Session([message(activity=True), message(text="Sarah garden "),
                           message(text="more words "), message(audio=True), message(complete=True)])
        task = asyncio.create_task(core.receive_audio(session))
        await asyncio.wait_for(session.finished.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
        assert embedder.embed_calls == [" ".join([f"word{i}" for i in range(12, 40)] + ["Sarah", "garden"])]
        names = [e[0] for e in core._timeline.events]
        assert "speech.server_notification" in names
        assert core._playback_buffer
        if delay < .15:
            assert len(session.sent) == 1
            assert session.sent[0]["turn_complete"] is False
            assert names.index("reply.first_audio_received") < names.index("memory.send_start") < names.index("playback.enqueued")
        else:
            assert not session.sent
            assert "memory.timeout" in names
        core._flush_playback()
    asyncio.run(run(.02))
    asyncio.run(run(.2))


def test_audio_flush_never_counts_queued_audio_as_played(core):
    tid = "b"*32
    data = np.array([0, 0, 100, 200], dtype=np.int16).tobytes()
    core._append_playback(data, tid)
    core._flush_playback()
    out = np.zeros((4, 1), dtype=np.int16)
    info = types.SimpleNamespace(currentTime=50, outputBufferDacTime=50.01)
    core._output_callback(out, 4, info, types.SimpleNamespace(output_underflow=False))
    assert not np.any(out)
    assert core._audio_timeline.markers.empty()
    core._append_playback(data, tid)
    # First callback contains zeros only; second reaches the first nonzero sample.
    core._output_callback(out[:2], 2, info, types.SimpleNamespace(output_underflow=False))
    assert core._audio_timeline.markers.empty()
    core._output_callback(out[:2], 2, info, types.SimpleNamespace(output_underflow=False))
    kind, _, _ = core._audio_timeline.markers.get_nowait()
    assert kind == "output_activity"
    kind, timestamp, values = core._audio_timeline.markers.get_nowait()
    assert kind == "output" and values[0] == tid and values[2] is False


def test_missing_patient_disables_reads_and_summary_and_keeps_legacy_untouched(core, tmp_path):
    assert core.PATIENT is None
    assert core._load_memory_embedder() is None
    core._transcript_user.append("personal")
    core._summarise_and_embed()
    core.genai.Client.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_shutdown_summary_and_initiation_use_same_patient(core, tmp_path):
    core.PATIENT = PatientContext.resolve(tmp_path, "alpha")
    core.CHROMA_DIR = str(core.PATIENT.chroma_dir)
    core.TRANSCRIPT_DIR = str(core.PATIENT.transcript_dir)
    core._transcript_user.append("Sarah is visiting")
    embedder = FakeEmbedder()
    embedder.save = MagicMock()
    core._memory_embedder = embedder
    core.genai.Client.return_value.models.generate_content.return_value.text = "Sarah is visiting."
    async def run():
        core._memory_service = MemoryService(embedder, "alpha", "fake", settings=MemorySettings(exact_enabled=True))
        await core._memory_service.start_session(core._live_session_id)
        core._recent_user_words[:] = ["Sarah"]
        session = Session([])
        await core._fire_initiation(session)
        assert "Sarah garden alpha" in session.sent[0]["turns"]["parts"][0]["text"]
        assert session.sent[0]["turn_complete"] is True
        core._recent_user_words.clear()
        await core._fire_initiation(session)  # Worker-based random fallback.
        assert len(session.sent) == 2
        await core._memory_service.close()
    asyncio.run(run())
    core._summarise_and_embed()
    embedder.save.assert_called_once()
    assert list(core.PATIENT.transcript_dir.glob("transcript_*.txt"))
    assert not (tmp_path / "chroma_store").exists()


@pytest.mark.parametrize("both_flags", [False, True])
def test_receiver_observes_whitespace_fragments_before_reply_and_flushes_interruptions(core, both_flags):
    async def run():
        class Service:
            def __init__(self):
                self.fragments = []
            async def lookup(self, query, session_id, turn_id):
                from ember_memory import RetrievalResult
                return RetrievalResult([], "chroma", 0, session_id, turn_id)
            def observe_transcript(self, text, context):
                assert not any(e[0] == "reply.first_content" for e in core._timeline.events)
                self.fragments.append(text)
        core._memory_service = Service()
        session = Session([message(text="Sa"), message(text="rah"), message(text=" "),
                           message(text="cancelled"), message(audio=True), message(interrupted=True, complete=both_flags)])
        task = asyncio.create_task(core.receive_audio(session))
        await asyncio.wait_for(session.finished.wait(), .5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert core._memory_service.fragments == ["Sa", "rah", " ", "cancelled"]
        assert not core._playback_buffer and not core._playback_tags
        assert core._audio_timeline.markers.empty()
        assert any(e[0] == "playback.interrupted" for e in core._timeline.events)
        enqueued = next(e[2] for e in core._timeline.events if e[0] == "playback.enqueued")
        interrupted = next(e[2] for e in core._timeline.events if e[0] == "playback.interrupted")
        assert enqueued == interrupted
    asyncio.run(run())


def test_both_pinned_sdk_activity_formats_are_read_without_server_content(core):
    async def run():
        session = Session([
            types.SimpleNamespace(server_content=None, voice_activity_detection_signal=
                types.SimpleNamespace(vad_signal_type="VAD_SIGNAL_TYPE_SOS")),
            types.SimpleNamespace(server_content=None, voice_activity_detection_signal=
                types.SimpleNamespace(vad_signal_type="VAD_SIGNAL_TYPE_EOS")),
            types.SimpleNamespace(server_content=None, voice_activity=
                types.SimpleNamespace(voice_activity_type="ACTIVITY_START")),
            types.SimpleNamespace(server_content=None, voice_activity=
                types.SimpleNamespace(voice_activity_type="ACTIVITY_END")),
        ])
        task = asyncio.create_task(core.receive_audio(session))
        await asyncio.wait_for(session.finished.wait(), .5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kinds = [e[3]["kind"] for e in core._timeline.events if e[0] == "speech.server_notification"]
        assert kinds == ["activity_start", "activity_end", "activity_start", "activity_end"]
    asyncio.run(run())


def test_pc_audio_selection_uses_saved_devices_without_pipewire(core, monkeypatch):
    from unittest.mock import Mock
    core.IS_PC = True
    select = Mock(return_value=(1, 2))
    monkeypatch.setattr("pc_hardware.select_audio_devices", select)
    core.sd.query_devices.return_value = {"max_input_channels": 1}
    core._configure_audio()
    select.assert_called_once_with(core.sd)
    assert core.INPUT_CHANNELS == 1
    assert core.sd.default.device == [1, 2]


def test_pc_audio_only_never_opens_camera(core, monkeypatch):
    from unittest.mock import Mock
    core.IS_PC = True
    core.POSE_TRACKING_ENABLED = core.FALL_DETECTION_ENABLED = False
    monkeypatch.setattr("pc_hardware.camera_candidates", lambda: [])
    opened = Mock()
    monkeypatch.setattr("pc_hardware.open_camera", opened)
    core._fall_detection_thread()
    opened.assert_not_called()


def test_memory_pc_receiver_feeds_desktop_monitor(core):
    from unittest.mock import Mock
    async def run():
        core.IS_PC = True
        core.desktop_monitor = Mock()
        service = MemoryService(FakeEmbedder(), "alpha", "fake")
        core._memory_service = service
        await service.start_session(core._live_session_id)
        session = Session([message(text="Sarah garden "), message(output="Sarah enjoys gardening. ", audio=True),
                           message(complete=True)])
        task = asyncio.create_task(core.receive_audio(session))
        await asyncio.wait_for(session.finished.wait(), .5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        core.desktop_monitor.transcribe.assert_any_call("user", "Sarah garden ")
        core.desktop_monitor.transcribe.assert_any_call("ember", "Sarah enjoys gardening. ")
        core.desktop_monitor.emit.assert_any_call("listening", "Reply complete")
        await service.close()
    asyncio.run(run())
