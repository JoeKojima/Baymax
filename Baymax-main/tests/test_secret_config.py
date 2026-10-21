from pathlib import Path
import tempfile
import unittest
from secret_config import CredentialError, gemini_key


class CredentialTests(unittest.TestCase):
    def test_file_takes_precedence_and_rotation_is_read_at_each_start(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'credential.txt'
            config = {'GOOGLE_API_KEY_FILE': str(path), 'GOOGLE_API_KEY': 'stale-test-key'}
            path.write_text('synthetic-first-key\n', encoding='utf-8-sig')
            self.assertEqual(gemini_key(config), 'synthetic-first-key')
            path.write_text('synthetic-rotated-key\r\n')
            self.assertEqual(gemini_key(config), 'synthetic-rotated-key')

    def test_bad_file_does_not_fall_back_or_leak_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'credential.txt'
            config = {'GOOGLE_API_KEY_FILE': str(path), 'GOOGLE_API_KEY': 'stale-test-key'}
            for value in [None, '', 'synthetic-secret key', 'x' * 9000]:
                if value is not None:
                    path.write_text(value)
                with self.assertRaises(CredentialError) as error:
                    gemini_key(config)
                self.assertNotIn('synthetic-secret', str(error.exception))
                self.assertNotIn('stale-test-key', str(error.exception))

    def test_existing_environment_key_still_works(self):
        self.assertEqual(gemini_key({'GOOGLE_API_KEY': 'synthetic-test-key'}), 'synthetic-test-key')
        self.assertEqual(gemini_key({}), '')
