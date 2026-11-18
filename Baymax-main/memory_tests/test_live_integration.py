"""Actual runtime memory hooks, with simulated devices and provider."""
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
from conftest import FakeEmbedder


@pytest.fixture
def core(monkeypatch, tmp_path):
    for name in ("cv2", "sounddevice", "requests", "google", "google.genai", "google.genai.types",
                 "mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision",
                 "dotenv", "fall_detection", "fall_detection.detector"):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    monkeypatch.setitem(sys.modules, "baymax_cloud", types.SimpleNamespace(DEVICE_KEY=""))
    monkeypatch.setenv("BAYMAX_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("test_live_core", Path(__file__).parents[1] / "realtime_gemini_10.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._notify_webapp = lambda *a, **k: None
    module._live_session_id = "test-session"
    assert not (tmp_path / "chroma_store").exists()
    return module


def message(text=None, audio=False, complete=False, interrupted=False):
    part = types.SimpleNamespace(text=None, inline_data=types.SimpleNamespace(
        mime_type="audio/pcm", data=np.array([0, 100, 200, 300], dtype=np.int16).tobytes()))
    return types.SimpleNamespace(server_content=types.SimpleNamespace(
        input_transcription=types.SimpleNamespace(text=text) if text else None,
        output_transcription=None, model_turn=types.SimpleNamespace(parts=[part]) if audio else None,
        turn_complete=complete, interrupted=interrupted))


class Session:
    def __init__(self, messages):
        self.messages, self.sent, self.order = messages, [], []
        self.finished = asyncio.Event()

    async def receive(self):
        for item in self.messages:
            yield item
            await asyncio.sleep(0)
        self.finished.set()
        await asyncio.Event().wait()

    async def send_client_content(self, **kwargs):
        self.order.append("memory")
        self.sent.append(kwargs)


@pytest.mark.parametrize("delay", [.02, .2])
def test_receiver_keeps_query_deadline_and_memory_before_playback(core, monkeypatch, delay):
    async def run():
        embedder = FakeEmbedder()
        original = embedder.embed
        def embed(query):
            time.sleep(delay)
            return original(query)
        embedder.embed = embed
        service = MemoryService(embedder, "alpha", "fake", settings=MemorySettings(prefetch_enabled=False))
        core._memory_service = service
        await service.start_session(core._live_session_id)
        session = Session([message(text="Sarah garden "), message(audio=True), message(complete=True)])
        append = core._append_playback
        def playback(data):
            session.order.append("playback")
            append(data)
        monkeypatch.setattr(core, "_append_playback", playback)
        task = asyncio.create_task(core.receive_audio(session))
        try:
            await asyncio.wait_for(session.finished.wait(), 1)
            assert core._playback_buffer
            assert session.order == (["memory", "playback"] if delay < .15 else ["playback"])
            if session.sent:
                assert session.sent[0]["turn_complete"] is False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await service.close()
        assert embedder.embed_calls == ["Sarah garden"]
    asyncio.run(run())


@pytest.mark.parametrize("complete", [False, True])
def test_receiver_passes_whitespace_and_discards_preparation_on_interrupt(core, complete):
    async def run():
        service = MagicMock()
        async def lookup(query, sid, tid):
            from ember_memory import RetrievalResult
            return RetrievalResult([], "chroma", 0, sid, tid)
        service.lookup.side_effect = lookup
        core._memory_service = service
        session = Session([message(text="Sa"), message(text="rah"), message(text=" "),
                           message(audio=True), message(interrupted=True, complete=complete)])
        task = asyncio.create_task(core.receive_audio(session))
        try:
            await asyncio.wait_for(session.finished.wait(), .5)
            assert [call.args[0] for call in service.observe_transcript.call_args_list] == ["Sa", "rah", " "]
            service.discard_preparation.assert_called_once()
            assert not core._playback_buffer
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_missing_patient_disables_memory_reads_and_writes(core, tmp_path):
    assert core.PATIENT is None and core._load_memory_embedder() is None
    core._transcript_user.append("A private fact.")
    core._summarise_and_embed()
    core.genai.Client.assert_not_called()
    assert not (tmp_path / "chroma_store").exists()
    assert not list(tmp_path.glob("transcript_*.txt"))


@pytest.mark.parametrize("response", [
    '[{"summary": "Sarah visits.", "tags": [" FAMILY ", "visits", "unknown"]},'
    '{"summary": "An untagged fact.", "tags": []}]', "[]", "not json", None])
def test_shutdown_tags_and_transcript_preserved_on_failure(core, tmp_path, response):
    core.PATIENT = PatientContext.resolve(tmp_path, "alpha")
    core.CHROMA_DIR, core.TRANSCRIPT_DIR = str(core.PATIENT.chroma_dir), str(core.PATIENT.transcript_dir)
    core._transcript_user.append("My daughter Sarah visits.")
    core._memory_embedder = FakeEmbedder()
    core._memory_embedder.save = MagicMock()
    request = core.genai.Client.return_value.models.generate_content
    if response is None:
        request.side_effect = RuntimeError("404 model unavailable")
    else:
        request.return_value.text = response
    core._summarise_and_embed()
    files = list(core.PATIENT.transcript_dir.glob("transcript_*.txt"))
    assert len(files) == 1 and "My daughter Sarah visits." in files[0].read_text()
    if response and response.startswith('[{"summary"'):
        args, kwargs = core._memory_embedder.save.call_args
        assert args[0] == ["Sarah visits.", "An untagged fact."]
        assert kwargs["metadatas"][0]["tags"] == ["family", "visits"]
        assert "tags" not in kwargs["metadatas"][1]
    else:
        core._memory_embedder.save.assert_not_called()


def test_profile_and_initiation_use_selected_patient_worker(core, tmp_path):
    async def run():
        core.PATIENT = PatientContext.resolve(tmp_path, "alpha")
        service = MemoryService(FakeEmbedder(), "alpha", "fake", settings=MemorySettings(prefetch_enabled=False))
        core._memory_service = service
        await service.start_session(core._live_session_id)
        try:
            profile = await service.profile_records()
            assert "Sarah garden alpha" in core._build_config(profile)["system_instruction"]
            session = Session([])
            await core._fire_initiation(session)
            assert "Sarah garden alpha" in session.sent[0]["turns"]["parts"][0]["text"]
        finally:
            await service.close()
    asyncio.run(run())


def test_runtime_preparation_uses_no_gemini_prediction(core, tmp_path, monkeypatch):
    core.PATIENT = PatientContext.resolve(tmp_path, "alpha")
    core._memory_embedder = FakeEmbedder()
    async def sessions():
        service = core._memory_service
        assert service.settings.prefetch_enabled
        await service.start_session("test")
        service.observe_transcript("family ", {"session_id": "test", "turn_id": "1"})
        for _ in range(100):
            if service.embedder.search_calls:
                break
            await asyncio.sleep(.002)
        assert service.embedder.search_calls
    monkeypatch.setattr(core, "_run_sessions", sessions)
    asyncio.run(core.run())
    core.genai.Client.assert_not_called()


def test_runtime_memory_profiler_is_bounded_and_reports_source_totals(core, capsys):
    for value in range(core.PROFILE_WINDOW + 1):
        core._record_memory("retrieval.exact_cache", value)
    tracker = core._memory_trackers["retrieval.exact_cache"]
    assert len(tracker.samples) == core.PROFILE_WINDOW
    assert tracker._count == core.PROFILE_WINDOW + 1
    output = capsys.readouterr().out
    assert "memory.retrieval.exact_cache" in output
    assert f"total={core.PROFILE_WINDOW}" in output
    assert "p50=" in output and "p95=" in output
