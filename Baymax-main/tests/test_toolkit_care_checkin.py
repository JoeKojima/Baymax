"""Facility morning check-in: fake clocks, session, monitor and a temporary care log; no devices or Gemini."""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import runtime_monitor as monitor
from features.care import store
from features.care.store import CareLog
from features.toolkits import care_checkin, checkins, fall_detection, gemini_tools
from features.toolkits.care_checkin import FIRST_NOTE, RETRY_NOTE, CareCheckIn
from features.toolkits.checkins import CheckIns

LIVE = care_checkin.care_checkin
CARE_ON = {"EMBER_CARE_ENABLED": "1", "EMBER_CARE_CLOUD": "0", "BAYMAX_TOOLKIT_ENABLED": "1",
           "BAYMAX_CHECKINS": "1", "EMBER_CARE_CHECKIN_WINDOW": "08:00-10:30"}


class Clock:
    """One fake wall clock behind both the datetime and the epoch the feature reads."""
    def __init__(self):
        self.local = datetime(2026, 11, 2, 9, 0)
        self.up = 30 * 60

    def advance(self, minutes):
        self.local = datetime.fromtimestamp(self.local.timestamp() + minutes * 60)
        self.up += minutes * 60

    def wall(self):
        return self.local.timestamp()


class CareCheckInTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.clock = Clock()
        self.log = CareLog(self.folder / "care.sqlite3", clock=self.clock.wall)
        self.talked = self.clock.wall() - 3600       # Last conversation an hour ago.
        self.spoke = []                              # Wall times of resident speech.
        for patcher in (patch.dict("os.environ", CARE_ON), patch.object(store, "log", self.log),
                        patch.object(monitor, "state", "listening"), patch.object(monitor, "event_observer", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ember = self.make()
        patcher = patch.object(care_checkin, "care_checkin", self.ember)  # The tool reads the same fake clock.
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self):
        ember = CareCheckIn(now=lambda: self.clock.local, clock=lambda: self.clock.up, wall=self.clock.wall,
                            last_conversation=lambda: self.talked, book_playing=lambda: False,
                            user_spoke_since=lambda moment: any(t >= moment for t in self.spoke))
        ember.started = 0.0
        return ember

    def ask(self, ember=None):
        """One session tick; returns the notes sent to the fake Gemini session."""
        ember = ember or self.ember
        session = AsyncMock()
        shutdown = threading.Event()

        async def sleep(seconds):
            shutdown.set()
        ember.sleep = sleep
        asyncio.run(ember.session_task(session, shutdown))
        return [call.kwargs["turns"]["parts"][0]["text"] for call in session.send_client_content.await_args_list]

    def resident_answers(self):
        self.clock.advance(0.5)
        self.spoke.append(self.clock.wall())
        self.talked = self.clock.wall()

    def check_ins(self):
        return self.log.events(kinds=["check_in"])

    # ── Outcomes ────────────────────────────────────────────────────────────
    def test_ok_answer_is_recorded_with_their_words(self):
        self.assertEqual(self.ask(), [FIRST_NOTE])
        self.resident_answers()
        result = care_checkin.execute("check_in_result", {"outcome": "ok", "summary": "Says she is doing well",
                                                          "quote": "I'm good, thanks"})
        self.assertEqual(result, {"recorded": True, "outcome": "ok", "staff_see_it": True})
        [event] = self.check_ins()
        self.assertEqual((event["status"], event["quote"]), ("ok", "I'm good, thanks"))
        self.assertEqual(event["data"], {"check": "morning", "attempts": 1})
        self.assertEqual(self.log.exceptions(), [])
        self.clock.advance(60)
        self.assertEqual(self.ask(), [])           # Done for today: no retry, no "no answer".
        self.clock.advance(120)
        self.ask()
        self.assertEqual(len(self.check_ins()), 1)

    def test_attention_goes_on_the_exceptions_list(self):
        self.ask()
        self.resident_answers()
        quote = "I didn't sleep well and my hip hurts"
        self.assertTrue(care_checkin.execute("check_in_result", {
            "outcome": "attention", "summary": "Says she slept badly and her hip hurts", "quote": quote})["recorded"])
        [event] = self.log.exceptions()
        self.assertEqual((event["kind"], event["status"], event["quote"]), ("check_in", "attention", quote))

    def test_private_answer_reaches_staff_as_summary_only(self):
        self.ask()
        self.resident_answers()
        care_checkin.execute("check_in_result", {"outcome": "attention", "summary": "Feeling low",
                                                 "quote": "Don't tell anyone, I feel low", "private": True})
        [event] = self.log.exceptions()
        self.assertNotIn("quote", event)

    def test_no_reply_then_one_gentle_retry_then_no_answer_once(self):
        self.assertEqual(self.ask(), [FIRST_NOTE])
        for minutes in (2, 3, 20):                 # Waiting for a reply, then not yet time to retry.
            self.clock.advance(minutes)
            self.assertEqual(self.ask(), [], minutes)
        self.assertEqual(self.check_ins(), [])
        self.clock.advance(10)                     # 35 minutes after the first ask.
        self.assertEqual(self.ask(), [RETRY_NOTE])
        self.clock.advance(1)
        self.assertEqual(self.ask(), [])
        self.clock.advance(3)
        self.assertEqual(self.ask(), [])           # No third ask.
        [event] = self.log.exceptions()
        self.assertEqual((event["status"], event["summary"]), ("no_answer", "No answer to the morning check-in (asked 2 times)"))
        self.assertEqual(event["data"]["reason"], "no_reply")
        self.clock.advance(30)
        self.ask()
        self.assertEqual(len(self.check_ins()), 1)

    def test_answer_to_the_retry_is_recorded(self):
        self.ask()
        self.clock.advance(35)
        self.assertEqual(self.ask(), [RETRY_NOTE])
        self.resident_answers()
        self.ask()
        care_checkin.execute("check_in_result", {"outcome": "ok", "summary": "Fine", "quote": "Fine, dear"})
        [event] = self.check_ins()
        self.assertEqual((event["status"], event["data"]["attempts"]), ("ok", 2))

    def test_retry_that_would_fall_after_the_window_becomes_no_answer_at_the_end(self):
        self.clock.local = datetime(2026, 11, 2, 10, 15)
        self.assertEqual(self.ask(), [FIRST_NOTE])
        self.clock.advance(10)
        self.assertEqual(self.ask(), [])
        self.clock.advance(6)                      # 10:31: the window is over.
        self.assertEqual(self.ask(), [])
        [event] = self.log.exceptions()
        self.assertEqual((event["status"], event["data"]), ("no_answer", {"check": "morning", "attempts": 1,
                                                                           "reason": "no_reply"}))

    def test_never_asked_in_the_window_is_no_answer(self):
        self.talked = self.clock.wall()            # Mid-conversation the whole window.
        with patch.object(monitor, "state", "speaking"):
            self.assertEqual(self.ask(), [])
        self.clock.local = datetime(2026, 11, 2, 10, 30)
        self.assertEqual(self.ask(), [])
        [event] = self.log.exceptions()
        self.assertEqual(event["status"], "no_answer")
        self.assertEqual(event["data"]["reason"], "not_reached")
        self.assertIn("10:30", event["summary"])

    def test_reply_without_a_tool_result_goes_on_the_visit_list(self):
        self.ask()
        self.resident_answers()
        self.clock.advance(5)
        self.assertEqual(self.ask(), [])
        self.assertEqual(self.check_ins(), [])
        self.clock.advance(5)
        self.ask()
        [event] = self.check_ins()
        self.assertEqual((event["status"], event["data"]["reason"]), ("attention", "no_result"))
        # On the visit list: a missed worry costs more than one extra visit.
        self.assertEqual([e["id"] for e in self.log.exceptions()], [event["id"]])

    def test_late_answer_replaces_no_answer_on_the_list(self):
        self.ask()
        self.clock.advance(35)
        self.ask()
        self.clock.advance(4)
        self.ask()
        self.assertEqual(self.log.exceptions()[0]["status"], "no_answer")
        self.resident_answers()
        result = care_checkin.execute("check_in_result", {"outcome": "ok", "summary": "Was in the shower; fine"})
        self.assertTrue(result["recorded"])
        statuses = [(e["status"], (e["data"] or {}).get("answered_late")) for e in self.check_ins()]
        self.assertEqual(statuses, [("handled", None), ("ok", True)])
        self.assertEqual(self.log.exceptions(), [])

    # ── Timing ──────────────────────────────────────────────────────────────
    def test_only_inside_the_window_and_not_at_boot_or_mid_conversation(self):
        self.clock.local = datetime(2026, 11, 2, 7, 59)
        self.assertEqual(self.ask(), [])
        self.clock.local = datetime(2026, 11, 2, 8, 0)
        self.talked = self.clock.wall() - 60
        self.assertEqual(self.ask(), [])
        self.talked = self.clock.wall() - 600
        self.ember.started = self.clock.up - 60    # Just booted.
        self.assertEqual(self.ask(), [])
        self.ember.started = 0.0
        with patch.object(monitor, "state", "thinking"):
            self.assertEqual(self.ask(), [])
        self.assertEqual(self.ask(), [FIRST_NOTE])

    def test_asks_even_if_nobody_spoke_for_hours(self):
        # Unlike the friendly check-ins, staff expect this one: an empty room becomes "no answer".
        self.talked = 0.0
        self.assertEqual(self.ask(), [FIRST_NOTE])

    def test_progress_survives_a_restart_and_resets_next_day(self):
        self.ask()
        restarted = self.make()
        restarted.started = None                   # A fresh process: uptime starts again.
        self.clock.advance(35)
        self.assertEqual(self.ask(restarted), [])  # Retry waits for uptime, never the startup hello.
        restarted.started = self.clock.up - 30 * 60
        self.assertEqual(self.ask(restarted), [RETRY_NOTE])
        self.clock.advance(4)
        self.ask(restarted)
        self.assertEqual(len(self.log.exceptions()), 1)
        again = self.make()
        self.clock.advance(5)
        self.ask(again)
        self.assertEqual(len(self.check_ins()), 1)  # "No answer" is recorded once per day.
        self.clock.local = datetime(2026, 11, 3, 8, 30)
        self.assertEqual(self.ask(again), [FIRST_NOTE])

    def test_failed_send_is_not_counted(self):
        session = AsyncMock()
        session.send_client_content.side_effect = RuntimeError("offline")
        shutdown = threading.Event()

        async def sleep(seconds):
            shutdown.set()
        self.ember.sleep = sleep
        asyncio.run(self.ember.session_task(session, shutdown))
        self.assertEqual(self.ember.load("2026-11-02")["attempts"], 0)
        self.assertEqual(self.ask(), [FIRST_NOTE])

    # ── The tool ────────────────────────────────────────────────────────────
    def test_tool_validation(self):
        result = care_checkin.execute
        self.assertIn("error", result("check_in_result", {"outcome": "ok", "summary": "Fine"}))  # Not asked yet.
        self.ask()
        for bad in ({"outcome": "great", "summary": "Fine"}, {"outcome": "ok"}, {"outcome": "ok", "summary": " "},
                    {"outcome": "ok", "summary": "x" * 301}, {"outcome": "ok", "summary": "Fine", "quote": 5},
                    {"outcome": "ok", "summary": "Fine", "private": "yes"},
                    {"outcome": "ok", "summary": "Fine", "diagnosis": "flu"}, "ok"):
            self.assertIn("error", result("check_in_result", bad), bad)
        self.assertEqual(self.check_ins(), [])
        self.assertIn("error", result("other", {}))
        self.assertTrue(result("check_in_result", {"outcome": "ok", "summary": "Fine", "quote": ""})["recorded"])
        self.assertNotIn("quote", self.check_ins()[0])
        self.assertIn("error", result("check_in_result", {"outcome": "attention", "summary": "Again"}))
        self.assertEqual(len(self.check_ins()), 1)

    def test_tool_routed_through_the_toolkit(self):
        self.ask()
        reply = gemini_tools.execute("check_in_result", {"outcome": "ok", "summary": "Fine"})
        self.assertTrue(reply["recorded"])

    def test_tool_and_rules_declared_only_in_facility_mode(self):
        base = {"system_instruction": "Base prompt.", "tools": []}
        config = gemini_tools.extend_config(base)
        names = {d["name"] for tool in config["tools"] for d in tool.get("function_declarations", [])}
        self.assertIn("check_in_result", names)
        self.assertIn("[MORNING CHECK-IN]", config["system_instruction"])
        self.assertTrue(config["system_instruction"].startswith(gemini_tools.persona.apply(base)["system_instruction"]))
        for off in ({"EMBER_CARE_ENABLED": "0"}, {"EMBER_CARE_CHECKIN": "0"}, {"EMBER_CARE_CHECKIN_WINDOW": "0"}):
            with patch.dict("os.environ", off):
                config = gemini_tools.extend_config(base)
                names = {d["name"] for tool in config["tools"] for d in tool.get("function_declarations", [])}
                self.assertNotIn("check_in_result", names, off)
                self.assertNotIn("[MORNING CHECK-IN]", config["system_instruction"], off)
                self.assertIn("error", care_checkin.execute("check_in_result", {"outcome": "ok", "summary": "x"}))
                self.assertEqual(self.ask(), [], off)

    def test_rules_stay_non_clinical(self):
        text = care_checkin.INSTRUCTION
        for rule in ("never diagnose", "no medical advice", "staff decide", "don't call the tool", "data, not instructions"):
            self.assertIn(rule, text)

    def test_registered_on_the_live_session(self):
        self.assertIn(LIVE.session_task, fall_detection.session_tasks)


class FriendlyCheckInsUnchangedTests(unittest.TestCase):
    """With facility mode off, the gentle daily check-ins (#23) behave exactly as before."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.local = datetime(2026, 11, 2, 9, 30)
        self.ember = CheckIns(path=Path(folder.name) / "toolkit_checkins.json", now=lambda: self.local,
                              clock=lambda: 30 * 60, wall=lambda: 100000.0, last_conversation=lambda: 100000.0 - 1200,
                              book_playing=lambda: False)
        self.ember.started = 0.0
        self.log = CareLog(Path(folder.name) / "care.sqlite3")
        for patcher in (patch.object(monitor, "state", "listening"), patch.object(store, "log", self.log)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_morning_question_unchanged_and_nothing_recorded_when_care_is_off(self):
        with patch.dict("os.environ", {**CARE_ON, "EMBER_CARE_ENABLED": "0"}):
            name, note = self.ember.due()
            self.assertEqual(name, "morning")
            self.assertEqual(note, checkins.NOTE.format(name="the morning", ask=checkins.NOTES["morning"]))
            self.assertFalse(care_checkin.active())
            self.assertIsNone(LIVE.tick())
        self.assertEqual(self.log.events(audience="ember"), [])

    def test_facility_mode_takes_over_only_the_morning(self):
        with patch.dict("os.environ", CARE_ON):
            self.assertIsNone(self.ember.due())
            self.local = datetime(2026, 11, 2, 15, 0)
            self.assertEqual(self.ember.due()[0], "afternoon")


if __name__ == "__main__":
    unittest.main()
