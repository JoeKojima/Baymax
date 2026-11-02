import importlib.util
import hashlib
import io
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

    def test_bad_download_never_replaces_cached_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            path = cache / 'asset'
            path.write_bytes(b'previous')
            spec = {'url': 'https://example.com/asset', 'sha256': hashlib.sha256(b'expected').hexdigest()}
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'wrong')), self.assertRaisesRegex(ValueError, 'checksum'):
                builder.asset('asset', spec, cache)
            self.assertEqual(path.read_bytes(), b'previous')
            self.assertFalse((cache / 'asset.download').exists())

    def test_verified_cache_does_not_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'asset').write_bytes(b'expected')
            spec = {'url': 'https://example.com/asset', 'sha256': hashlib.sha256(b'expected').hexdigest()}
            with patch('urllib.request.urlopen') as request:
                builder.asset('asset', spec, cache)
                request.assert_not_called()
