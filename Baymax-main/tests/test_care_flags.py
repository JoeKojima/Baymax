"""Night activity episodes and care-level review flags (offline, fake clock, synthetic histories)."""
import asyncio
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.care import flags, night, store
from features.care.store import CareLog

DAY = 86400
NIGHT = ((23, 0), (6, 0))


def at(text):
    return datetime.fromisoformat(text).astimezone().timestamp()


class Clock:
    def __init__(self, when):
        self.now = at(when)

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    env = {"EMBER_CARE_ENABLED": "1"}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock("2026-11-13T09:00:00")
        self.path = Path(self.dir.name) / "care.sqlite3"
        self.log = CareLog(self.path, self.clock)
        for patcher in (patch.object(store, "forward"), patch.dict(os.environ, self.env)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith(("EMBER_CARE_FLAGS", "EMBER_CARE_NIGHT"))]:
            del os.environ[key]  # Restored by patch.dict above.

    def add(self, kind, when, status="info", data=None, private=False):
        """Record an event as if it happened at `when` (epoch seconds)."""
        saved, self.clock.now = self.clock.now, when
        try:
            return self.log.record(kind, f"synthetic {kind}", status=status, data=data, private=private)
        finally:
            self.clock.now = saved

    def nights(self):
        return self.log.events(kinds=["night_wake"], audience="ember")

    def flag_events(self):
        return self.log.events(kinds=["flag"], audience="ember")


class NightEpisodeTests(Base):
    def update(self, times, now):
        return night.update(self.log, [at(t) for t in times], at(now), NIGHT, 1800)

    def test_window_parsing_and_membership(self):
        self.assertEqual(night.window("23:00-06:00"), NIGHT)
        self.assertIsNone(night.window("off"))
        self.assertEqual(night.window("nonsense"), NIGHT)
        self.assertTrue(night.in_night(at("2026-11-12T23:30:00"), NIGHT))
        self.assertTrue(night.in_night(at("2026-11-13T05:59:00"), NIGHT))
        self.assertFalse(night.in_night(at("2026-11-13T06:00:00"), NIGHT))
        self.assertFalse(night.in_night(at("2026-11-12T22:59:00"), NIGHT))
        self.assertTrue(night.in_night(at("2026-11-13T01:00:00"), ((0, 30), (5, 0))))

    def test_one_event_per_episode_across_midnight(self):
        self.update(["2026-11-12T23:50:00", "2026-11-13T00:05:00", "2026-11-13T00:20:00"], "2026-11-13T00:21:00")
        self.assertEqual(self.nights(), [])  # Still going.
        self.update(["2026-11-13T02:00:00"], "2026-11-13T02:01:00")  # >30 min later: a new episode.
        [first] = self.nights()
        self.assertEqual(first["data"]["utterances"], 3)
        self.assertEqual(first["data"]["night"], "2026-11-12")
        self.assertEqual(first["status"], "info")
        self.assertIn("from 23:50 to 00:20", first["summary"])
        self.update([], "2026-11-13T02:40:00")
        second = self.nights()[1]
        self.assertEqual((second["data"]["utterances"], second["data"]["night"]), (1, "2026-11-12"))
        self.assertIn("at 02:00 (1 time)", second["summary"])

    def test_daytime_speech_ignored_and_transcripts_counted_once(self):
        times = ["2026-11-13T14:00:00", "2026-11-13T23:10:00"]
        self.update(times, "2026-11-13T23:11:00")
        self.update(times, "2026-11-13T23:12:00")  # Same transcripts seen again.
        self.update([], "2026-11-14T00:00:00")
        [event] = self.nights()
        self.assertEqual(event["data"]["utterances"], 1)

    def test_episode_survives_restart(self):
        self.update(["2026-11-13T01:00:00", "2026-11-13T01:10:00"], "2026-11-13T01:11:00")
        # Restart: new log object on the same file, the old in-memory transcripts are gone.
        self.log = CareLog(self.path, self.clock)
        self.update(["2026-11-13T01:25:00"], "2026-11-13T01:26:00")
        self.assertEqual(self.nights(), [])
        # The runtime stays down; the dashboard loop closes the stale episode.
        self.clock.now = at("2026-11-13T03:00:00")
        night.close_stale(self.log)
        [event] = self.nights()
        self.assertEqual(event["data"]["utterances"], 3)
        self.assertTrue(event["data"]["start"].startswith("2026-11-13T01:00"))
        self.assertEqual(night.close_stale(self.log), [])  # Not twice.

    def test_disabled_care_or_window_records_nothing(self):
        with patch.dict(os.environ, {"EMBER_CARE_ENABLED": "0"}):
            self.assertEqual(self.update(["2026-11-13T01:00:00"], "2026-11-13T03:00:00"), [])
        with patch.dict(os.environ, {"EMBER_CARE_NIGHT": "off"}):
            self.assertEqual(night.update(self.log, [at("2026-11-13T01:00:00")], at("2026-11-13T03:00:00")), [])
        self.assertEqual(self.nights(), [])

    def test_session_task_reads_existing_transcripts_and_never_blocks(self):
        import runtime_monitor as monitor
        night.register()
        night.register()
        from features.toolkits.fall_detection import session_tasks
        self.assertEqual(session_tasks.count(night.session_task), 1)
        fake = [{"speaker": "user", "text": "hello", "time": 5.0}, {"speaker": "ember", "text": "hi", "time": 6.0},
                {"speaker": "user", "text": "  ", "time": 7.0}]
        with patch.object(monitor, "transcripts", fake):
            self.assertEqual(night.user_speech_times(), [5.0])
        calls = []
        with patch.object(night, "update", side_effect=lambda *a: calls.append(a)), \
                patch.object(night, "user_speech_times", return_value=[1.0]):
            async def run():
                task = asyncio.create_task(night.session_task(SimpleNamespace(send_client_content=AsyncMock()),
                                                              threading.Event()))
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            asyncio.run(asyncio.wait_for(run(), 2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], [1.0])


