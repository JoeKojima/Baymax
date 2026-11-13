"""The activity section of the family's daily report (features/toolkits/activity/daily.py), offline."""
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from features.toolkits.activity import daily
from features.toolkits.activity.store import Store
from features.toolkits.family import reports

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 11, 12)


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


class FakeSettings:
    def reports_on(self):
        return True

    def report_flags_on(self):
        return True

    def report_time(self):
        return (20, 0)


class DailyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "activity.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.n = 0

    def seg(self, posture, start, end, **extra):
        self.n += 1
        self.store.save({"id": f"s{self.n}", "start": start, "end": end, "posture": posture, "hint": posture,
                         "samples": 10, **extra})

    def typical_day(self, d, up_h=1.5, night=True, get_ups=1):
        """A day of the 11th-style: night before, then a day until 20:00."""
        if night:
            start = at(d - 1, 22, 30)
            wake = at(d, 6, 45)
            step = (wake - start) / (get_ups + 1)
            t = start
            for k in range(get_ups + 1):
                end = start + step * (k + 1) - (600 if k < get_ups else 0)
                self.seg("lying", t, end)
                if k < get_ups:
                    self.seg("walking", end, end + 600)
                t = end + 600
        self.seg("standing", at(d, 7), at(d, 7) + up_h * 3600 - 1200)
        self.seg("walking", at(d, 7) + up_h * 3600 - 1200, at(d, 7) + up_h * 3600)
        self.seg("sitting", at(d, 9), at(d, 14))
        self.seg("lying", at(d, 14), at(d, 15))
        self.seg("sitting", at(d, 15), at(d, 18))
        self.seg("absent", at(d, 18), at(d, 20))

    def summary(self, d=12):
        return daily.summary(date(2026, 11, d), at(d, 20), TZ, self.path)

    def test_a_typical_day_reads_like_a_care_note(self):
        self.typical_day(12)
        out = self.summary()
        self.assertEqual(out["lines"], [
            "Rested overnight from 10:30 pm to 6:45 am (about 8 h 5 min). Up once during the night.",
            "Up and about for about 1 h 30 min, including 20 min walking.",
            "Lay down to rest from 2:00 pm to 3:00 pm.",
        ])
        self.assertEqual(out["notes"], [])   # No history yet: nothing is "than usual".
        self.assertEqual(out["night"]["times_up"], 1)
        self.assertEqual(out["day"]["up_min"], 90)
        self.assertIn("Ember saw them for about 10 h 30 min today", out["footnote"])

    def test_only_reliable_things_are_reported(self):
        self.typical_day(12)
        self.seg("upright", at(12, 18), at(12, 19), hand_to_mouth=12)
        text = json.dumps(self.summary()).lower()
        for word in ("eat", "hand", "upright", "confidence", "agreement", "sitting", "restless spell", "fall"):
            self.assertNotIn(word, text)

    def test_nothing_to_say_means_no_section(self):
        self.assertIsNone(self.summary())
        self.assertIsNone(daily.summary(DAY, at(12, 20), TZ, Path(self.path.parent) / "missing.sqlite3"))

    def test_little_camera_time_gives_no_daytime_numbers(self):
        self.seg("sitting", at(12, 10), at(12, 11))
        out = self.summary()
        self.assertEqual(out["lines"], ["Ember only saw them briefly today, so there is no daytime summary."])
        self.assertNotIn("day", out)

    def test_camera_off_for_much_of_the_night_says_nothing_about_it(self):
        self.seg("lying", at(11, 22), at(11, 23, 30))
        self.seg("lying", at(12, 5), at(12, 6, 30))   # 22:00-06:30 with 5.5 h missing.
        self.seg("standing", at(12, 7), at(12, 10))
        out = self.summary()
        self.assertNotIn("night", out)
        self.assertFalse(any("overnight" in line for line in out["lines"]))

    def test_four_get_ups_is_a_restless_night(self):
        self.typical_day(12, get_ups=4)
        self.assertIn("A restless night: up 4 times.", self.summary()["lines"][0])
        self.assertTrue(self.summary()["night"]["restless"])

    def test_clear_changes_from_the_last_two_weeks_are_noted(self):
        for d in range(4, 12):
            self.typical_day(d, up_h=2.0)
        # Today: short night (23:30-04:30), little activity, a long afternoon lie-down.
        self.seg("lying", at(11, 23, 30), at(12, 4, 30))
        self.seg("standing", at(12, 7), at(12, 7, 25))
        self.seg("sitting", at(12, 7, 25), at(12, 12))
        self.seg("lying", at(12, 12), at(12, 16))
        self.seg("sitting", at(12, 16), at(12, 20))
        kinds = [n["kind"] for n in self.summary()["notes"]]
        self.assertEqual(kinds, ["less_rest", "less_active", "more_day_rest"])
        texts = " ".join(n["text"] for n in self.summary()["notes"])
        self.assertIn("usually about 2 h", texts)

    def test_an_ordinary_day_against_its_history_has_no_notes(self):
        for d in range(4, 13):
            self.typical_day(d, up_h=2.0)
        self.assertEqual(self.summary()["notes"], [])


class ReportHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.now = at(12, 20, 5)
        self.store = reports.ReportStore(self.folder / "r.sqlite3", clock=lambda: self.now)
        self.section = {"version": 1, "lines": ["Up and about for about 1 h."], "notes": [], "footnote": "x"}
        self.calls = []

        def activity(day, until, tz):
            self.calls.append((day, until))
            if isinstance(self.section, Exception):
                raise self.section
            return self.section
        self.reporter = reports.Reporter(self.store, FakeSettings(), generate=lambda prompt: {},
                                         clock=lambda: self.now, tz="America/New_York", activity=activity)

    def test_the_day_report_carries_the_activity_section(self):
        self.reporter.tick()
        rows = self.store.pending_sync(10)["day_reports"]
        self.assertEqual(rows[0]["date"], "2026-11-12")
        self.assertEqual(rows[0]["activity"], self.section)
        self.assertEqual(self.calls, [(DAY, at(12, 20))])

    def test_a_preview_carries_today_so_far(self):
        self.reporter.write_preview("req-1")
        preview = self.store.pending_sync(10)["previews"][0]
        self.assertEqual(preview["activity"], self.section)
        self.assertNotIn("_activity", preview["conversation"])

    def test_no_section_or_an_error_leaves_the_report_unchanged(self):
        self.section = None
        self.reporter.tick()
        self.assertNotIn("activity", self.store.pending_sync(10)["day_reports"][0])
        self.section = RuntimeError("store locked")
        self.reporter.write_preview("req-2")
        self.assertNotIn("activity", self.store.pending_sync(10)["previews"][0])

    def test_an_older_journal_gains_the_column(self):
        path = self.folder / "old.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE reports (date TEXT PRIMARY KEY, until REAL NOT NULL, body TEXT NOT NULL, "
                   "synced INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO reports VALUES ('2026-11-10', 1.0, '{}', 0)")
        db.commit()
        db.close()
        old = reports.ReportStore(path, clock=lambda: self.now)
        self.assertEqual(old.pending_sync(10)["day_reports"], [{"date": "2026-11-10", "conversation": {}}])

    def test_the_runtime_reporter_uses_activity_only_when_tracking_is_on(self):
        with mock.patch.dict("os.environ", {"BAYMAX_ACTIVITY_ENABLED": "0"}):
            reports._reporter = None
            try:
                self.assertIsNone(reports.reporter().activity(DAY, at(12, 20), TZ))
            finally:
                reports._reporter = None


if __name__ == "__main__":
    unittest.main()
