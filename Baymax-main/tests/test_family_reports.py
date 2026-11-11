"""Daily conversation reports and urgent alerts (features/toolkits/family/reports.py), offline."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from features.toolkits.everyday.store import Store
from features.toolkits.family import reports, tools as family
from features.toolkits.family.link import Link
from features.toolkits.family.store import FamilyStore

TZ = ZoneInfo("America/New_York")


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


class FakeSettings:
    def __init__(self):
        self.reports, self.flags, self.time = True, True, (20, 0)

    def reports_on(self):
        return self.reports

    def report_flags_on(self):
        return self.reports and self.flags

    def report_time(self):
        return self.time


class ReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = at(11, 9)
        self.store = reports.ReportStore(Path(tmp.name) / "r.sqlite3", clock=lambda: self.now)
        self.settings = FakeSettings()
        self.prompts = []
        self.answer = {}

        def generate(prompt):
            self.prompts.append(prompt)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        self.reporter = reports.Reporter(self.store, self.settings, generate=generate, profile=lambda: "Ruth, 82",
                                         clock=lambda: self.now, tz="America/New_York")

    def say(self, when, speaker, text):
        self.now = when
        self.reporter.record(speaker, text)

    def day(self):
        self.say(at(11, 9), "user", "Good morning Ember, my knee hurts again today")
        self.say(at(11, 9, 1), "gemini", "I'm sorry to hear that.")
        self.say(at(11, 15), "user", "My daughter Jane is visiting on Sunday")

    # ── journal ──
    def test_nothing_journalled_with_reports_off(self):
        self.settings.reports = False
        self.say(at(11, 9), "user", "hello")
        self.assertEqual(self.store.turns(), [])

    def test_conversations_split_at_long_pauses(self):
        self.day()
        groups = reports.conversations(self.store.turns())
        self.assertEqual([len(g[2]) for g in groups], [2, 1])

    # ── when reports are due ──
    def test_first_run_reports_only_today_after_report_time(self):
        self.now = at(11, 19, 59)
        self.assertEqual(self.reporter.due_dates(), [])
        self.now = at(11, 20)
        self.assertEqual([d.isoformat() for d in self.reporter.due_dates()], ["2026-11-11"])

    def test_catches_up_after_being_off_and_never_repeats(self):
        self.store.add_report("2026-11-08", at(8, 20), {"no_conversation": True})
        self.now = at(11, 10)   # off from the 9th to this morning
        self.assertEqual([d.isoformat() for d in self.reporter.due_dates()], ["2026-11-09", "2026-11-10"])
        self.reporter.tick()
        self.assertEqual(self.reporter.due_dates(), [])

    def test_report_time_setting(self):
        self.settings.time = (8, 30)
        self.now = at(11, 8, 31)
        self.assertEqual([d.isoformat() for d in self.reporter.due_dates()], ["2026-11-11"])

    # ── writing it ──
    def test_quiet_day_needs_no_gemini_call(self):
        self.now = at(11, 20, 1)
        body = self.reporter.write_report(datetime(2026, 11, 11).date())
        self.assertTrue(body["no_conversation"])
        self.assertEqual(self.prompts, [])

    def test_report_keeps_only_real_quotes_and_few_flags(self):
        self.day()
        self.store.set_watch([{"id": "w1", "text": "her knee"}])
        self.answer = {
            "summary": "Ruth chatted about her week.", "topics": ["knee", "Jane's visit"], "mood": "content",
            "flags": [{"category": "health", "level": "watch", "title": "Knee pain again", "detail": "Mentioned twice.",
                       "quotes": ["my knee hurts again today", "I fell down the stairs"]}]
            + [{"category": "bogus", "title": f"Extra {n}", "quotes": []} for n in range(6)],
            "watch_matches": [{"watch_id": "w1", "note": "She brought it up.", "quotes": ["My knee hurts again"]},
                              {"watch_id": "unknown", "note": "x"}],
        }
        self.now = at(11, 20, 5)
        body = self.reporter.write_report(datetime(2026, 11, 11).date())
        self.assertIn("her knee", self.prompts[0])
        self.assertIn("Person: My daughter Jane is visiting on Sunday", self.prompts[0])
        self.assertIn("Ruth, 82", self.prompts[0])
        self.assertEqual(body["conversations"], 2)
        self.assertEqual(body["flags"][0]["quotes"], ["my knee hurts again today"])   # invented quote dropped
        self.assertEqual(len(body["flags"]), reports.MAX_FLAGS)
        self.assertEqual(body["flags"][1]["category"], "health")                       # unknown category tidied
        self.assertEqual([m["watch_id"] for m in body["watch_matches"]], ["w1"])
        self.assertEqual(body["watch_matches"][0]["text"], "her knee")
        sent = self.store.pending_sync(15)["day_reports"]
        self.assertEqual(sent[0]["date"], "2026-11-11")
        self.assertEqual(sent[0]["conversation"]["summary"], "Ruth chatted about her week.")

    def test_flags_off_summary_only(self):
        self.day()
        self.settings.flags = False
        self.answer = {"summary": "A calm day.", "flags": [{"title": "Should not appear"}],
                       "watch_matches": [{"watch_id": "w1"}]}
        self.now = at(11, 20, 5)
        body = self.reporter.write_report(datetime(2026, 11, 11).date())
        self.assertIn("turned concern flags off", self.prompts[0])
        self.assertEqual((body["flags"], body["watch_matches"], body["flags_enabled"]), ([], [], False))

    def test_next_report_starts_where_the_last_ended(self):
        self.day()
        self.answer = {"summary": "Day one."}
        self.now = at(11, 20, 5)
        self.reporter.write_report(datetime(2026, 11, 11).date())
        self.say(at(11, 22), "user", "Late night chat about the baseball game")
        self.now = at(12, 20, 5)
        self.reporter.write_report(datetime(2026, 11, 12).date())
        self.assertIn("baseball", self.prompts[1])
        self.assertNotIn("knee", self.prompts[1])

    def test_gemini_failure_leaves_the_day_for_later(self):
        self.day()
        self.answer = RuntimeError("quota")
        self.now = at(11, 20, 5)
        self.reporter.tick()
        self.assertFalse(self.store.has_report("2026-11-11"))

    # ── urgent ──
    def test_urgent_check_waits_for_a_pause_then_runs_once(self):
        self.say(at(11, 9), "user", "I have a pain in my chest and my arm feels numb")
        self.answer = {"urgent": [{"title": "Chest pain", "detail": "Said so.", "quotes": ["a pain in my chest", "made up"]}]}
        self.now = at(11, 9, 1)
        self.assertEqual(self.reporter.check_urgent(), [])   # still talking
        self.assertEqual(self.prompts, [])
        self.now = at(11, 9, 5)
        added = self.reporter.check_urgent()
        self.assertEqual(added[0]["quotes"], ["a pain in my chest"])
        self.assertEqual(self.reporter.check_urgent(), [])   # nothing new
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.store.pending_sync(15)["alerts"][0]["title"], "Chest pain")

    def test_no_urgent_check_with_flags_off(self):
        self.settings.flags = False
        self.say(at(11, 9), "user", "I have a pain in my chest")
        self.now = at(11, 9, 5)
        self.assertEqual(self.reporter.check_urgent(), [])
        self.assertEqual(self.prompts, [])

    def test_urgent_failure_retries_the_same_lines(self):
        self.say(at(11, 9), "user", "I fell in the kitchen")
        self.now = at(11, 9, 5)
        self.answer = RuntimeError("offline")
        self.reporter.tick()
        self.answer = {"urgent": []}
        self.reporter.check_urgent()
        self.assertIn("I fell in the kitchen", self.prompts[-1])

    # ── mention once ──
    def test_mention_once(self):
        self.assertTrue(self.reporter.needs_mention())
        self.reporter.mentioned()
        self.assertFalse(self.reporter.needs_mention())


class LinkReportTests(unittest.TestCase):
    def test_watch_list_in_and_reports_out(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = reports.ReportStore(root / "r.sqlite3")
        store.add_report("2026-11-11", at(11, 20), {"summary": "ok"})
        store.add_alert({"title": "Fall", "detail": "", "quotes": []})
        inbox = {"poll_seconds": 15, "watch_for": [{"id": "w1", "text": "driving"}]}
        syncs = []
        http = SimpleNamespace(
            get=lambda url, headers, timeout: SimpleNamespace(raise_for_status=lambda: None, json=lambda: inbox),
            post=lambda url, json, headers, timeout: syncs.append(json) or SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: self.reply(json)))
        settings = SimpleNamespace(apply=lambda s: [], report=dict)
        self.site_has_reports = False
        link = Link(FamilyStore(root / "f.sqlite3"), Store(root / "e.sqlite3", tz="UTC"), url="https://example.test",
                    key="002.k", summarizer=lambda n: n, http=http, settings=settings, reports=store)
        link.cycle()
        self.assertEqual(store.watch(), [{"id": "w1", "text": "driving"}])
        self.assertEqual(syncs[0]["day_reports"], [{"date": "2026-11-11", "conversation": {"summary": "ok"}}])
        self.assertEqual(syncs[0]["alerts"][0]["title"], "Fall")
        link.cycle()                      # an older site ignored them: they are sent again
        self.assertEqual(len(syncs[1]["day_reports"]), 1)
        self.site_has_reports = True
        link.cycle()
        link.cycle()
        self.assertEqual((syncs[-1]["day_reports"], syncs[-1]["alerts"]), ([], []))   # stored once acknowledged

    def reply(self, body):
        if not self.site_has_reports:
            return {"status": "ok"}
        return {"status": "ok", "accepted": {"day_reports": [r["date"] for r in body["day_reports"]],
                                             "alerts": [a["id"] for a in body["alerts"]]}}


class MentionTaskTests(unittest.TestCase):
    def test_session_task_mentions_once_when_ember_can_speak(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = reports.ReportStore(Path(tmp.name) / "r.sqlite3")
        daily = reports.Reporter(store, FakeSettings(), generate=lambda p: {}, tz="UTC")
        session = SimpleNamespace(send_client_content=AsyncMock())
        with patch.object(reports, "reporter", lambda: daily), patch.object(family.everyday, "available_to_speak", lambda: True):
            asyncio.run(family.mention_reports(session))
            asyncio.run(family.mention_reports(session))
        self.assertEqual(session.send_client_content.await_count, 1)
        self.assertIn("[DAILY REPORTS]", session.send_client_content.call_args.kwargs["turns"]["parts"][0]["text"])


if __name__ == "__main__":
    unittest.main()