class FlagRuleTests(Base):
    def setUp(self):
        super().setUp()
        self.now = self.clock.now
        # An old, quiet history so baselines are available (a note 60 days ago).
        self.add("note", self.now - 60 * DAY)

    def spread(self, kind, count, start_days_ago, end_days_ago, **fields):
        span = (start_days_ago - end_days_ago) * DAY
        return [self.add(kind, self.now - start_days_ago * DAY + span * (i + 0.5) / count, **fields)
                for i in range(count)]

    def result(self, rule, results=None):
        results = results if results is not None else flags.evaluate(self.log, self.now)
        return next(r for r in results if r["rule"] == rule)

    def test_help_requests_rise_against_baseline(self):
        self.spread("help_request", 4, 42, 14)  # Baseline: 2 per fortnight.
        ids = self.spread("help_request", 5, 14, 0)
        r = self.result("help_requests")
        self.assertTrue(r["flagged"])
        [flag] = self.flag_events()
        self.assertEqual(flag["status"], "attention")
        self.assertEqual(flag["summary"],
                         "5 requests for a person to come in the last 14 days (usually about 2) — for staff review of care level")
        data = flag["data"]
        self.assertEqual((data["basis"], data["current"], data["baseline"]["count"], data["baseline"]["per_window"]),
                         ("baseline", 5, 4, 2.0))
        self.assertEqual(data["event_ids"], ids)
        self.assertEqual(data["window"]["days"], 14)

    def test_help_requests_steady_or_too_few_do_not_flag(self):
        self.spread("help_request", 10, 42, 14)  # Baseline 5 per fortnight.
        self.spread("help_request", 6, 14, 0)    # 6 < 1.5 x 5
        self.assertFalse(self.result("help_requests")["triggered"])
        self.assertEqual(self.flag_events(), [])

    def test_absolute_minimum_applies_even_from_zero_baseline(self):
        self.spread("help_request", 3, 14, 0)
        self.assertFalse(self.result("help_requests")["triggered"])
        self.add("help_request", self.now - DAY)
        self.assertTrue(self.result("help_requests")["flagged"])

    def test_requests_rule(self):
        self.spread("request", 4, 42, 14)
        self.spread("request", 9, 14, 0)
        self.assertFalse(self.result("requests")["triggered"])
        self.add("request", self.now - DAY, status="open")
        self.assertTrue(self.result("requests")["flagged"])

    def test_night_activity_counts_nights_not_episodes(self):
        for days_ago in (1, 3, 5, 7):
            for _ in range(3):  # Several episodes on one night still count once.
                self.add("night_wake", self.now - days_ago * DAY, data={"night": f"n{days_ago}"})
        self.assertFalse(self.result("night_activity")["triggered"])
        self.add("night_wake", self.now - 9 * DAY, data={"night": "n9"})
        r = self.result("night_activity")
        self.assertTrue(r["flagged"])
        self.assertEqual(r["summary"], "Awake at night on 5 of the last 14 nights (none in the 28 days before) — for staff review of care level")

    def test_night_activity_steady_pattern_not_flagged_against_its_baseline(self):
        for days_ago in range(1, 42, 2):  # Every other night for six weeks: ~7 per fortnight, always.
            self.add("night_wake", self.now - days_ago * DAY, data={"night": f"n{days_ago}"})
        r = self.result("night_activity")
        self.assertGreaterEqual(r["current"], 5)
        self.assertFalse(r["triggered"])

    def test_check_ins_needing_a_look(self):
        self.add("check_in", self.now - 2 * DAY, status="ok")
        self.add("check_in", self.now - 3 * DAY, status="no_answer")
        handled = self.add("check_in", self.now - 4 * DAY, status="attention")
        self.log.handle(handled, "Maria")
        self.add("check_in", self.now - 20 * DAY, status="attention")  # Outside the window.
        self.assertFalse(self.result("check_ins")["triggered"])
        self.add("check_in", self.now - 5 * DAY, status="attention")
        r = self.result("check_ins")
        self.assertTrue(r["flagged"])
        self.assertEqual(r["current"], 3)
        self.assertEqual(r["data"]["basis"], "absolute")

    def test_two_falls_in_thirty_days(self):
        self.add("fall", self.now - 40 * DAY, status="attention")
        self.add("fall", self.now - 25 * DAY, status="attention")
        self.assertFalse(self.result("falls")["triggered"])
        self.add("fall", self.now - DAY, status="attention")
        r = self.result("falls")
        self.assertTrue(r["flagged"])
        self.assertEqual(r["summary"], "2 falls recorded in the last 30 days — for staff review of care level")
        self.assertEqual(r["data"]["window"]["days"], 30)

    def test_cooldown_then_worsening_then_after_cooldown(self):
        self.spread("help_request", 4, 14, 0)
        self.assertTrue(self.result("help_requests")["flagged"])
        self.now += 7 * DAY
        self.spread("help_request", 4, 7, 0)  # Window now holds 6: at least 25% (and 1) more than 4.
        r = self.result("help_requests")
        self.assertTrue(r["flagged"])  # Worse: re-flagged inside the cooldown.
        self.now += DAY  # Still 6: not worse than the last flag, and inside the cooldown.
        r = self.result("help_requests")
        self.assertTrue(r["triggered"])
        self.assertEqual(r["skipped"], "cooldown")
        self.assertEqual(len(self.flag_events()), 2)
        self.now += 14 * DAY
        self.spread("help_request", 6, 14, 0)
        self.assertTrue(self.result("help_requests")["flagged"])  # Cooldown over.

    def test_evidence_capped(self):
        self.spread("request", 30, 14, 0)
        r = self.result("requests")
        self.assertEqual(len(r["data"]["event_ids"]), flags.EVIDENCE_CAP)
        self.assertTrue(r["data"]["event_ids_truncated"])
        self.assertEqual(r["data"]["event_count"], 30)

    def test_private_events_are_not_counted(self):
        self.spread("request", 12, 14, 0, private=True)
        self.assertEqual(self.result("requests")["current"], 0)

    def test_thresholds_from_environment(self):
        self.spread("help_request", 2, 14, 0)
        with patch.dict(os.environ, {"EMBER_CARE_FLAGS_HELP_MIN": "2"}):
            self.assertTrue(self.result("help_requests")["flagged"])
        with patch.dict(os.environ, {"EMBER_CARE_FLAGS_FALLS": "bad"}):
            self.assertEqual(flags.setting("FALLS"), 2)


