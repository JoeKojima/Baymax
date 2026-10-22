"""Offline installer failure/retry tests; no downloads, providers or capture."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

FILE = Path(__file__).resolve().parents[2] / "devops/clickables/bootstrap.py"
spec = importlib.util.spec_from_file_location("ember_bootstrap", FILE)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.ExitStack()
        output.enter_context(contextlib.redirect_stdout(io.StringIO()))
        output.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(output.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        (self.runtime / "requirements-pc.txt").write_text("requests==2.32.5\n")
        self.uv = self.runtime / "uv.exe"
        self.uv.touch()
        self.python = bootstrap.environment_python(self.runtime)
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.marker = self.runtime / ".venv/.ember-ready.json"

    def test_ready_install_does_not_download_or_run_installer(self):
        self.marker.write_text(json.dumps(bootstrap.fingerprint(self.runtime)))
        with patch.object(bootstrap, "run_checked") as run:
            self.assertEqual(bootstrap.ensure_environment(self.runtime, self.uv), self.python)
        run.assert_not_called()

    def test_changed_requirements_trigger_install_model_and_offline_checks(self):
        self.marker.write_text(json.dumps(bootstrap.fingerprint(self.runtime)))
        (self.runtime / "requirements-pc.txt").write_text("requests==2.32.4\n")
        with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch.object(
                bootstrap, "run_checked") as run:
            bootstrap.ensure_environment(self.runtime, self.uv)
        self.assertEqual(run.call_count, 3)
        self.assertIn("install", [str(part) for part in run.call_args_list[0].args[0]])
        self.assertIn("en_core_web_sm", [str(part) for part in run.call_args_list[1].args[0]])
        self.assertIn("check_release.py", [str(part) for part in run.call_args_list[2].args[0]])
        self.assertEqual(json.loads(self.marker.read_text()), bootstrap.fingerprint(self.runtime))

    def test_failed_install_is_not_marked_ready_and_can_be_retried(self):
        with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch.object(
                bootstrap, "run_checked", side_effect=bootstrap.SetupError("failed")):
            with self.assertRaises(bootstrap.SetupError):
                bootstrap.ensure_environment(self.runtime, self.uv)
        self.assertFalse(self.marker.exists())
        with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch.object(
                bootstrap, "run_checked"):
            bootstrap.ensure_environment(self.runtime, self.uv)
        self.assertTrue(self.marker.exists())

    def test_missing_uv_cannot_report_install_success(self):
        self.uv.unlink()
        with self.assertRaises(bootstrap.SetupError):
            bootstrap.ensure_environment(self.runtime, self.uv)
        self.assertFalse(self.marker.exists())

    def test_incompatible_python_is_rejected(self):
        with patch.object(bootstrap.sys, "version_info", (3, 13, 0)):
            with self.assertRaisesRegex(bootstrap.SetupError, "3.12"):
                bootstrap.ensure_environment(self.runtime, self.uv)

    def test_install_lock_rejects_concurrent_click_and_releases_after_failure(self):
        with self.assertRaises(RuntimeError):
            with bootstrap.installation_lock(self.runtime):
                with self.assertRaisesRegex(bootstrap.SetupError, "Another Ember setup"):
                    with bootstrap.installation_lock(self.runtime):
                        self.fail("Two installs entered concurrently")
                raise RuntimeError("simulate cancellation")
        with bootstrap.installation_lock(self.runtime):
            pass

    def test_install_only_does_not_open_ui_or_start_capture(self):
        with patch.object(bootstrap, "PROJECT", self.runtime), patch.object(bootstrap, "RUNTIME", self.runtime), patch.object(
                bootstrap, "ensure_environment", return_value=self.python), patch.object(
                bootstrap, "install_shortcuts") as shortcuts, patch.object(bootstrap.subprocess, "call") as launch:
            self.assertEqual(bootstrap.main(["setup", "--uv", str(self.uv), "--install-only"]), 0)
        shortcuts.assert_called_once_with(self.runtime)
        launch.assert_not_called()

    def test_install_failure_never_launches_runtime(self):
        with patch.object(bootstrap, "RUNTIME", self.runtime), patch.object(
                bootstrap, "ensure_environment", side_effect=bootstrap.SetupError("failed")), patch.object(
                bootstrap.subprocess, "call") as launch:
            self.assertEqual(bootstrap.main(["start", "--uv", str(self.uv)]), 1)
        launch.assert_not_called()

    def test_native_intel_mac_is_reported_before_installing(self):
        with patch.object(bootstrap.sys, "platform", "darwin"), patch.object(
                bootstrap.platform, "machine", return_value="x86_64"), patch.object(bootstrap, "run_checked") as run:
            with self.assertRaisesRegex(bootstrap.SetupError, "Intel"):
                bootstrap.ensure_environment(self.runtime, self.uv)
        run.assert_not_called()
