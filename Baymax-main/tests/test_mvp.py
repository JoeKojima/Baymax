"""MVP first-run, saved configuration and startup checks; no capture or providers."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dev_settings import save_configuration
import run_mvp

CATALOG = {"microphones": [{"index": 2, "name": "Fixed mic", "hostapi": "ALSA"}],
           "speakers": [{"index": 3, "name": "Fixed speaker", "hostapi": "ALSA"}],
           "cameras": [{"index": 0, "name": "Fixed camera"}]}


class MvpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(run_mvp, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.payload = dict(key="fake-key", microphone="2", speaker="3", camera="0")

    def save(self, **changes):
        save_configuration(self.root / ".env", {**self.payload, **changes}, CATALOG)

    def test_key_alone_does_not_skip_device_configuration(self):
        (self.root / ".env").write_text("GOOGLE_API_KEY=fake-key\n")
        self.assertFalse(run_mvp.configuration_ready(self.root / ".env"))

    def test_saved_devices_and_explicit_no_camera_are_ready(self):
        self.assertFalse(run_mvp.configuration_ready(self.root / ".env"))
        self.save(camera="none")
        self.assertTrue(run_mvp.configuration_ready(self.root / ".env"))
        self.save(camera="")
        self.assertFalse(run_mvp.configuration_ready(self.root / ".env"))

    def test_first_setup_then_next_start_skips_setup(self):
        def setup():
            self.save()
            return True
        with patch.object(run_mvp, "configure_once", side_effect=setup) as configure, patch(
                "run_pc.main", return_value=0) as start:
            self.assertEqual(run_mvp.main([]), 0)
            self.assertEqual(run_mvp.main([]), 0)
        self.assertEqual(configure.call_count, 1)
        self.assertEqual(start.call_count, 2)

    def test_runtime_failure_does_not_reopen_setup_or_rewrite_devices(self):
        self.save()
        before = (self.root / ".env").read_bytes()
        with patch.object(run_mvp, "configure_once") as configure, patch("run_pc.main", return_value=1):
            self.assertEqual(run_mvp.main([]), 1)
        configure.assert_not_called()
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_explicit_setup_reopens_ui(self):
        self.save()
        with patch.object(run_mvp, "configure_once", return_value=True) as configure, patch("run_pc.main", return_value=0):
            self.assertEqual(run_mvp.main(["--setup"]), 0)
        configure.assert_called_once()

    def test_cancelled_setup_never_starts_capture(self):
        with patch.object(run_mvp, "configure_once", return_value=False), patch("run_pc.main") as start:
            self.assertEqual(run_mvp.main([]), 0)
        start.assert_not_called()

    def test_lock_rejects_second_launcher_and_releases(self):
        path = self.root / ".mvp.lock"
        with run_mvp.runtime_lock(path):
            with self.assertRaisesRegex(RuntimeError, "already"):
                with run_mvp.runtime_lock(path):
                    self.fail("second launcher entered")
        with run_mvp.runtime_lock(path):
            pass

    def test_existing_runtime_prevents_setup_and_capture(self):
        with patch.object(run_mvp, "read_status", return_value={"state": "listening"}), patch.object(
                run_mvp, "configure_once") as configure, patch("run_pc.main") as start:
            self.assertEqual(run_mvp.main([]), 0)
        configure.assert_not_called()
        start.assert_not_called()

    def test_autostart_is_user_scoped_and_points_to_private_python(self):
        source = Path(__file__).resolve().parents[2] / "devops/mvp/install_autostart.py"
        spec = importlib.util.spec_from_file_location("mvp_install", source)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        project = self.root / "Ember with spaces"
        python = project / "Baymax-main/.venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        config_home = self.root / "user-config"
        unit, entry = installer.install(project, self.root, config_home)
        service = unit.read_text()
        self.assertIn(f'ExecStart={installer.systemd_quote(python)}', service)
        self.assertIn("run_mvp.py", service)
        self.assertIn("KillSignal=SIGTERM", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("start-session.sh", entry.read_text())
        self.assertFalse((self.root / "etc").exists())


if __name__ == "__main__":
    unittest.main()
