import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wifi_setup', Path(__file__).parent / 'payload/ember-wifi-setup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SAVED = '''country=US
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1
network={
    ssid=6e61746579
    priority=100
    key_mgmt=NONE
}
network={
    ssid="Home"
    psk="old-password"
}
'''


class WifiSetupTests(unittest.TestCase):
    def test_new_network_replaces_same_name_and_keeps_others(self):
        text = module.with_network(SAVED, 'Home', 'new-password')
        self.assertTrue(text.startswith('country=US\nctrl_interface='))
        self.assertIn('ssid=6e61746579', text)  # preloaded hotspot survives
        self.assertNotIn('old-password', text)
        self.assertNotIn('new-password', text)  # only the derived PSK is stored
        psk = hashlib.pbkdf2_hmac('sha1', b'new-password', b'Home', 4096, 32).hex()
        self.assertIn('psk=' + psk, text)
        self.assertEqual(text.count('network={'), 2)
        self.assertEqual(module.with_network(text, 'Home', 'new-password'), text)

    def test_open_network_and_validation(self):
        self.assertIn('key_mgmt=NONE', module.network_block('Cafe', ''))
        for ssid, password in [('', 'password1'), ('x' * 33, 'password1'), ('Home', 'short'),
                               ('Home', 'x' * 64), ('Bad\nName', 'password1')]:
            with self.subTest(ssid=ssid, password=password), self.assertRaises(ValueError):
                module.validate(ssid, password)

    def test_save_is_private_and_rejects_bad_input_without_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wpa_supplicant.conf'
            path.write_text(SAVED)
            with self.assertRaises(ValueError):
                module.save_network('Home', 'short', path)
            self.assertEqual(path.read_text(), SAVED)
            module.save_network('Café', 'password1', path)
            self.assertIn('ssid=' + 'Café'.encode().hex(), path.read_text())
            if module.os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_scan_lists_visible_names_once(self):
        output = '\tSSID: Home\n\tSSID: \n\tSSID: Home\n\tSSID: Caf\\xc3\\xa9\n\tSSID: \\x00\\x00\n'
        self.assertEqual(module.scan_ssids(output), ['Home', 'Café'])

    def test_hotspot_is_password_protected_and_page_escapes_names(self):
        config = module.hostapd_config('Ember-Setup-1A2B', 'abcd2345ef')
        self.assertIn('wpa=2', config)
        self.assertIn('wpa_passphrase=abcd2345ef', config)
        with self.assertRaises(ValueError):
            module.hostapd_config('Ember-Setup-1A2B', '')
        html = module.page(['<script>'], 'bad "x"')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_failed_join_reopens_hotspot_until_online(self):
        portal_runs = []
        def start(portal, secret):
            portal_runs.append(secret)
            portal.submitted.set()
            return []
        with patch.object(module, 'start_hotspot', side_effect=start), \
             patch.object(module, 'stop_hotspot'), patch.object(module.time, 'sleep'), \
             patch.object(module, 'join_saved', side_effect=[False, True]):
            module.setup_until_online({'password': 'abcd2345ef'})
        self.assertEqual(len(portal_runs), 2)


if __name__ == '__main__':
    unittest.main()
