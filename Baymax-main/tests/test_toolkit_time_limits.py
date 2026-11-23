import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from features.toolkits import experimental, gemini_tools, time_limits


class TimeLimitTests(unittest.TestCase):
    def test_per_tool_values(self):
        self.assertEqual(time_limits.limit_for("get_weather"), 8)
        self.assertEqual(time_limits.limit_for("get_location"), 8)
        self.assertEqual(time_limits.limit_for("public_knowledge"), 12)
        self.assertEqual(time_limits.limit_for("audiobook"), 15)
        self.assertEqual(time_limits.limit_for("reminder"), 5)
        self.assertEqual(time_limits.limit_for("public_trivia"), time_limits.PUBLIC_DEFAULT)
        self.assertEqual(time_limits.limit_for("something_new"), time_limits.DEFAULT)

    def test_slow_tool_times_out_and_late_result_is_dropped(self):
        release = threading.Event()
        finished = threading.Event()

        def slow():
            release.wait(2)
            finished.set()
            return {"late": True}

        async def go():
            with mock.patch.dict(time_limits.LIMITS, {"slow_tool": 0.05}):
                result = await time_limits.run("slow_tool", slow)
            release.set()
            await asyncio.sleep(0.1)  # Let the late result try to land.
            return result

        with self.assertLogs(time_limits.log, "WARNING"):
            result = asyncio.run(go())
        self.assertEqual(result["error"], "timed_out")
        self.assertIn("try again", result["message"])
        self.assertTrue(finished.wait(1))

    def test_fast_tool_unaffected_and_errors_propagate(self):
        self.assertEqual(asyncio.run(time_limits.run("get_weather", lambda: {"ok": 1})), {"ok": 1})
        with self.assertRaises(ValueError):
            asyncio.run(time_limits.run("get_weather", lambda: (_ for _ in ()).throw(ValueError())))

    def test_hung_tool_does_not_block_shutdown(self):
        start = time.monotonic()
        with mock.patch.dict(time_limits.LIMITS, {"hung": 0.05}):
            asyncio.run(time_limits.run("hung", time.sleep, 5))
        self.assertLess(time.monotonic() - start, 2)

    def test_handle_tool_call_sends_only_timeout_result(self):
        sent = []

        class Session:
            async def send_tool_response(self, function_responses):
                sent.append(function_responses)

        call = SimpleNamespace(id="1", name="get_weather", args={})
        slow = lambda name, args: (time.sleep(0.5), {"late": True})[1]
        with mock.patch.object(gemini_tools, "execute", slow), \
                mock.patch.dict(time_limits.LIMITS, {"get_weather": 0.05}), \
                self.assertLogs(time_limits.log, "WARNING"):
            asyncio.run(gemini_tools.handle_tool_call(Session(), SimpleNamespace(function_calls=[call])))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0].response["error"], "timed_out")

    def test_experimental_uses_same_limits(self):
        call = SimpleNamespace(name="public_trivia", args={})
        slow = lambda name, args: (time.sleep(0.5), {})[1]
        with mock.patch.object(experimental.toolkit, "execute", slow), \
                mock.patch.object(time_limits, "PUBLIC_DEFAULT", 0.05), \
                self.assertLogs(time_limits.log, "WARNING"):
            self.assertEqual(asyncio.run(experimental.run_call(call))["error"], "timed_out")


if __name__ == "__main__":
    unittest.main()
