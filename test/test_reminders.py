"""
Tests for Baymax-main/reminders.py — uses a fake clock and a temp file.

    python3 -m unittest test/test_reminders.py -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))
import reminders as rm  # noqa: E402

BASE = dt.datetime(2026, 10, 6, 15, 0)   # Tuesday 3:00 PM local


class Clock:
    def __init__(self, at=BASE):
        self.t = at.timestamp()

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60

    def local(self):
        return dt.datetime.fromtimestamp(self.t)


# ─── Time parsing ────────────────────────────────────────────────────────────
class ParseTimeTests(unittest.TestCase):
    def test_valid_formats(self):
        cases = {
            "5:00 pm": (17, 0, True), "5 PM": (17, 0, True), "5pm": (17, 0, True),
            "5:30pm": (17, 30, True), "9am": (9, 0, True), "9:05 a.m.": (9, 5, True),
            "12am": (0, 0, True), "12 pm": (12, 0, True), "noon": (12, 0, True),
            "midnight": (0, 0, True), "17:30": (17, 30, False), "5:00": (5, 0, False),
            "5 o'clock": (5, 0, False), "08.15": (8, 15, False),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(rm.parse_clock_time(text), expected)

    def test_invalid(self):
        for text in ["25:00", "13pm", "5:75", "soon", "", None]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    rm.parse_clock_time(text)


class ResolveDueTests(unittest.TestCase):
    def at(self, hour, minute=0, day=6):
        return dt.datetime(2026, 10, day, hour, minute)

    def test_in_one_hour(self):
        self.assertEqual(rm.resolve_due(BASE, minutes_from_now=60), self.at(16))

    def test_at_5pm_later_today(self):
        self.assertEqual(rm.resolve_due(BASE, time_text="5:00 pm"), self.at(17))

    def test_at_5pm_already_passed_means_tomorrow(self):
        evening = self.at(18)
        self.assertEqual(rm.resolve_due(evening, time_text="5:00 pm"), self.at(17, day=7))

    def test_ambiguous_5_oclock_picks_next_occurrence(self):
        self.assertEqual(rm.resolve_due(BASE, time_text="5:00"), self.at(17))
        self.assertEqual(rm.resolve_due(self.at(18), time_text="5"), self.at(5, day=7))
        self.assertEqual(rm.resolve_due(self.at(3), time_text="5"), self.at(5))

    def test_tomorrow_morning(self):
        self.assertEqual(rm.resolve_due(BASE, time_text="9am", days_from_now=1),
                         self.at(9, day=7))

    def test_today_at_a_past_time_is_rejected(self):
        with self.assertRaises(ValueError):
            rm.resolve_due(BASE, time_text="9am", days_from_now=0)

    def test_bad_arguments(self):
        for kwargs in ({}, {"minutes_from_now": 0}, {"minutes_from_now": -5},
                       {"minutes_from_now": 60 * 24 * 8}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    rm.resolve_due(BASE, **kwargs)

    def test_describe(self):
        self.assertEqual(rm.describe_due(self.at(17), BASE), "5:00 PM today")
        self.assertEqual(rm.describe_due(self.at(9, 5, day=7), BASE), "tomorrow at 9:05 AM")
        self.assertEqual(rm.describe_due(self.at(0, 0, day=9), BASE), "Friday, Oct 9 at 12:00 AM")


# ─── Manager ─────────────────────────────────────────────────────────────────
class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "reminders.json")
        self.clock = Clock()
        self.events = []
        self.mgr = self.new_manager()

    def tearDown(self):
        self.dir.cleanup()

    def new_manager(self):
        return rm.ReminderManager(path=self.path, clock=self.clock,
                                  on_event=self.events.append)

    def call(self, name, **args):
        return self.mgr.handle_tool_call(name, args)

    def announce(self):
        due = self.mgr.due()
        prompt = self.mgr.prompt_for(due) if due else None
        if due:
            self.mgr.mark_announced(due)
        return due, prompt

    # ── The user's scenarios ──
    def test_remind_me_in_one_hour_to_take_medication(self):
        result = self.call("set_reminder", task="take your medication", minutes_from_now=60)
        self.assertEqual(result["status"], "set")
        self.assertEqual(result["when"], "4:00 PM today")

        self.clock.advance(59)
        self.assertEqual(self.mgr.due(), [])
        self.clock.advance(1)
        due, prompt = self.announce()
        self.assertEqual([r["task"] for r in due], ["take your medication"])
        self.assertIn("[REMINDER]", prompt)
        self.assertIn("take your medication", prompt)
        self.assertIn("ask whether they've done it", prompt)

        self.mgr.user_spoke()
        result = self.call("record_reminder_response", task="medication", done=True)
        self.assertEqual(result, {"status": "recorded", "task": "take your medication",
                                  "done": True})
        self.clock.advance(30)
        self.assertEqual(self.mgr.due(), [])
        self.assertIn("[Reminder response] take your medication — done", self.events)

    def test_remind_me_at_5pm(self):
        result = self.call("set_reminder", task="take your medication", time="5:00 pm")
        self.assertEqual(result["when"], "5:00 PM today")
        self.clock.advance(119)
        self.assertEqual(self.mgr.due(), [])
        self.clock.advance(1)
        self.assertEqual(len(self.mgr.due()), 1)

    # ── Follow-ups ──
    def test_repeats_when_user_says_nothing_then_gives_up(self):
        self.call("set_reminder", task="take your pills", minutes_from_now=1)
        self.clock.advance(1)
        self.announce()
        for attempt in range(rm.MAX_RETRIES):
            self.clock.advance(4)
            self.assertEqual(self.mgr.due(), [], "too early to repeat")
            self.clock.advance(1)
            due, prompt = self.announce()
            self.assertEqual(len(due), 1)
            self.assertIn("haven't answered", prompt)
        self.clock.advance(5)
        self.assertEqual(self.mgr.due(), [])
        self.assertIn("[Reminder] No response to: take your pills", self.events)

    def test_no_repeat_if_user_answered_but_gemini_did_not_record(self):
        self.call("set_reminder", task="take your pills", minutes_from_now=1)
        self.clock.advance(1)
        self.announce()
        self.clock.advance(0.5)
        self.mgr.user_spoke()
        self.clock.advance(10)
        self.assertEqual(self.mgr.due(), [])
        self.assertEqual(self.mgr._reminders[0]["status"], "acknowledged")

    def test_not_done_is_recorded(self):
        self.call("set_reminder", task="take your medication", minutes_from_now=5)
        self.clock.advance(5)
        self.announce()
        result = self.call("record_reminder_response", done=False)
        self.assertEqual(result["done"], False)
        self.assertIn("[Reminder response] take your medication — NOT done", self.events)

    def test_record_without_recent_reminder(self):
        self.assertEqual(self.call("record_reminder_response", done=True)["status"],
                         "no_recent_reminder")

    def test_two_due_together_share_one_prompt(self):
        self.call("set_reminder", task="take your medication", minutes_from_now=10)
        self.call("set_reminder", task="drink some water", minutes_from_now=10)
        self.clock.advance(10)
        due, prompt = self.announce()
        self.assertEqual(len(due), 2)
        self.assertIn("take your medication; drink some water", prompt)

    def test_daily_reminder_reschedules(self):
        result = self.call("set_reminder", task="take your medication", time="8am",
                           repeat_daily=True)
        self.assertEqual(result["when"], "tomorrow at 8:00 AM")
        self.clock.advance(17 * 60)                     # 8:00 AM tomorrow
        self.announce()
        upcoming = self.call("list_reminders")["reminders"]
        self.assertEqual(upcoming, [{"task": "take your medication",
                                     "when": "tomorrow at 8:00 AM", "repeats_daily": True}])

    # ── Listing / cancelling ──
    def test_list_and_cancel(self):
        self.call("set_reminder", task="take your medication", minutes_from_now=60)
        self.call("set_reminder", task="call Sarah", time="6pm")
        listing = self.call("list_reminders")
        self.assertEqual(listing["current_time"], "3:00 PM today")
        self.assertEqual([r["task"] for r in listing["reminders"]],
                         ["take your medication", "call Sarah"])
        self.assertEqual(self.call("cancel_reminder", task="meds")["status"], "cancelled")
        self.assertEqual([r["task"] for r in self.call("list_reminders")["reminders"]],
                         ["call Sarah"])
        self.assertEqual(self.call("cancel_reminder", task="dentist")["status"], "not_found")
        self.clock.advance(61)
        self.assertEqual(self.mgr.due(), [])           # cancelled one never fires

    # ── Errors ──
    def test_tool_errors_are_returned_not_raised(self):
        self.assertIn("error", self.call("set_reminder", task="x"))
        self.assertIn("error", self.call("set_reminder", task="", minutes_from_now=5))
        self.assertIn("error", self.call("set_reminder", task="x", time="whenever"))
        self.assertIn("error", self.call("nonexistent_tool"))

    # ── Persistence / restarts ──
    def test_survives_restart(self):
        self.call("set_reminder", task="take your medication", minutes_from_now=60)
        self.mgr = self.new_manager()
        self.clock.advance(60)
        self.assertEqual(len(self.mgr.due()), 1)

    def test_recently_overdue_still_fires_after_restart(self):
        self.call("set_reminder", task="take your medication", minutes_from_now=10)
        self.clock.advance(40)                          # robot was off for a bit
        self.mgr = self.new_manager()
        self.assertEqual(len(self.mgr.due()), 1)

    def test_long_overdue_is_missed_and_daily_rolls_forward(self):
        self.call("set_reminder", task="take your medication", minutes_from_now=10)
        self.call("set_reminder", task="vitamins", time="3:30 pm", repeat_daily=True)
        self.clock.advance(5 * 60)                      # off for 5 hours → 8:00 PM
        self.mgr = self.new_manager()
        self.assertEqual(self.mgr.due(), [])
        with open(self.path) as f:
            saved = json.load(f)
        statuses = {r["task"]: r["status"] for r in saved if r["status"] != "pending"}
        self.assertEqual(statuses, {"take your medication": "missed", "vitamins": "missed"})
        self.assertEqual(self.call("list_reminders")["reminders"],
                         [{"task": "vitamins", "when": "tomorrow at 3:30 PM",
                           "repeats_daily": True}])

    def test_corrupt_file_starts_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.mgr = self.new_manager()
        self.assertEqual(self.call("list_reminders")["reminders"], [])


if __name__ == "__main__":
    unittest.main()
