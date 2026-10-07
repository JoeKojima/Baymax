import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from dev_settings import public_configuration, read_configuration, save_configuration
from pc_hardware import camera_candidates, select_audio_devices

CATALOG = {"microphones": [{"index": 8, "name": "Mic ${literal}", "hostapi": "WASAPI"}],
           "speakers": [{"index": 7, "name": "Speakers", "hostapi": "WASAPI"}],
           "cameras": [{"index": 3, "name": "Camera 3"}]}


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"

    def payload(self, **changes):
        return dict({"key": "test-key-not-real", "microphone": "8", "speaker": "7", "camera": "3"}, **changes)

    def test_save_persists_all_devices_and_key_without_returning_key(self):
        result = save_configuration(self.path, self.payload(), CATALOG)
        stored = read_configuration(self.path)
        self.assertEqual(stored["GOOGLE_API_KEY"], "test-key-not-real")
        self.assertEqual(stored["BAYMAX_MIC_NAME"], "Mic ${literal}")
        self.assertEqual(stored["BAYMAX_CAMERA_DEVICE"], "3")
        self.assertTrue(result["key_configured"])
        self.assertNotIn("test-key-not-real", json.dumps(result))
        self.assertEqual(public_configuration(self.path), result)

    def test_blank_key_preserves_existing_key_and_other_settings(self):
        self.path.write_text("GOOGLE_API_KEY=existing-test-key\nOTHER=value\n# Keep this comment\n")
        save_configuration(self.path, self.payload(key="", camera="none"), CATALOG)
        self.assertEqual(read_configuration(self.path)["GOOGLE_API_KEY"], "existing-test-key")
        self.assertIn("OTHER=value", self.path.read_text())
        self.assertIn("# Keep this comment", self.path.read_text())

    def test_invalid_save_does_not_change_existing_configuration(self):
        self.path.write_text("GOOGLE_API_KEY=original-test-key\n")
        before = self.path.read_bytes()
        for changes in ({"microphone": "7"}, {"speaker": "100"}, {"camera": "9"}, {"key": "bad\nkey"}):
            with self.assertRaises(ValueError):
                save_configuration(self.path, self.payload(**changes), CATALOG)
            self.assertEqual(self.path.read_bytes(), before)

    def test_fresh_install_can_save_hardware_without_key(self):
        result = save_configuration(self.path, self.payload(key="", microphone="", speaker="", camera="none"), CATALOG)
        self.assertFalse(result["key_configured"])
        self.assertEqual(result["camera"], "none")

    def test_camera_selection_and_disabled_camera_reach_adapter(self):
        for value, expected in (("3", [3]), ("none", []), ("", [0, 1, 2])):
            with patch.dict(os.environ, {"BAYMAX_CAMERA_DEVICE": value}):
                self.assertEqual(camera_candidates(), expected)

    def test_saved_audio_identity_survives_index_reordering(self):
        class Devices:
            def query_devices(self):
                return [{"name": "Speaker", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
                        {"name": "Mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0}]
            def query_hostapis(self):
                return [{"name": "WASAPI"}]
            def check_input_settings(self, **_):
                pass
            def check_output_settings(self, **_):
                pass
            class default:
                device = (-1, -1)
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "0", "BAYMAX_MIC_NAME": "Mic", "BAYMAX_MIC_HOSTAPI": "WASAPI",
                                     "BAYMAX_SPEAKER_DEVICE": "1", "BAYMAX_SPEAKER_NAME": "Speaker", "BAYMAX_SPEAKER_HOSTAPI": "WASAPI"}):
            self.assertEqual(select_audio_devices(Devices()), (1, 0))


if __name__ == "__main__":
    unittest.main()
