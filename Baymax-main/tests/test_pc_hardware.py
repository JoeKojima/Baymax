import ast
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pc_hardware import camera_candidates, open_camera, select_audio_devices


ROOT = Path(__file__).resolve().parents[1]


class ParityTests(unittest.TestCase):
    def test_shared_runtime_functions_are_unchanged(self):
        original = (ROOT / "upstream/realtime_gemini_8.py").read_text(encoding="utf-8")
        runtime = (ROOT / "realtime_gemini_8.py").read_text(encoding="utf-8")
        camera_adapter = ('    from pc_hardware import camera_candidates, open_camera\n'
                          '    for cam_idx in (camera_candidates() if os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc" else [0, 1, 2]):\n'
                          '        test = open_camera(cv2, cam_idx) if os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc" else cv2.VideoCapture(cam_idx)')
        self.assertIn(camera_adapter, runtime)
        runtime = runtime.replace(camera_adapter, '    for cam_idx in [0, 1, 2]:\n        test = cv2.VideoCapture(cam_idx)')
        # Remove only the three explicit toolkit hooks; all original processing
        # functions and the base prompt/models still must match upstream.
        runtime = runtime.replace('    from features.toolkits.gemini_tools import extend_config\n    return extend_config({**_BASE_CONFIG, "system_instruction": system_instruction})',
                                  '    return {**_BASE_CONFIG, "system_instruction": system_instruction}')
        runtime = runtime.replace('                if response.tool_call:\n                    from features.toolkits.gemini_tools import handle_tool_call\n                    await handle_tool_call(session, response.tool_call)\n', '')
        # Session resumption/GoAway hook (features/conversational_ability/session.py).
        runtime = runtime.replace('                if response.session_resumption_update or response.go_away:\n                    from features.conversational_ability.session import on_server_message\n                    on_server_message(response)\n', '')
        runtime = runtime.replace('    from features.toolkits.gemini_tools import prewarm\n    prewarm()\n', '')
        # Normalize only the documented optional fall hooks; inference stays outside the core.
        alert_hook = '                        tg.create_task(fall_worker.alert_monitor(live_session, _shutdown_event))\n'
        self.assertEqual(runtime.count(alert_hook), 1)
        runtime = runtime.replace(alert_hook, '')
        camera_message = 'f"— fall detection: {fall_worker.status()[\'state\']}."'
        self.assertIn(camera_message, runtime)
        runtime = runtime.replace(camera_message, 'f"— fall detection disabled."')
        runtime = runtime.replace('print("Fall detection:   " + fall_worker.status()["state"])',
                                  'print("Fall detection:   DISABLED (demo latency) — camera feed only")')
        # Normalize only guarded conversational ability/waiting-cue hooks; the disabled
        # audio path must still recover the original upstream functions exactly.
        runtime = runtime.replace('if not buffer_empty and not (_audio_enhancer is not None and _audio_enhancer.full_duplex):', 'if not buffer_empty:')
        class RemoveMonitorHooks(ast.NodeTransformer):
            def visit_If(self, node):
                if any(isinstance(n, ast.Name) and n.id in {'_audio_enhancer', '_waiting_cue'} for n in ast.walk(node.test)):
                    return None
                return self.generic_visit(node)

            def visit_Expr(self, node):
                if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and (node.value.func.value.id == 'monitor' or
                         (node.value.func.value.id == 'fall_worker' and node.value.func.attr in {'start', 'submit', 'stop'}))):
                    return None
                return self.generic_visit(node)
        trees = [ast.parse(original), RemoveMonitorHooks().visit(ast.parse(runtime))]
        functions = [{node.name: ast.dump(node, include_attributes=False) for node in tree.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                     for tree in trees]
        self.assertEqual(set(functions[0]), set(functions[1]))
        for name in functions[0]:
            if name != "get_default_device_id":
                self.assertEqual(functions[0][name], functions[1][name], name)

    def test_prompt_models_and_sample_rates_match_upstream(self):
        names = {"MODEL", "SUMMARY_MODEL", "_BASE_SYSTEM_INSTRUCTION", "SEND_SAMPLE_RATE", "RECEIVE_SAMPLE_RATE"}
        values = []
        for file in (ROOT / "upstream/realtime_gemini_8.py", ROOT / "realtime_gemini_8.py"):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            values.append({target.id: ast.dump(node.value) for node in tree.body if isinstance(node, ast.Assign)
                           for target in node.targets if isinstance(target, ast.Name) and target.id in names})
        self.assertEqual(set(values[0]), names)
        self.assertEqual(values[0], values[1])


class FakeAudio:
    class default:
        device = (0, 1)

    def query_devices(self):
        return [{"max_input_channels": 1, "max_output_channels": 0},
                {"max_input_channels": 0, "max_output_channels": 2}]

    def check_input_settings(self, **kwargs):
        if kwargs["channels"] != 1:
            raise ValueError("Microphone supports mono only")

    def check_output_settings(self, **kwargs):
        pass


class HardwareTests(unittest.TestCase):
    def test_default_mono_mic_and_output_work(self):
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": ""}):
            self.assertEqual(select_audio_devices(FakeAudio()), (0, 1))

    def test_invalid_or_wrong_direction_device_is_rejected(self):
        for index in ("1", "99", "-1", "unknown"):
            with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": index, "BAYMAX_SPEAKER_DEVICE": ""}):
                with self.assertRaises(RuntimeError):
                    select_audio_devices(FakeAudio())

    def test_saved_alsa_device_is_found_after_card_renumbering(self):
        class Renumbered(FakeAudio):
            def query_hostapis(self):
                return [{"name": "ALSA"}]
            def query_devices(self):
                return [{"name": "USB Mic: Audio (hw:3,0)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
                        {"name": "HDA Intel PCH: ALC269 (hw:0,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
                        {"name": "UACDemoV1.0: USB Audio (hw:1,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}]
        saved = {"BAYMAX_MIC_DEVICE": "0", "BAYMAX_MIC_NAME": "USB Mic: Audio (hw:2,0)", "BAYMAX_MIC_HOSTAPI": "ALSA",
                 "BAYMAX_SPEAKER_DEVICE": "1", "BAYMAX_SPEAKER_NAME": "UACDemoV1.0: USB Audio (hw:2,0)",
                 "BAYMAX_SPEAKER_HOSTAPI": "ALSA"}
        with patch.dict(os.environ, saved):
            self.assertEqual(select_audio_devices(Renumbered()), (0, 2))
        with patch.dict(os.environ, {**saved, "BAYMAX_SPEAKER_NAME": "Other Speaker (hw:2,0)"}):
            with self.assertRaises(RuntimeError):
                select_audio_devices(Renumbered())

    def test_ambiguous_renumbered_alsa_device_is_still_rejected(self):
        class Twins(FakeAudio):
            def query_hostapis(self):
                return [{"name": "ALSA"}]
            def query_devices(self):
                return [{"name": "Mic (hw:0,0)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
                        {"name": "Speaker (hw:1,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
                        {"name": "Speaker (hw:3,0)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}]
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": "1",
                                     "BAYMAX_SPEAKER_NAME": "Speaker (hw:2,0)", "BAYMAX_SPEAKER_HOSTAPI": "ALSA"}):
            with self.assertRaises(RuntimeError):
                select_audio_devices(Twins())

    def test_linux_camera_accepts_stable_udev_path(self):
        path = "/dev/v4l/by-id/usb-Arducam_Technology_Co.__Ltd._Arducam-1080P-HDR_SN0001-video-index0"
        cv2 = SimpleNamespace(CAP_ANY=0, CAP_V4L2=200, VideoCapture=Mock(return_value="capture"))
        with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"BAYMAX_CAMERA_DEVICE": path}):
            self.assertEqual(camera_candidates(), [path])
            self.assertEqual(open_camera(cv2, path), "capture")
            cv2.VideoCapture.assert_called_with(path, 200)
            open_camera(cv2, 0)
            cv2.VideoCapture.assert_called_with(0, 0)

    def test_missing_default_uses_an_available_input(self):
        audio = FakeAudio()
        audio.default = type("Defaults", (), {"device": (-1, 1)})
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": ""}):
            self.assertEqual(select_audio_devices(audio), (0, 1))


if __name__ == "__main__":
    unittest.main()
