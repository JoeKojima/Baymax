from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import devUI
from dev_settings import read_configuration

CATALOG = {"microphones": [{"index": 8, "name": "Test mic", "hostapi": "Test API"}],
           "speakers": [{"index": 7, "name": "Test speaker", "hostapi": "Test API"}],
           "cameras": []}


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.path.write_text("GOOGLE_API_KEY=existing-fake-key\n")
        self.env = patch.object(devUI, "ENV", self.path)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), devUI.Handler)
        self.server.token = "test-token"
        self.server.catalog = CATALOG
        self.server.settings_lock = threading.Lock()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, token="test-token", origin=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "Origin": origin or self.url,
                                                  "X-devUI-Token": token})
        return urllib.request.urlopen(request)

    def test_memory_palace_page_and_api_use_existing_local_protections(self):
        with self.request("/memory-palace") as response:
            page = response.read().decode()
            self.assertIn("Memory Palace", page)
            self.assertIn("test-token", page)
            self.assertNotIn("existing-fake-key", page)
        snapshot = {"available": True, "people": [{"name": "Alex"}], "memories": []}
        with patch.object(devUI, "read_memory_snapshot", return_value=snapshot):
            with self.request("/api/memory-palace", {}) as response:
                self.assertEqual(json.loads(response.read()), snapshot)
            for token, origin in [("wrong", self.url), ("test-token", "https://other.example")]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request("/api/memory-palace", {}, token=token, origin=origin)
                self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/.runtime-memory.json")
        self.assertEqual(error.exception.code, 404)

    def test_speaker_confirmation_uses_existing_local_protections(self):
        import runtime_monitor
        snapshot={'live':True,'pid':123,'people':[{'id':'p-alex','name':'Alex'}]}
        with patch.object(devUI,'read_memory_snapshot',return_value=snapshot), patch.object(
                devUI,'read_status',return_value={'pid':123}), patch.object(runtime_monitor,'MONITOR_DIR',Path(self.temp.name)):
            body={'person_id':'p-alex','single_speaker':True}
            with self.request('/api/memory-palace/speaker',body) as response:
                self.assertEqual(json.loads(response.read())['status'],'queued')
            command=json.loads((Path(self.temp.name)/'.runtime-speaker-command.json').read_text())
            self.assertEqual(command['person_id'],'p-alex');self.assertEqual(command['pid'],123)
            for bad in ({'person_id':'invented','single_speaker':True},{'person_id':'p-alex','single_speaker':False}):
                with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/memory-palace/speaker',bad)
                self.assertEqual(error.exception.code,400)
            for token,origin in [('wrong',self.url),('test-token','https://other.example')]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request('/api/memory-palace/speaker',body,token=token,origin=origin)
                self.assertEqual(error.exception.code,403)

    def test_mac_save_resolves_cached_camera_uid_again_before_persisting(self):
        self.server.catalog = {**CATALOG, "cameras": [{"index": 0, "name": "USB", "uid": "saved"}]}
        fresh = [{"index": 0, "name": "Other", "uid": "new"}, {"index": 1, "name": "USB", "uid": "saved"}]
        with patch("sys.platform", "darwin"), patch.object(devUI, "audio_catalog", return_value=CATALOG), patch.object(
                devUI, "camera_catalog", return_value=fresh):
            with self.request("/api/save", {"key": "", "microphone": "8", "speaker": "7", "camera": "0"}) as response:
                result = json.loads(response.read())
        self.assertEqual(result["camera"], "1")
        self.assertEqual(result["camera_uid"], "saved")

    def test_mac_disconnected_camera_cannot_overwrite_settings(self):
        before = self.path.read_bytes()
        self.server.catalog = {**CATALOG, "cameras": [{"index": 0, "name": "USB", "uid": "saved"}]}
        with patch("sys.platform", "darwin"), patch.object(devUI, "audio_catalog", return_value=CATALOG), patch.object(
                devUI, "camera_catalog", return_value=[]):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/save", {"key": "", "microphone": "8", "speaker": "7", "camera": "0"})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_mac_camera_test_passes_uid_to_isolated_capture_process(self):
        self.server.catalog = {**CATALOG, "cameras": [{"index": 0, "name": "USB", "uid": "saved"}]}
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"message": "simulated preview"}))
        with patch("sys.platform", "darwin"), patch.object(devUI.subprocess, "run", return_value=result) as run:
            with self.request("/api/test", {"kind": "camera", "device": "0"}):
                pass
        self.assertEqual(run.call_args.args[0][-3:], ["camera", "0", "saved"])

    def test_open_and_status_do_not_modify_settings_or_expose_key(self):
        before = self.path.read_bytes()
        with self.request("/") as response:
            page = response.read().decode()
            self.assertIn("Save configuration", page)
            self.assertNotIn("existing-fake-key", page)
        with self.request("/api/status", {}) as response:
            status = response.read().decode()
            self.assertTrue(json.loads(status)["key_configured"])
            self.assertNotIn("existing-fake-key", status)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/.env")
        self.assertEqual(error.exception.code, 404)

    def test_save_and_reopen_restore_selected_devices(self):
        with patch.object(devUI, "audio_catalog", return_value={"microphones": CATALOG["microphones"], "speakers": CATALOG["speakers"]}):
            with self.request("/api/save", {"key": "", "microphone": "8", "speaker": "7", "camera": "none"}) as response:
                self.assertNotIn("existing-fake-key", response.read().decode())
        with self.request("/api/status", {}) as response:
            status = json.load(response)
        self.assertEqual((status["microphone"], status["speaker"], status["camera"]), ("8", "7", "none"))
        self.assertEqual(read_configuration(self.path)["GOOGLE_API_KEY"], "existing-fake-key")

    def test_foreign_origin_and_missing_token_cannot_save(self):
        before = self.path.read_bytes()
        for origin, token in (("https://other.example", "test-token"), (self.url, "")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/save", {}, token=token, origin=origin)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.path.read_bytes(), before)


    def test_only_one_devui_server_can_own_a_port(self):
        first = devUI.LocalServer(("127.0.0.1", 0), devUI.Handler)
        try:
            with self.assertRaises(OSError):
                devUI.LocalServer(("127.0.0.1", first.server_port), devUI.Handler)
        finally:
            first.server_close()

    def test_device_test_returns_preview_without_writing_settings(self):
        before = self.path.read_bytes()
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"image": "data:image/jpeg;base64,test", "message": "Preview"}))
        with patch("sys.platform", "linux"), patch.object(devUI.subprocess, "run", return_value=result) as run:
            with self.request("/api/test", {"kind": "camera", "device": "0"}) as response:
                self.assertEqual(json.load(response)["message"], "Preview")
            self.assertEqual(run.call_args.args[0][-2:], ["camera", "0"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_device_failure_is_reported_without_saving(self):
        before = self.path.read_bytes()
        result = SimpleNamespace(returncode=1, stdout=json.dumps({"error": "Device busy"}))
        with patch.object(devUI.subprocess, "run", return_value=result):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/test", {"kind": "microphone", "device": "8"})
            self.assertEqual(json.load(error.exception)["error"], "Device busy")
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_test_cannot_start_subprocess(self):
        with patch.object(devUI.subprocess, "run") as run:
            with self.assertRaises(urllib.error.HTTPError):
                self.request("/api/test", {"kind": "other", "device": "0"})
            run.assert_not_called()

    def test_monitor_endpoint_returns_activity_without_config(self):
        with patch.object(devUI, 'read_status', return_value={'state': 'calling_tool', 'detail': 'Calling toolkit get_weather'}):
            with self.request('/api/runtime', {}) as response:
                body = response.read().decode()
                self.assertEqual(json.loads(body)['state'], 'calling_tool')
                self.assertNotIn('existing-fake-key', body)
        with self.request('/monitor') as response:
            self.assertIn('Ember dev monitor', response.read().decode())


if __name__ == "__main__":
    unittest.main()
