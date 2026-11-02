import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('image_builder', Path(__file__).parent / 'build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def test_new_images_get_distinct_access_credentials_and_hostnames(self):
        first, second = builder.generate_credentials(), builder.generate_credentials()
        self.assertNotEqual(first['hostname'], second['hostname'])
        self.assertNotEqual(first['linux_password'], second['linux_password'])
        self.assertNotEqual(first['linux_password'], first['anydesk_password'])
        self.assertGreaterEqual(len(first['anydesk_password']), 24)

    def test_fixed_access_password_sets_both_logins(self):
        fixed = builder.generate_credentials('longenough')
        self.assertEqual((fixed['linux_password'], fixed['anydesk_password']), ('longenough', 'longenough'))

    def test_bad_download_never_replaces_cached_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            path = cache / 'asset'
            path.write_bytes(b'previous')
            spec = {'url': 'https://example.com/asset', 'sha256': hashlib.sha256(b'expected').hexdigest()}
            def download(command, check):
                Path(command[command.index('--output') + 1]).write_bytes(b'wrong')
            with patch('subprocess.run', side_effect=download), self.assertRaisesRegex(ValueError, 'checksum'):
                builder.asset('asset', spec, cache)
            self.assertEqual(path.read_bytes(), b'previous')
            self.assertFalse((cache / 'asset.download').exists())

    def test_verified_cache_does_not_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'asset').write_bytes(b'expected')
            spec = {'url': 'https://example.com/asset', 'sha256': hashlib.sha256(b'expected').hexdigest()}
            with patch('subprocess.run') as request:
                builder.asset('asset', spec, cache)
                request.assert_not_called()

    def test_prebuilt_wheels_match_the_image_python_and_reach_uv(self):
        here = Path(__file__).parent
        wheels = [name for name in builder.json.loads((here / 'assets.json').read_text()) if name.endswith('.whl')]
        self.assertTrue(wheels)
        for name in wheels:
            self.assertTrue(name.endswith('-cp312-cp312-linux_aarch64.whl'), name)
        self.assertIn("payload.glob('*.whl')", (here / 'inject_image.py').read_text())
        self.assertIn('UV_FIND_LINKS=/opt/ember-provision/wheels', (here / 'payload/ember-update').read_text())

    def test_preinstalled_and_fallback_packages_share_one_list(self):
        here = Path(__file__).parent
        self.assertTrue((here / 'payload/system-packages.txt').read_text().split())
        self.assertIn('/opt/ember-provision/system-packages.txt', (here / 'payload/ember-provision').read_text())
        self.assertIn('payload/system-packages.txt', (here / 'build_linux.sh').read_text())
        self.assertIn('/var/lib/ember/packages-baked', (here / 'payload/ember-provision').read_text())
