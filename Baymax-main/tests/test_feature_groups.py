"""Grouped settings preserve old launch/import APIs and keep dormant code inert."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from features import settings
from dev_settings import save_configuration, public_configuration
from test_public_core_integration import load_function

ROOT = Path(__file__).resolve().parents[1]


class FeatureGroupsTests(unittest.TestCase):
    def test_profile_uses_opi_pr_and_keeps_memory_and_recognition_off(self):
        profile = json.loads((ROOT.parent / 'devops/opi4/flash-profile.json').read_text())
        self.assertEqual(profile['software_pr'], 29)
        for name in ('Memory Palace', 'People Recognition'):
            self.assertTrue(profile['groups'][name]['packaged'])
            self.assertFalse(profile['groups'][name]['enabled'])
        self.assertEqual([g['name'] for g in settings.groups()], list(profile['groups']))

    def test_legacy_imports_share_the_actual_modules_and_state(self):
        for old, new in (
            ('run_mvp', 'features.startup.launcher'),
            ('features.conversational_ability.engage', 'features.conversation.engage'),
            ('features.toolkits.fall_detection', 'features.pose_detection.worker'),
            ('features.vision_process', 'features.pose_detection.vision_process'),
            ('fall_detection.detector', 'features.pose_detection.fall_detection.detector'),
            ('face_id', 'features.person_recognition.face_id'),
            ('person_memory', 'features.memory_palace.person_memory'),
        ):
            self.assertIs(importlib.import_module(old), importlib.import_module(new))

    def test_disabled_packages_are_not_imported_by_setup_or_feature_inventory(self):
        code = '''import sys
from features.settings import groups
import dev_settings
groups()
assert not any(n.startswith(('features.memory_palace', 'features.person_recognition')) for n in sys.modules)
'''
        subprocess.run([sys.executable, '-c', code], cwd=ROOT, check=True, capture_output=True)

    def test_feature_switches_round_trip_and_partial_save_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('GOOGLE_API_KEY=existing\nEMBER_SESSION_MEMORY_ENABLED=0\n')
            catalog = {'microphones': [], 'speakers': [], 'cameras': []}
            payload = {'microphone': '', 'speaker': '', 'camera': 'none',
                       'feature_switches': {'voice_sleep': False, 'eyes': False}}
            saved = save_configuration(path, payload, catalog)
            self.assertFalse(saved['feature_switches']['voice_sleep'])
            payload.pop('feature_switches')
            save_configuration(path, payload, catalog)
            self.assertFalse(public_configuration(path)['feature_switches']['eyes'])
            self.assertIn('EMBER_SESSION_MEMORY_ENABLED=0', path.read_text())
            self.assertIn('GOOGLE_API_KEY="existing"', path.read_text())

    def test_invalid_switches_are_rejected_before_writing(self):
        for invalid in ({'unknown': True}, {'voice_sleep': 'yes'}, []):
            with self.assertRaises(ValueError):
                settings.validate(invalid)

    def test_setup_respects_existing_device_pose_opt_outs(self):
        with patch('device_config.get', return_value=False):
            self.assertFalse(settings.public({})['pose_tracking'])
            self.assertFalse(settings.public({})['builtin_fall_detection'])
            self.assertTrue(settings.public({'EMBER_POSE_TRACKING': '1'})['pose_tracking'])

    def test_flash_profile_disables_legacy_summary_before_provider_or_storage_access(self):
        summarize = load_function('_summarise_and_embed', {'os': os})
        with patch.dict(os.environ, {'EMBER_SESSION_MEMORY_ENABLED': '0'}):
            self.assertIsNone(summarize())


if __name__ == '__main__':
    unittest.main()
