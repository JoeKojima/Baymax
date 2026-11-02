import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('device_config', Path(__file__).parent / 'payload/ember-device-config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeviceConfigTests(unittest.TestCase):
    def test_defaults_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings/device.json'
            self.assertEqual(module.load(path), module.DEFAULTS)
            settings = module.DEFAULTS | {'branch': 'release/board-1', 'auto_update': False}
            module.save(path, settings)
            self.assertEqual(module.load(path), settings)
            self.assertEqual(list(path.parent.glob('.device-*')), [])

    def test_rejects_credentials_and_invalid_branches(self):
        for repository in ['https://user:secret@example.com/repo.git', 'file:///tmp/repo',
                           'https://example.com/repo\n.git', 'https://example.com/repo?token=secret']:
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                module.validate(module.DEFAULTS | {'repository': repository})
        for branch in ['-main', '../main', 'main..bad', 'branch with space', 'main.lock', '.hidden']:
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                module.validate(module.DEFAULTS | {'branch': branch})

    def test_invalid_save_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            module.save(path, module.DEFAULTS)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                module.save(path, module.DEFAULTS | {'auto_update': 'false'})
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
