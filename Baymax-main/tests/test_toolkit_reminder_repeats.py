"""Repeating reminders, check-ins, dashboard reminders and sync (offline, fake clock)."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from features.toolkits.everyday import recurrence, tools
from features.toolkits.everyday.store import Store

TZ = "America/New_York"
NY = ZoneInfo(TZ)


def ts(text):
    return datetime.fromisoformat(text).replace(tzinfo=NY).timestamp()


def local(value):
    return datetime.fromtimestamp(value, NY).strftime("%Y-%m-%d %a %H:%M")


def series(rule, start, after, n=5):
    out, t = [], ts(after)
    for _ in range(n):
        t = recurrence.next_after(recurrence.normalize(rule), start, TZ, t)
        if t is None:
            break
        out.append(local(t))
    return out


class RecurrenceTests(unittest.TestCase):
    def test_daily_and_every_other_day(self):
        self.assertEqual(series({"freq": "daily"}, "2026-10-22T08:00", "2026-10-22T09:00", 2),
                         ["2026-10-23 Fri 08:00", "2026-10-24 Sat 08:00"])
        self.assertEqual(series({"freq": "daily", "interval": 2}, "2026-10-22T08:00", "2026-10-21T00:00", 3),
                         ["2026-10-22 Thu 08:00", "2026-10-24 Sat 08:00", "2026-10-26 Mon 08:00"])

    def test_daily_keeps_local_time_across_daylight_saving(self):
        self.assertEqual(series({"freq": "daily"}, "2026-10-31T08:00", "2026-10-31T09:00", 2),
                         ["2026-11-01 Sun 08:00", "2026-11-02 Mon 08:00"])

    def test_weekly_days_and_every_two_weeks(self):
        self.assertEqual(series({"freq": "weekly", "weekdays": ["MO", "WE", "FR"]}, "2026-10-21T09:00", "2026-10-21T10:00", 4),
                         ["2026-10-23 Fri 09:00", "2026-10-26 Mon 09:00", "2026-10-28 Wed 09:00", "2026-10-30 Fri 09:00"])
        self.assertEqual(series({"freq": "weekly", "interval": 2, "weekdays": ["TU"]}, "2026-10-20T09:00", "2026-10-20T10:00", 2),
                         ["2026-11-03 Tue 09:00", "2026-11-17 Tue 09:00"])

    def test_monthly_each_day_skips_short_months_and_last_day(self):
        self.assertEqual(series({"freq": "monthly"}, "2027-01-31T10:00", "2027-01-31T11:00", 2),
                         ["2027-03-31 Wed 10:00", "2027-05-31 Mon 10:00"])
        self.assertEqual(series({"freq": "monthly", "month_days": [-1]}, "2027-01-31T10:00", "2027-01-31T11:00", 2),
                         ["2027-02-28 Sun 10:00", "2027-03-31 Wed 10:00"])
        self.assertEqual(series({"freq": "monthly", "month_days": [1, 15]}, "2026-10-01T10:00", "2026-10-02T00:00", 3),
                         ["2026-10-15 Thu 10:00", "2026-11-01 Sun 10:00", "2026-11-15 Sun 10:00"])

    def test_on_the_nth_weekday(self):
        self.assertEqual(series({"freq": "monthly", "nth": {"n": 2, "day": "TU"}}, "2026-10-13T18:00", "2026-10-14T00:00", 2),
                         ["2026-11-10 Tue 18:00", "2026-12-08 Tue 18:00"])
        self.assertEqual(series({"freq": "monthly", "nth": {"n": -1, "day": "weekday"}}, "2026-10-30T17:00", "2026-10-31T00:00", 2),
                         ["2026-11-30 Mon 17:00", "2026-12-31 Thu 17:00"])
        self.assertEqual(series({"freq": "yearly", "months": [11], "nth": {"n": 4, "day": "TH"}}, "2026-11-26T12:00", "2026-11-27T00:00", 1),
                         ["2027-11-25 Thu 12:00"])

    def test_yearly_and_hourly(self):
        self.assertEqual(series({"freq": "yearly", "months": [1, 7]}, "2027-01-15T09:00", "2027-01-16T00:00", 2),
                         ["2027-07-15 Thu 09:00", "2028-01-15 Sat 09:00"])
        self.assertEqual(series({"freq": "hourly", "interval": 6}, "2026-10-22T08:00", "2026-10-22T09:00", 3),
                         ["2026-10-22 Thu 14:00", "2026-10-22 Thu 20:00", "2026-10-23 Fri 02:00"])

    def test_end_repeat_and_one_time(self):
        self.assertEqual(series({"freq": "daily", "until": "2026-10-24"}, "2026-10-22T08:00", "2026-10-22T09:00"),
                         ["2026-10-23 Fri 08:00", "2026-10-24 Sat 08:00"])
        self.assertEqual(recurrence.next_after(None, "2026-10-22T08:00", TZ, ts("2026-10-21T00:00")), ts("2026-10-22T08:00"))
        self.assertIsNone(recurrence.next_after(None, "2026-10-22T08:00", TZ, ts("2026-10-22T08:00")))

    def test_invalid_rules_are_rejected(self):
        for rule in ({"freq": "minutely"}, {"freq": "daily", "interval": 0}, {"freq": "daily", "weekdays": ["MO"]},
                     {"freq": "weekly", "weekdays": ["XX"]}, {"freq": "monthly", "month_days": [32]},
                     {"freq": "monthly", "month_days": [1], "nth": {"n": 1, "day": "MO"}},
                     {"freq": "monthly", "nth": {"n": 6, "day": "MO"}}, {"freq": "daily", "until": "soon"},
                     {"freq": "daily", "extra": 1}, {"freq": "monthly", "month_days": [True]}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                recurrence.normalize(rule)

    def test_descriptions(self):
        d = lambda rule, start: recurrence.describe(recurrence.normalize(rule), start, TZ)
        self.assertEqual(d({"freq": "daily"}, "2026-10-22T08:00"), "every day at 8:00 AM")
        self.assertEqual(d({"freq": "weekly", "interval": 2, "weekdays": ["MO", "WE"]}, "2026-10-21T21:30"),
                         "every 2 weeks on Monday and Wednesday at 9:30 PM")
        self.assertEqual(d({"freq": "monthly", "nth": {"n": -1, "day": "FR"}}, "2026-10-30T10:00"),
                         "every month on the last Friday at 10:00 AM")
        self.assertEqual(d({"freq": "hourly", "interval": 6, "until": "2026-12-01"}, "2026-10-22T08:00"),
                         "every 6 hours from 8:00 AM, until Dec 01 2026")


class StoreRepeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = ts("2026-10-22T07:00")
        self.store = Store(Path(self.tmp.name) / "tasks.sqlite3", clock=lambda: self.now, tz=TZ)
        env = patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_EVERYDAY_ENABLED": "1"})
        env.start()
        self.addCleanup(env.stop)

    def deliver(self):
        due = self.store.due()
        self.store.delivered([r["occurrence"] for r in due])
        return due

    def test_voice_repeat_advances_and_logs_answers(self):
        saved = self.store.reminder("create", text="Take pills", due_at="2026-10-22T08:00:00-04:00",
                                    repeat={"freq": "daily"})
        self.assertEqual(saved["repeat"], "every day at 8:00 AM")
        self.now = ts("2026-10-22T08:00")
        due = self.deliver()
        self.assertEqual(due[0]["set_by"], "voice")
        self.assertEqual(local(self.store.reminder("list")["reminders"][0]["due_at"] and
                               datetime.fromisoformat(self.store.reminder("list")["reminders"][0]["due_at"]).timestamp()),
                         "2026-10-23 Fri 08:00")
        self.assertTrue(self.store.reminder("answer", occurrence=due[0]["occurrence"], outcome="done")["recorded"])
        self.assertFalse(self.store.reminder("answer", occurrence=due[0]["occurrence"], outcome="not_done")["recorded"])
        occ = self.store.changes()["occurrences"]
        self.assertEqual([(o["outcome"], local(o["due"])) for o in occ], [("done", "2026-10-22 Thu 08:00")])

    def test_unanswered_is_asked_once_more_then_logged_no_answer(self):
        self.store.reminder("create", text="Water plants", seconds=60)
        self.now += 60
        due = self.deliver()
        self.assertEqual(self.store.followups(), [])
        self.now += 600
        again = self.store.followups()
        self.assertEqual([r["occurrence"] for r in again], [due[0]["occurrence"]])
        self.store.reasked([due[0]["occurrence"]])
        self.assertEqual(self.store.followups(), [])
        self.now += 600
        self.assertEqual(self.store.followups(), [])
        self.assertEqual(self.store.changes()["occurrences"][0]["outcome"], "no_answer")
        self.assertEqual(self.store.changes()["reminders"][0]["state"], "announced")

    def test_missed_repeats_after_downtime_are_announced_once(self):
        self.store.reminder("create", text="Stretch", due_at="2026-10-22T08:00:00-04:00", repeat={"freq": "hourly"})
        self.now = ts("2026-10-22T12:30")
        due = self.deliver()
        self.assertEqual(len(due), 1)
        self.assertIn("was_due", due[0])
        self.assertEqual(self.store.due(), [])
        nxt = self.store.reminder("list")["reminders"][0]["due_at"]
        self.assertEqual(local(datetime.fromisoformat(nxt).timestamp()), "2026-10-22 Thu 13:00")

    def test_repeat_that_ends_is_marked_ended(self):
        self.store.reminder("create", text="Course", due_at="2026-10-22T08:00:00-04:00",
                            repeat={"freq": "daily", "until": "2026-10-22"})
        self.now = ts("2026-10-22T08:01")
        self.deliver()
        self.assertEqual(self.store.reminder("list")["reminders"], [])
        self.assertEqual(self.store.changes()["reminders"][0]["state"], "ended")

    def test_dashboard_reminders_are_idempotent_labelled_and_cancellable(self):
        rid = self.store.dashboard_create("c1", "Call Dr. Lee", "2026-10-22T09:30", None, "Jane")
        self.assertEqual(self.store.dashboard_create("c1", "Call Dr. Lee", "2026-10-22T09:30", None, "Jane"), rid)
        weekly = self.store.dashboard_create("c2", "Bins out", "2026-10-21T19:00", {"freq": "weekly", "weekdays": ["WE"]}, "Jane")
        listed = {r["id"]: r for r in self.store.reminder("list")["reminders"]}
        self.assertEqual(listed[rid]["set_by"], "dashboard")
        self.assertEqual(local(datetime.fromisoformat(listed[weekly]["due_at"]).timestamp()), "2026-10-28 Wed 19:00")
        voice = self.store.reminder("create", text="Nap", seconds=600)["id"]
        self.assertTrue(self.store.dashboard_cancel(id=voice))
        self.assertTrue(self.store.dashboard_cancel(cloud_id="c2"))
        self.assertFalse(self.store.dashboard_cancel(cloud_id="c2"))
        states = {r["robot_id"]: (r["state"], r["source"], r["created_by"]) for r in self.store.changes()["reminders"]}
        self.assertEqual(states, {rid: ("pending", "dashboard", "Jane"), weekly: ("cancelled", "dashboard", "Jane"),
                                  voice: ("cancelled", "voice", None)})
        with self.assertRaises(ValueError):
            self.store.dashboard_create("c3", "Past", "2026-10-21T09:00", None, "Jane")

    def test_sync_resends_only_changed_rows(self):
        rid = self.store.reminder("create", text="Tea", seconds=60)["id"]
        first = self.store.changes()
        self.store.synced([(r["robot_id"], r["rev"]) for r in first["reminders"]])
        self.assertEqual(self.store.changes()["reminders"], [])
        self.store.reminder("cancel", id=rid)
        again = self.store.changes()["reminders"]
        self.assertEqual([(r["robot_id"], r["state"]) for r in again], [(rid, "cancelled")])
        self.assertEqual(first["tz"], TZ)

    def test_database_from_before_repeats_is_upgraded(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending')")
        db.execute("INSERT INTO reminders(text,due) VALUES ('Old one', ?)", (self.now + 3600,))
        db.commit()
        db.close()
        old = Store(path, clock=lambda: self.now, tz=TZ)
        listed = old.reminder("list")["reminders"]
        self.assertEqual([(r["text"], r["set_by"]) for r in listed], [("Old one", "voice")])
        self.assertEqual(old.changes()["reminders"][0]["start"], "2026-10-22T08:00")

    def test_tool_schema_accepts_repeat_and_answer(self):
        with patch.object(tools, "store", self.store):
            saved = tools.execute("reminder", {"action": "create", "text": "Pills", "due_at": "2026-10-22T08:00:00-04:00",
                                               "repeat": {"freq": "weekly", "weekdays": ["MO", "TH"]}})
            self.assertEqual(saved["repeat"], "every week on Monday and Thursday at 8:00 AM")
            self.assertIn("error", tools.execute("reminder", {"action": "create", "text": "Bad", "seconds": 60,
                                                              "repeat": {"freq": "fortnightly"}}))
            self.assertIn("error", tools.execute("reminder", {"action": "answer", "occurrence": 1, "outcome": "maybe"}))

    def test_session_announces_then_rechecks(self):
        self.store.reminder("create", text="Feed cat", seconds=60)
        self.now += 60
        session = AsyncMock()
        with patch.object(tools, "store", self.store), patch.object(tools, "available_to_speak", return_value=True):
            asyncio.run(tools.notify_due(session))
            self.assertIn("[DUE_REMINDERS]", str(session.send_client_content.call_args))
            self.assertIn('"occurrence"', str(session.send_client_content.call_args))
            self.now += 600
            asyncio.run(tools.notify_due(session))
            self.assertIn("[REMINDER_CHECK]", str(session.send_client_content.call_args))
            asyncio.run(tools.notify_due(session))
        self.assertEqual(session.send_client_content.await_count, 2)


if __name__ == "__main__":
    unittest.main()
