"""Built-in v10 detection defaults and explicit local opt-outs; no camera."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import device_config


class PoseFallDefaultsTests(unittest.TestCase):
    def test_fresh_device_enables_pose_and_builtin_fall_detection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(device_config, '_config', None), patch.object(
                device_config, 'CONFIG_PATH', str(Path(directory) / 'absent.toml')):
            self.assertIs(device_config.get('camera.pose_tracking'), True)
            self.assertIs(device_config.get('fall_detection.enabled'), True)
            self.assertEqual(device_config.get('camera.pose_model'), 'lite')

    def test_saved_demo_opt_out_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.toml'
            path.write_text('[camera]\npose_tracking=false\n[fall_detection]\nenabled=false\n')
            with patch.object(device_config, '_config', None), patch.object(device_config, 'CONFIG_PATH', str(path)):
                self.assertIs(device_config.get('camera.pose_tracking'), False)
                self.assertIs(device_config.get('fall_detection.enabled'), False)
