"""'Look closely': one sharper still from the shared camera frames; fake frames and session."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np

from features.toolkits import fall_detection, gemini_tools, look_closely
from features.toolkits.look_closely import Camera


def sharp_frame(width=640, height=480):
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)  # Busy texture: sharp, bright.
    cv2.putText(frame, "EXP 03/2027", (40, 240), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 4)
    return frame


class LookCloselyTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.camera = Camera(clock=lambda: self.now)

    def test_full_size_high_quality_photo(self):
        frame = sharp_frame()
        self.camera.observe(frame)
        jpeg, result = self.camera.photo()
        self.assertEqual(result["status"], "sent")
        self.assertIn("640x480", result["photo"])
        self.assertNotIn("hints", result)
        decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (480, 640, 3))  # Not the 320x240 of the 3 s frames.
        regular = cv2.imencode(".jpg", cv2.resize(frame, (320, 240)), [int(cv2.IMWRITE_JPEG_QUALITY), 50])[1]
        self.assertGreater(len(jpeg), 3 * len(regular))

    def test_large_captures_are_scaled_down(self):
        self.camera.observe(sharp_frame(1920, 1080))
        jpeg, result = self.camera.photo()
        self.assertIn("1280x720", result["photo"])
        self.assertEqual(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape[:2], (720, 1280))

    def test_blurry_and_dark_hints(self):
        self.camera.observe(np.full((480, 640, 3), 128, dtype=np.uint8))
        _, result = self.camera.photo()
        self.assertTrue(any(h.startswith("blurry") for h in result["hints"]))
        self.camera.observe(np.full((480, 640, 3), 10, dtype=np.uint8))
        _, result = self.camera.photo()
        self.assertTrue(any(h.startswith("dark") for h in result["hints"]))

    def test_no_frame_and_stale_frame_are_errors(self):
        jpeg, result = self.camera.photo()
        self.assertIsNone(jpeg)
        self.assertIn("No camera picture", result["error"])
        self.camera.observe(sharp_frame())
        self.now += look_closely.STALE_SECONDS + 0.1
        jpeg, result = self.camera.photo()
        self.assertIsNone(jpeg)
        self.assertIn("stopped", result["error"])

    def test_run_sends_one_jpeg_on_the_realtime_video_input(self):
        self.camera.observe(sharp_frame())
        session = SimpleNamespace(send_realtime_input=AsyncMock())
        with patch.object(look_closely, "camera", self.camera):
            result = asyncio.run(look_closely.run(session))
        self.assertEqual(result["status"], "sent")
        blob = session.send_realtime_input.call_args.kwargs["video"]
        self.assertEqual(blob.mime_type, "image/jpeg")
        self.assertTrue(blob.data.startswith(b"\xff\xd8"))

    def test_run_without_frame_or_failed_send(self):
        session = SimpleNamespace(send_realtime_input=AsyncMock())
        with patch.object(look_closely, "camera", self.camera):
            self.assertIn("error", asyncio.run(look_closely.run(session)))
            session.send_realtime_input.assert_not_awaited()
            self.camera.observe(sharp_frame())
            session.send_realtime_input.side_effect = RuntimeError("closed")
            self.assertIn("could not be sent", asyncio.run(look_closely.run(session))["error"])

    def test_tool_call_sends_photo_before_the_tool_response(self):
        self.camera.observe(sharp_frame())
        order = []
        session = SimpleNamespace(
            send_realtime_input=AsyncMock(side_effect=lambda **kw: order.append("photo")),
            send_tool_response=AsyncMock(side_effect=lambda **kw: order.append("response")))
        call = SimpleNamespace(id="c1", name="look_closely", args={})
        with patch.object(look_closely, "camera", self.camera), \
                patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LOOK_CLOSELY": "1"}):
            asyncio.run(gemini_tools.handle_tool_call(session, SimpleNamespace(function_calls=[call])))
        self.assertEqual(order, ["photo", "response"])
        reply = session.send_tool_response.call_args.kwargs["function_responses"][0]
        self.assertEqual((reply.id, reply.name, reply.response["status"]), ("c1", "look_closely", "sent"))

    def test_disabled_tool_sends_nothing(self):
        self.camera.observe(sharp_frame())
        session = SimpleNamespace(send_realtime_input=AsyncMock(), send_tool_response=AsyncMock())
        call = SimpleNamespace(id="c1", name="look_closely", args={})
        with patch.object(look_closely, "camera", self.camera), \
                patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LOOK_CLOSELY": "0"}):
            asyncio.run(gemini_tools.handle_tool_call(session, SimpleNamespace(function_calls=[call])))
        session.send_realtime_input.assert_not_awaited()
        self.assertIn("error", session.send_tool_response.call_args.kwargs["function_responses"][0].response)

    def test_declared_with_instruction_and_disable_switch(self):
        base = {"system_instruction": "base", "tools": []}
        with patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LOOK_CLOSELY": "1"}):
            config = gemini_tools.extend_config(base)
        names = [d["name"] for t in config["tools"] for d in t.get("function_declarations", [])]
        self.assertEqual(names.count("look_closely"), 1)
        self.assertIn("[LOOKING CLOSELY]", config["system_instruction"])
        self.assertIn("pharmacist", look_closely.INSTRUCTION)
        with patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LOOK_CLOSELY": "0"}):
            config = gemini_tools.extend_config(base)
        names = [d["name"] for t in config["tools"] for d in t.get("function_declarations", [])]
        self.assertNotIn("look_closely", names)
        self.assertNotIn("[LOOKING CLOSELY]", config["system_instruction"])

    def test_declaration_is_valid_for_the_sdk(self):
        from google.genai import types
        types.LiveConnectConfig(tools=[{"function_declarations": [look_closely.DECLARATION]}])
        types.LiveClientRealtimeInput(video=types.Blob(data=b"\xff\xd8", mime_type="image/jpeg"))

    def test_registered_on_existing_camera_frames(self):
        self.assertIn(look_closely.camera.observe, fall_detection.frame_observers)


if __name__ == "__main__":
    unittest.main()