class MinimumHistoryTests(Base):
    def test_short_history_uses_absolute_thresholds_and_says_so(self):
        now = self.clock.now
        for i in range(4):
            self.add("help_request", now - (i + 1) * DAY)
        r = next(x for x in flags.evaluate(self.log, now) if x["rule"] == "help_requests")
        self.assertTrue(r["flagged"])
        self.assertIsNone(r["data"]["baseline"])
        self.assertEqual(r["data"]["basis"], "absolute")
        self.assertIn("Less than 14 days of history", r["data"]["note"])
        self.assertNotIn("usually", r["summary"])

    def test_partial_baseline_is_scaled(self):
        now = self.clock.now
        self.add("help_request", now - 34 * DAY)  # 20 days of baseline history, one request.
        for i in range(4):
            self.add("help_request", now - (i + 1) * DAY)
        r = next(x for x in flags.evaluate(self.log, now) if x["rule"] == "help_requests")
        self.assertEqual(r["data"]["basis"], "baseline")
        self.assertEqual(r["data"]["baseline"]["days"], 20.0)
        self.assertEqual(r["data"]["baseline"]["per_window"], 0.7)


class ScheduleAndWordingTests(Base):
    def test_weekly_run_once_across_restarts(self):
        now = self.clock.now
        for i in range(4):
            self.add("help_request", now - (i + 1) * DAY)
        self.assertIsNotNone(flags.evaluate_if_due(self.log, now))
        self.assertIsNone(flags.evaluate_if_due(CareLog(self.path, self.clock), now + 3600))  # "Restarted".
        self.assertIsNone(flags.evaluate_if_due(self.log, now + 6 * DAY))
        self.assertIsNotNone(flags.evaluate_if_due(self.log, now + 7 * DAY))
        with patch.dict(os.environ, {"EMBER_CARE_FLAGS_EVERY_DAYS": "0"}):
            self.assertIsNone(flags.evaluate_if_due(self.log, now + 30 * DAY))

    def test_failed_run_is_retried(self):
        with patch.object(flags, "evaluate", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                flags.evaluate_if_due(self.log, self.clock.now)
        self.assertIsNotNone(flags.evaluate_if_due(self.log, self.clock.now + 60))

    def test_disabled_records_nothing(self):
        now = self.clock.now
        for i in range(6):
            self.add("help_request", now - (i + 1) * DAY)
            self.add("fall", now - (i + 1) * DAY)
        with patch.dict(os.environ, {"EMBER_CARE_ENABLED": "0"}):
            self.assertEqual(flags.evaluate(self.log, now), [])
            self.assertIsNone(flags.evaluate_if_due(self.log, now))
        with patch.dict(os.environ, {"EMBER_CARE_FLAGS": "0"}):
            self.assertEqual(flags.evaluate(self.log, now), [])
        self.assertEqual(self.flag_events(), [])

    def test_wording_is_never_clinical(self):
        now = self.clock.now
        self.add("note", now - 60 * DAY)
        for i in range(12):
            for kind, status in (("help_request", "open"), ("request", "open"), ("fall", "attention"),
                                 ("check_in", "no_answer")):
                self.add(kind, now - (i + 0.5) * DAY, status=status)
            self.add("night_wake", now - (i + 0.5) * DAY, data={"night": str(i)})
        results = flags.evaluate(self.log, now)
        self.assertEqual({r["rule"] for r in results if r["flagged"]}, set(flags.RULES))
        texts = [r["summary"] + " " + str(r["data"]) for r in results]
        texts += [e["summary"] for e in self.flag_events()]
        for value in texts:
            self.assertTrue(value.split(" {")[0].endswith("for staff review of care level"), value)
            for word in flags.BANNED + ("needs more care", "worse", "concern", "risk"):
                self.assertNotIn(word, value.lower())
        # Night activity summaries too.
        night.update(self.log, [at("2026-11-13T02:00:00")], at("2026-11-13T04:00:00"), NIGHT, 1800)
        for event in self.log.events(kinds=["night_wake"], audience="ember")[-1:]:
            for word in flags.BANNED:
                self.assertNotIn(word, event["summary"].lower())


class DashboardRouteTests(Base):
    env = {"EMBER_CARE_ENABLED": "1", "EMBER_STAFF_EMAILS": "nurse@example.com"}

    def setUp(self):
        super().setUp()
        from flask import Flask
        from features.care import dashboard
        self.user = {"email": "nurse@example.com", "username": "nurse"}
        app = Flask(__name__)
        dashboard.register(app, lambda: self.user, lambda: [], lambda *a: None, {"serial": "x"},
                           self.dir.name, start_digest=False)
        self.client = app.test_client()
        patcher = patch.object(store, "log", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_staff_run_on_demand_and_list_flags(self):
        for i in range(2):
            self.add("fall", self.clock.now - (i + 1) * DAY, status="attention")
        response = self.client.post("/api/care/flags")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["flagged"]), 1)
        listed = self.client.get("/api/care/flags").get_json()["flags"]
        self.assertEqual([f["data"]["rule"] for f in listed], ["falls"])

    def test_family_cannot_see_or_run_flags(self):
        self.user = {"email": "daughter@example.com"}
        self.assertEqual(self.client.get("/api/care/flags").status_code, 403)
        self.assertEqual(self.client.post("/api/care/flags").status_code, 403)


if __name__ == "__main__":
    unittest.main()
