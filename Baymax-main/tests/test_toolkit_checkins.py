"""Gentle daily check-ins: fake clocks, session and state file; no devices or Gemini."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import runtime_monitor as monitor
from features.toolkits import checkins, fall_detection
from features.toolkits.checkins import CheckIns, window, windows


class CheckInTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "toolkit_checkins.json"
        self.local = datetime(2026, 11, 2, 9, 30)   # Morning window.
        self.up = 0.0
        self.wall = 100000.0
        self.talked = self.wall - 20 * 60          # Talked 20 minutes ago.
        self.book = False
        self.ember = CheckIns(path=self.path, now=lambda: self.local, clock=lambda: self.up,
                              wall=lambda: self.wall, last_conversation=lambda: self.talked,
                              book_playing=lambda: self.book)
        self.ember.started = 0.0
        self.up = 30 * 60
        for patcher in (patch.object(monitor, "state", "listening"),
                        patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_CHECKINS": "1"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_morning_check_in_when_quiet_and_someone_is_around(self):
        name, note = self.ember.due()
        self.assertEqual(name, "morning")
        self.assertIn("how they slept", note)
        self.assertIn("no medical advice", note)
        self.assertIn("do not ask again", note)

    def test_never_soon_after_boot_or_before_the_first_session(self):
        self.up = 19 * 60
        self.assertIsNone(self.ember.due())
        self.ember.started = None
        self.up = 10 ** 6
        self.assertIsNone(self.ember.due())

    def test_only_inside_a_window(self):
        for hour, expected in ((7, None), (12, None), (15, "afternoon"), (19, "evening"), (22, None)):
            self.local = datetime(2026, 11, 2, hour, 0)
            due = self.ember.due()
            self.assertEqual(due and due[0], expected, hour)
        self.local = datetime(2026, 11, 2, 15, 0)
        self.assertIn("water", self.ember.due()[1])

    def test_not_mid_conversation_and_not_to_an_empty_room(self):
        self.talked = self.wall - 60                # Talking a minute ago.
        self.assertIsNone(self.ember.due())
        self.talked = self.wall - 4 * 3600          # Nobody for four hours.
        self.assertIsNone(self.ember.due())
        self.talked = 0.0                           # Nobody since boot.
        self.assertIsNone(self.ember.due())

    def test_not_while_busy_or_reading_a_book(self):
        for state in ("speaking", "thinking", "calling_tool"):
            with patch.object(monitor, "state", state):
                self.assertIsNone(self.ember.due(), state)
        self.book = True
        self.assertIsNone(self.ember.due())

    def test_disabled_and_windows_turned_off(self):
        with patch.dict("os.environ", {"BAYMAX_CHECKINS": "0"}):
            self.assertIsNone(self.ember.due())
        with patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "0"}):
            self.assertIsNone(self.ember.due())
        with patch.dict("os.environ", {"BAYMAX_CHECKIN_MORNING": "0"}):
            self.assertIsNone(self.ember.due())

    def test_window_parsing(self):
        self.assertEqual(window("08:00-11:00"), (480, 660))
        self.assertEqual(window(" 7:30 - 9:15 "), (450, 555))
        for bad in ("", "0", "11:00-08:00", "25:00-26:00", "morning", None):
            self.assertIsNone(window(bad), bad)
        self.assertEqual(set(windows({})), {"morning", "afternoon", "evening"})
        self.assertEqual(windows({"BAYMAX_CHECKIN_EVENING": "17:00-19:00", "BAYMAX_CHECKIN_AFTERNOON": "off"}),
                         {"morning": (480, 660), "evening": (1020, 1140)})

    def test_one_attempt_per_window_kept_across_restarts_and_reset_next_day(self):
        session = AsyncMock()
        self.run_task(session)
        session.send_client_content.assert_awaited_once()
        self.assertEqual(json.loads(self.path.read_text()), {"date": "2026-11-02", "done": ["morning"]})
        self.assertIsNone(self.ember.due())         # Unanswered: not asked again.
        restarted = CheckIns(path=self.path, now=lambda: self.local, clock=lambda: self.up,
                             wall=lambda: self.wall, last_conversation=lambda: self.talked,
                             book_playing=lambda: False)
        restarted.started = 0.0
        self.assertIsNone(restarted.due())
        self.local = datetime(2026, 11, 2, 15, 0)
        self.assertEqual(restarted.due()[0], "afternoon")
        self.local = datetime(2026, 11, 3, 9, 0)
        self.assertEqual(restarted.due()[0], "morning")

    def test_failed_send_is_retried_and_unreadable_state_is_ignored(self):
        self.path.write_text("not json")
        self.assertEqual(self.ember.due()[0], "morning")
        session = AsyncMock()
        session.send_client_content.side_effect = RuntimeError("offline")
        self.run_task(session)
        self.assertEqual(self.ember.due()[0], "morning")

    def test_first_session_starts_the_uptime_clock_and_reconnects_keep_it(self):
        self.ember.started = None
        self.up = 5.0
        self.run_task(AsyncMock())
        self.assertEqual(self.ember.started, 5.0)
        self.up = 50.0
        self.run_task(AsyncMock())
        self.assertEqual(self.ember.started, 5.0)

    def run_task(self, session):
        calls = []

        async def sleep(seconds):
            calls.append(seconds)
            shutdown.set()
        shutdown = threading.Event()
        self.ember.sleep = sleep
        with patch.object(monitor, "event_observer", None):
            asyncio.run(self.ember.session_task(session, shutdown))
        self.assertEqual(calls, [30])

    def test_registered_on_the_live_session(self):
        import features.toolkits.gemini_tools  # noqa: F401
        self.assertIn(checkins.checkins.session_task, fall_detection.session_tasks)


if __name__ == "__main__":
    unittest.main()
