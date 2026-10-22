import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from pc_hardware import select_audio_devices


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
        runtime = runtime.replace('    from toolkit.gemini_tools import extend_config\n    return extend_config({**_BASE_CONFIG, "system_instruction": system_instruction})',
                                  '    return {**_BASE_CONFIG, "system_instruction": system_instruction}')
        runtime = runtime.replace('                if response.tool_call:\n                    from toolkit.gemini_tools import handle_tool_call\n                    await handle_tool_call(session, response.tool_call)\n', '')
        runtime = runtime.replace('    from toolkit.gemini_tools import prewarm\n    prewarm()\n', '')
        class RemoveMonitorHooks(ast.NodeTransformer):
            def visit_Expr(self, node):
                if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == 'monitor'):
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

    def test_missing_default_uses_an_available_input(self):
        audio = FakeAudio()
        audio.default = type("Defaults", (), {"device": (-1, 1)})
        with patch.dict(os.environ, {"BAYMAX_MIC_DEVICE": "", "BAYMAX_SPEAKER_DEVICE": ""}):
            self.assertEqual(select_audio_devices(audio), (0, 1))


if __name__ == "__main__":
    unittest.main()
