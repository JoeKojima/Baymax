"""macOS contract simulations: run on Windows without accessing real devices."""
from contextlib import nullcontext
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import devUI
import macos_devices
from dev_settings import public_configuration, read_configuration, save_configuration
from pc_hardware import camera_candidates, configure_audio_backend, open_camera, select_audio_devices


class NativeString(str):
    def compare_(self, other):
        return (self > other) - (self < other)


def native_camera(uid, name):
    return SimpleNamespace(uniqueID=lambda: NativeString(uid), localizedName=lambda: name)


class MacDeviceTests(unittest.TestCase):
    def setUp(self):
        platform = patch("sys.platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        env = patch.dict(os.environ, {"BAYMAX_CAMERA_DEVICE": "0", "BAYMAX_CAMERA_NAME": "",
                                     "BAYMAX_CAMERA_UID": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_native_names_and_indices_match_opencv_uid_order_including_muxed(self):
        native = Mock()
        native.devicesWithMediaType_.side_effect = [[native_camera("z", "FaceTime HD Camera")],
                                                   [native_camera("a", "USB Camera")]]
        av = SimpleNamespace(AVCaptureDevice=native, AVMediaTypeVideo="video", AVMediaTypeMuxed="muxed")
        with patch.object(macos_devices, "_framework", return_value=av), patch.dict(
                "sys.modules", {"objc": SimpleNamespace(autorelease_pool=nullcontext)}):
            result = macos_devices.camera_devices()
        self.assertEqual(result, [{"index": 0, "name": "USB Camera", "uid": "a"},
                                  {"index": 1, "name": "FaceTime HD Camera", "uid": "z"}])
        self.assertEqual([call.args[0] for call in native.devicesWithMediaType_.call_args_list], ["video", "muxed"])

    def test_camera_catalog_does_not_open_capture(self):
        devices = [{"index": 0, "name": "FaceTime HD Camera", "uid": "built-in"}]
        with patch.object(macos_devices, "camera_devices", return_value=devices), patch.object(devUI, "open_camera") as capture:
            self.assertEqual(devUI.camera_catalog(), devices)
        capture.assert_not_called()

    def test_saved_camera_uid_survives_reordering_and_renaming(self):
        devices = [{"index": 0, "name": "Same name", "uid": "new"},
                   {"index": 1, "name": "Renamed camera", "uid": "saved"}]
        with patch.object(macos_devices, "camera_devices", return_value=devices), patch.dict(
                os.environ, {"BAYMAX_CAMERA_UID": "saved", "BAYMAX_CAMERA_NAME": "Old name"}):
            self.assertEqual(camera_candidates(), [1])

    def test_disconnected_uid_never_falls_back_to_different_camera(self):
        with patch.object(macos_devices, "camera_devices", return_value=[{"index": 0, "name": "Camera", "uid": "other"}]):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                macos_devices.resolve_camera(0, "missing", "Camera")

    def test_legacy_duplicate_names_are_rejected(self):
        devices = [{"index": i, "name": "USB Camera", "uid": str(i)} for i in range(2)]
        with patch.object(macos_devices, "camera_devices", return_value=devices):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                macos_devices.resolve_camera(0, name="USB Camera")

    def test_legacy_unique_name_can_migrate(self):
        with patch.object(macos_devices, "camera_devices", return_value=[{"index": 2, "name": "USB", "uid": "u"}]):
            self.assertEqual(macos_devices.resolve_camera(0, name="USB"), 2)

    def test_auto_camera_uses_native_catalog_not_fixed_probe_range(self):
        with patch.dict(os.environ, {"BAYMAX_CAMERA_DEVICE": ""}), patch.object(
                macos_devices, "camera_devices", return_value=[{"index": 6}]):
            self.assertEqual(camera_candidates(), [6])

    def test_disabled_camera_does_not_load_native_framework(self):
        with patch.dict(os.environ, {"BAYMAX_CAMERA_DEVICE": "none"}), patch.object(macos_devices, "_framework") as native:
            self.assertEqual(camera_candidates(), [])
        native.assert_not_called()

    def test_camera_uid_persists_and_disabled_selection_clears_it(self):
        catalog = {"microphones": [], "speakers": [],
                   "cameras": [{"index": 0, "name": "USB Camera", "uid": "stable-id"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            payload = {"key": "", "microphone": "", "speaker": "", "camera": "0"}
            save_configuration(path, payload, catalog)
            self.assertEqual(public_configuration(path)["camera_uid"], "stable-id")
            self.assertEqual(read_configuration(path)["BAYMAX_CAMERA_UID"], "stable-id")
            save_configuration(path, dict(payload, camera="none"), catalog)
            self.assertEqual(public_configuration(path)["camera_uid"], "")

    def test_denied_or_restricted_camera_permission_explains_settings(self):
        for status in (1, 2):
            av = SimpleNamespace(AVCaptureDevice=SimpleNamespace(authorizationStatusForMediaType_=lambda _: status),
                                 AVMediaTypeVideo="video", AVAuthorizationStatusDenied=2, AVAuthorizationStatusRestricted=1)
            with patch.object(macos_devices, "_framework", return_value=av):
                with self.assertRaisesRegex(RuntimeError, "Privacy & Security"):
                    macos_devices.check_camera_permission()

    def test_not_determined_and_authorized_permission_allow_opencv_attempt(self):
        for status in (0, 3):
            av = SimpleNamespace(AVCaptureDevice=SimpleNamespace(authorizationStatusForMediaType_=lambda _: status),
                                 AVMediaTypeVideo="video", AVAuthorizationStatusDenied=2, AVAuthorizationStatusRestricted=1)
            with patch.object(macos_devices, "_framework", return_value=av):
                macos_devices.check_camera_permission()

    def test_opencv_uses_avfoundation_and_checks_permission_before_capture(self):
        cv = SimpleNamespace(CAP_DSHOW=700, CAP_AVFOUNDATION=1200, CAP_ANY=0, VideoCapture=Mock())
        with patch.object(macos_devices, "check_camera_permission"):
            open_camera(cv, 2)
        cv.VideoCapture.assert_called_once_with(2, 1200)
        cv.VideoCapture.reset_mock()
        with patch.object(macos_devices, "check_camera_permission", side_effect=RuntimeError("denied")):
            with self.assertRaises(RuntimeError):
                open_camera(cv, 2)
        cv.VideoCapture.assert_not_called()

    def test_coreaudio_names_and_direction_are_listed_without_windows_filter(self):
        fake = SimpleNamespace(query_hostapis=lambda: [{"name": "Core Audio"}], query_devices=lambda: [
            {"name": "MacBook Microphone", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
            {"name": "MacBook Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}])
        with patch.object(devUI, "sd", fake):
            catalog = devUI._audio_catalog_snapshot()
        self.assertEqual(catalog["microphones"][0]["name"], "MacBook Microphone")
        self.assertEqual(catalog["speakers"][0]["name"], "MacBook Speakers")
        self.assertEqual(len(catalog["microphones"]), 1)

    def test_coreaudio_reordered_devices_resolve_without_wasapi(self):
        fake = SimpleNamespace(query_devices=lambda: [
            {"name": "MacBook Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
            {"name": "MacBook Microphone", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0}],
            query_hostapis=lambda: [{"name": "Core Audio"}], default=SimpleNamespace(device=(-1, -1)),
            check_input_settings=Mock(), check_output_settings=Mock(), WasapiSettings=Mock())
        values = {"BAYMAX_MIC_DEVICE": "0", "BAYMAX_MIC_NAME": "MacBook Microphone", "BAYMAX_MIC_HOSTAPI": "Core Audio",
                  "BAYMAX_SPEAKER_DEVICE": "1", "BAYMAX_SPEAKER_NAME": "MacBook Speakers", "BAYMAX_SPEAKER_HOSTAPI": "Core Audio"}
        with patch.dict(os.environ, values):
            self.assertEqual(select_audio_devices(fake), (1, 0))
        configure_audio_backend(fake, 1, 0)
        fake.WasapiSettings.assert_not_called()
        self.assertEqual(fake.check_input_settings.call_args.kwargs["samplerate"], 48000)
