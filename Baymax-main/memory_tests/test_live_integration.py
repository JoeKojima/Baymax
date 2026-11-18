"""Actual runtime memory hooks, with simulated devices and provider."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_memory import PatientContext
from conftest import FakeEmbedder, until


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
    return module


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


@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_preparation_switch_and_no_gemini_prediction(core, tmp_path, monkeypatch, enabled):
    core.PATIENT = PatientContext.resolve(tmp_path, "alpha")
    core._memory_embedder = FakeEmbedder()
    original_get = core.device_config.get
    monkeypatch.setattr(core.device_config, "get", lambda key, *args:
        {"prefetch_enabled": enabled} if key == "memory" else original_get(key, *args))
    async def sessions():
        service = core._memory_service
        assert service.prefetch is enabled
        await service.start_session("test")
        service.observe_transcript("family ", "test", "1")
        if enabled:
            await until(lambda: bool(service.embedder.search_calls))
        else:
            assert not service.embedder.embed_calls
    monkeypatch.setattr(core, "_run_sessions", sessions)
    asyncio.run(core.run())
    core.genai.Client.assert_not_called()
