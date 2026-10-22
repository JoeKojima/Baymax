"""Memory desktop routing/settings/monitor checks; no devices or network."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from dev_settings import public_configuration, save_configuration
from toolkit.memory_desktop import (DesktopTimeline, configure_runtime, ensure_model,
                                    public_settings, seed_demo, validate_settings, MODEL_FILES)


class DesktopMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_defaults_keep_original_desktop_and_all_experiments_off(self):
        settings = public_settings({})
        self.assertEqual(settings["runtime"], "desktop")
        self.assertEqual(settings["memory_patient"], "")
        self.assertFalse(any(v for k, v in settings.items() if k.startswith("memory_") and isinstance(v, bool)))
        self.assertEqual(configure_runtime(self.root, {}), self.root / "realtime_gemini_8.py")
        self.assertFalse(list(self.root.iterdir()))

    def test_memory_route_uses_v10_and_private_patient_configuration(self):
        values = {"BAYMAX_DESKTOP_RUNTIME": "memory", "BAYMAX_MEMORY_PATIENT": "demo01",
                  "BAYMAX_MEMORY_EXACT": "1", "BAYMAX_MEMORY_TRACE": "1", "GOOGLE_API_KEY": "test-secret"}
        with patch.dict(os.environ, {}, clear=False):
            path = configure_runtime(self.root, values)
            self.assertEqual(path.name, "realtime_gemini_10.py")
            config = tomllib.loads((self.root / ".memory-desktop.toml").read_text())
            self.assertEqual(config["memory"]["patient_id"], "demo01")
            self.assertTrue(config["memory"]["cache_enabled"])
            self.assertTrue(config["observability"]["enabled"])
            self.assertFalse(config["camera"]["pose_tracking"])
            self.assertFalse(config["fall_detection"]["enabled"])
            self.assertNotIn("test-secret", (self.root / ".memory-desktop.toml").read_text())
            self.assertEqual(os.environ["BAYMAX_DATA_DIR"], str(self.root / ".memory-data"))

    def test_settings_save_is_atomic_and_preserves_existing_key(self):
        path = self.root / ".env"
        path.write_text('GOOGLE_API_KEY="private-test-key"\nOTHER="preserved"\n')
        catalog = {"microphones": [], "speakers": [], "cameras": []}
        payload = {"key": "", "microphone": "", "speaker": "", "camera": "none", "runtime": "memory",
                   "memory_patient": "demo01", "memory_exact": True, "memory_trace": True}
        result = save_configuration(path, payload, catalog)
        self.assertTrue(result["memory_exact"])
        self.assertEqual(result["runtime"], "memory")
        self.assertNotIn("private-test-key", json.dumps(result))
        self.assertIn("private-test-key", path.read_text())
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            save_configuration(path, {**payload, "memory_patient": "../unsafe"}, catalog)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(public_configuration(path), result)

    def test_missing_patient_disables_reads_and_invalid_flags_are_rejected(self):
        with patch.dict(os.environ):
            configure_runtime(self.root, {"BAYMAX_DESKTOP_RUNTIME": "memory"})
        config = tomllib.loads((self.root / ".memory-desktop.toml").read_text())
        self.assertEqual(config["memory"]["patient_id"], "")
        with self.assertRaises(ValueError):
            validate_settings({"runtime": "memory", "memory_exact": True}, {})
        with self.assertRaises(ValueError):
            validate_settings({"memory_trace": "yes"}, {})

    def test_monitor_only_contains_numeric_summary_not_memory_contents(self):
        timeline = DesktopTimeline(enabled=False)
        timeline.event("retrieval.ready", source="exact_cache", document="private", patient="name")
        timeline.duration("retrieval.total", 4)
        result = timeline.summary()
        self.assertEqual(result["memory_exact_hits"], 1)
        self.assertEqual(result["memory_last_ms"], 4)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("name", json.dumps(result))
        timeline.close()

    def test_ready_model_has_no_network_or_capture_side_effects(self):
        model = self.root / "all-MiniLM-L6-v2-onnx"
        model.mkdir()
        for name in MODEL_FILES:
            (model / name).write_bytes(b"test-placeholder")
        self.assertEqual(ensure_model(self.root), model)

    def test_demo_records_use_patient_directory_and_stable_ids(self):
        model = self.root / "all-MiniLM-L6-v2-onnx"
        model.mkdir()
        (model / "model.onnx").touch()
        embedder = Mock()
        with patch("semantic_embedder.SemanticEmbedder", return_value=embedder) as constructor:
            result = seed_demo(self.root, {"BAYMAX_MEMORY_PATIENT": "demo01"})
        self.assertEqual(result["count"], 6)
        self.assertEqual(embedder.save.call_args.kwargs["ids"], [f"desktop_demo_{i}" for i in range(6)])
        self.assertEqual(Path(constructor.call_args.kwargs["chroma_dir"]),
                         (self.root / ".memory-data/patients/demo01/chroma_store").resolve())
        self.assertFalse((self.root / "chroma_store").exists())

    def test_pc_entry_preflight_and_launch_use_the_same_selected_file(self):
        import run_pc
        core = self.root / "realtime_gemini_10.py"
        with patch.object(run_pc, "ROOT", self.root), patch.object(run_pc.os, "chdir"), patch.object(
                run_pc, "load_dotenv"), patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key"}, clear=True), patch(
                "toolkit.memory_desktop.configure_runtime", return_value=core), patch.object(
                run_pc, "validate_key") as validate, patch.object(run_pc.runpy, "run_path") as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_pc.main(), 0)
        validate.assert_called_once_with("fake-key", core)
        execute.assert_called_once_with(str(core), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
