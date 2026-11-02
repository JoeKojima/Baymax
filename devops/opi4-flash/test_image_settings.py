from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from configure_image import save_private
from image_settings import validate, load, source, supplicant, dietpi_wifi


class ImageSettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'pr': 20, 'wifi': {'ssid': 'IEEE', 'password': 'password'}, 'gemini_api_key': 'test-key'}

    def test_three_settings_round_trip_and_follow_pr_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.json'
            save_private(path, self.settings)
            self.assertEqual(load(path), self.settings)
            self.assertEqual(source(load(path))['branch'], 'refs/pull/20/head')
            self.assertTrue(source(load(path))['auto_update'])

    def test_optional_access_password_is_validated(self):
        self.assertEqual(validate(self.settings | {'access_password': 'longenough'})['access_password'], 'longenough')
        for bad in ('short', 'has space1', 42):
            with self.assertRaises(ValueError):
                validate(self.settings | {'access_password': bad})

    def test_wpa_derivation_matches_known_vector(self):
        text = supplicant(self.settings['wifi'])
        self.assertIn('ssid=49454545', text)
        self.assertIn('psk=f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e', text)
        self.assertNotIn('password', text)

    def test_open_network_has_no_psk(self):
        text = supplicant({'ssid': 'Apartment', 'password': ''})
        self.assertIn('key_mgmt=NONE', text)
        self.assertNotIn('psk=', text)

    def test_shell_metacharacters_are_quoted_and_missing_fields_rejected(self):
        text = dietpi_wifi("aWIFI_SSID[0]=''\naWIFI_KEY[0]=''\n", {'ssid': "O'Brien $(echo test)", 'password': 'pass$word'})
        self.assertIn("'\"'\"'", text)
        self.assertIn("'pass$word'", text)
        with self.assertRaises(ValueError):
            dietpi_wifi('', self.settings['wifi'])

    def test_bad_values_do_not_replace_valid_settings(self):
        for field, invalid in (('pr', True), ('pr', -1), ('gemini_api_key', 'key\ncommand')):
            data = deepcopy(self.settings)
            data[field] = invalid
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'settings.json'
                save_private(path, self.settings)
                with self.assertRaises(ValueError):
                    save_private(path, data)
                self.assertEqual(load(path), self.settings)
        for wifi in ({'ssid': 'x' * 33, 'password': ''}, {'ssid': 'name\nother', 'password': ''},
                     {'ssid': 'ok', 'password': 'short'}):
            with self.assertRaises(ValueError):
                validate(self.settings | {'wifi': wifi})


if __name__ == '__main__':
    unittest.main()
