"""Facility toolkit: calendar/menu parsing, facility_info, activity reminders, staff requests,
per-route request emails and the staff upload route (offline: no network, Gemini or devices)."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from features.care import store as care_store
from features.care.store import CareLog
from features.toolkits.everyday import tools as everyday
from features.toolkits.everyday.store import Store
from features.toolkits.facility import dashboard, ics, sources, tools
from features.toolkits.facility.sources import Sources

NY = ZoneInfo("America/New_York")
ON = {"EMBER_CARE_ENABLED": "1", "EMBER_STAFF_EMAILS": "nurse@example.com"}


def calendar(*events):
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN", *events, "END:VCALENDAR"])
    return ics.parse(body, NY)


def vevent(*lines):
    return "\r\n".join(["BEGIN:VEVENT", *lines, "END:VEVENT"])


def titles(cal, day):
    return [(i["title"], i["time"]) for i in cal.on(day, NY)]


class IcsTests(unittest.TestCase):
    def test_folding_escapes_utc_and_tzid(self):
        cal = calendar(
            vevent("UID:1", "DTSTART:20261113T190000Z", "DTEND:20261113T200000Z",
                   "SUMMARY:Bingo\\, with prizes", "LOCATION:Main", " Lounge"),
            vevent("UID:2", 'DTSTART;TZID="America/Los_Angeles":20261113T080000', "SUMMARY:Call with LA"),
            vevent("UID:3", "DTSTART:20261113T100000", "SUMMARY:Floating chair yoga"))
        items = cal.on(date(2026, 11, 13), NY)
        self.assertEqual([(i["title"], i["time"]) for i in items],
                         [("Floating chair yoga", "10:00"), ("Call with LA", "11:00"), ("Bingo, with prizes", "14:00")])
        self.assertEqual(items[2]["location"], "MainLounge")
        self.assertEqual(items[2]["ends_at"], "2026-11-13T15:00-05:00")

    def test_utc_event_near_midnight_lands_on_the_local_day(self):
        cal = calendar(vevent("UID:1", "DTSTART:20261114T030000Z", "SUMMARY:Late movie"))
        self.assertEqual(titles(cal, date(2026, 11, 13)), [("Late movie", "22:00")])
        self.assertEqual(titles(cal, date(2026, 11, 14)), [])

    def test_all_day_single_and_multi_day(self):
        cal = calendar(vevent("UID:1", "DTSTART;VALUE=DATE:20261113", "SUMMARY:Craft fair"),
                       vevent("UID:2", "DTSTART;VALUE=DATE:20261114", "DTEND;VALUE=DATE:20261116", "SUMMARY:Family weekend"))
        self.assertEqual(titles(cal, date(2026, 11, 13)), [("Craft fair", None)])
        self.assertEqual(titles(cal, date(2026, 11, 15)), [("Family weekend", None)])
        self.assertEqual(titles(cal, date(2026, 11, 16)), [])
        self.assertTrue(cal.on(date(2026, 11, 13), NY)[0]["all_day"])

    def test_weekly_byday_count_until_interval_and_dst(self):
        cal = calendar(
            vevent("UID:w", "DTSTART;TZID=America/New_York:20261026T140000", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4",
                   "SUMMARY:Bingo"),
            vevent("UID:u", "DTSTART;TZID=America/New_York:20261101T100000", "RRULE:FREQ=WEEKLY;INTERVAL=2;UNTIL=20261115",
                   "SUMMARY:Church bus"))
        self.assertEqual(titles(cal, date(2026, 10, 28)), [("Bingo", "14:00")])
        # After the DST change on Nov 1 the wall-clock time stays 14:00.
        self.assertEqual(titles(cal, date(2026, 11, 4)), [("Bingo", "14:00")])
        self.assertEqual(titles(cal, date(2026, 11, 9)), [])  # COUNT=4 used up (26, 28, 2, 4).
        self.assertEqual(titles(cal, date(2026, 11, 8)), [])  # Every other week.
        self.assertEqual(titles(cal, date(2026, 11, 15)), [("Church bus", "10:00")])  # Date UNTIL is inclusive.
        self.assertEqual(titles(cal, date(2026, 11, 29)), [])

    def test_daily_exdate_override_and_cancelled_occurrence(self):
        cal = calendar(
            vevent("UID:d", "DTSTART:20261110T150000Z", "RRULE:FREQ=DAILY", "EXDATE:20261112T150000Z", "SUMMARY:Walk"),
            vevent("UID:d", "RECURRENCE-ID:20261113T150000Z", "DTSTART:20261113T170000Z", "SUMMARY:Walk (moved)"),
            vevent("UID:d", "RECURRENCE-ID:20261114T150000Z", "DTSTART:20261114T150000Z", "STATUS:CANCELLED", "SUMMARY:Walk"))
        self.assertEqual(titles(cal, date(2026, 11, 11)), [("Walk", "10:00")])
        self.assertEqual(titles(cal, date(2026, 11, 12)), [])
        self.assertEqual(titles(cal, date(2026, 11, 13)), [("Walk (moved)", "12:00")])
        self.assertEqual(titles(cal, date(2026, 11, 14)), [])
        self.assertEqual(titles(cal, date(2026, 11, 15)), [("Walk", "10:00")])

    def test_unsupported_rules_cancelled_alarms_and_bad_input(self):
        cal = calendar(
            vevent("UID:m", "DTSTART:20261103T180000Z", "RRULE:FREQ=MONTHLY;BYDAY=1TU", "SUMMARY:Birthday party"),
            vevent("UID:c", "DTSTART:20261113T180000Z", "STATUS:CANCELLED", "SUMMARY:Cancelled"),
            vevent("UID:a", "DTSTART:20261113T200000Z", "SUMMARY:Choir", "BEGIN:VALARM", "TRIGGER:-PT15M",
                   "DESCRIPTION:Alarm", "END:VALARM"),
            vevent("UID:x", "DTSTART:not-a-date", "SUMMARY:Broken"))
        self.assertEqual(cal.unsupported, 1)
        self.assertEqual(cal.skipped, 2)
        self.assertEqual(titles(cal, date(2026, 11, 3)), [("Birthday party", "13:00")])
        self.assertEqual(titles(cal, date(2026, 12, 1)), [])  # Only the first date: documented.
        self.assertEqual(titles(cal, date(2026, 11, 13)), [("Choir", "15:00")])
        with self.assertRaises(ValueError):
            ics.parse("date,time,title\n", NY)

    def test_duration_and_unknown_tzid_falls_back_to_local(self):
        cal = calendar(vevent("UID:1", "DTSTART;TZID=Custom/Facility:20261113T090000", "DURATION:PT1H30M",
                              "SUMMARY:Coffee"))
        item = cal.on(date(2026, 11, 13), NY)[0]
        self.assertEqual((item["time"], item["ends_at"]), ("09:00", "2026-11-13T10:30-05:00"))


class CsvTests(unittest.TestCase):
    MENU = ("﻿Date,Meal,Items\n"
            "Monday,Lunch,Tomato soup; grilled cheese\n"
            "11/16/2026,Lunch,Thanksgiving turkey\n"
            "Monday,Breakfast,Oatmeal\n"
            "2026-11-16,Dinner,\n"
            "someday,Lunch,Nothing\n")

    def test_menu_weekday_rows_dated_overrides_order_and_skips(self):
        menu = sources.parse_menu(self.MENU)
        self.assertEqual(menu.skipped, 2)
        self.assertEqual(menu.on(date(2026, 11, 9)), [{"meal": "breakfast", "items": "Oatmeal"},
                                                     {"meal": "lunch", "items": "Tomato soup; grilled cheese"}])
        self.assertEqual(menu.on(date(2026, 11, 16)), [{"meal": "breakfast", "items": "Oatmeal"},
                                                      {"meal": "lunch", "items": "Thanksgiving turkey"}])
        self.assertEqual(menu.on(date(2026, 11, 10)), [])
        with self.assertRaises(ValueError):
            sources.parse_menu("day,dish\nfoo,bar\n")
        with self.assertRaises(ValueError):
            sources.parse_menu("date,meal,items\n")

    def test_activity_times_and_loose_headers(self):
        table = sources.parse_activities_csv(
            "Day,Start Time,Activity,Room\n2026-11-13,2pm,Bingo,Lounge\n2026-11-13,10:30 a.m.,Chair yoga,Gym\n"
            "2026-11-13,,Craft fair,Hall\n2026-11-13,2,Ambiguous,Hall\n2026-11-13,13:00 pm,Bad,Hall\n")
        self.assertEqual(table.skipped, 2)
        self.assertEqual([(i["title"], i["time"]) for i in table.on(date(2026, 11, 13), NY)],
                         [("Craft fair", None), ("Chair yoga", "10:30"), ("Bingo", "14:00")])
        self.assertEqual(sources.parse_time("12 am"), "00:00")
        self.assertEqual(sources.parse_time("12:15 PM"), "12:15")

    def test_activities_sniff_ics_or_csv(self):
        self.assertIsInstance(sources.parse("activities", "﻿BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261113T190000Z\r\n"
                                            "SUMMARY:A\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n", NY), ics.Calendar)
        self.assertIsInstance(sources.parse("activities", "date,title\nmonday,Walk\n", NY), sources.Table)
        with self.assertRaises(ValueError):
            sources.parse("activities", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", NY)


class Clock:
    def __init__(self, at):
        self.now = datetime.fromisoformat(at).astimezone().timestamp()

    def __call__(self):
        return self.now


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.clock = Clock("2026-11-13T08:00:00")
        self.sources = Sources(self.root / "facility", self.clock)
        self.file = self.root / "menu.csv"
        self.file.write_text("date,meal,items\nfriday,lunch,Fish\n")

    def test_failed_refresh_keeps_the_last_good_copy(self):
        with patch.dict(os.environ, {"EMBER_FACILITY_MENU_CSV": str(self.file)}):
            menu, meta = self.sources.load("menu", NY)
            self.assertEqual(menu.on(date(2026, 11, 13)), [{"meal": "lunch", "items": "Fish"}])
            self.assertIsNone(meta["error"])
            self.file.write_text("garbage")
            self.clock.now += 3601
            menu, meta = self.sources.load("menu", NY)
            self.assertEqual(menu.on(date(2026, 11, 13)), [{"meal": "lunch", "items": "Fish"}])
            self.assertIn("could not refresh", meta["error"])
            self.file.write_text("date,meal,items\nfriday,lunch,Chicken\n")
            self.clock.now += 60  # Not due yet: still the cached copy.
            self.assertEqual(self.sources.load("menu", NY)[0].on(date(2026, 11, 13))[0]["items"], "Fish")
            self.clock.now += 3600
            self.assertEqual(self.sources.load("menu", NY)[0].on(date(2026, 11, 13))[0]["items"], "Chicken")

    def test_url_fetch_hides_tokens_and_upload_wins(self):
        url = "webcal://calendar.example.org/feed.ics?token=SECRET"
        body = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261113T190000Z\r\nSUMMARY:Bingo\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with patch.dict(os.environ, {"EMBER_FACILITY_CALENDAR_URL": url}), \
                patch.object(sources, "read_source", return_value=body) as fetch:
            calendar, meta = self.sources.load("activities", NY)
            self.assertEqual(fetch.call_args[0][0], url)
            self.assertEqual(meta["source"], "webcal://calendar.example.org/feed.ics")
            self.assertNotIn("SECRET", str(self.sources.status(NY)))
            self.sources.save_upload("activities", "date,time,title\n2026-11-13,15:00,Choir\n", NY)
            self.assertEqual([i["title"] for i in self.sources.load("activities", NY)[0].on(date(2026, 11, 13), NY)],
                             ["Choir"])
            with self.assertRaises(ValueError):
                self.sources.save_upload("activities", "not a calendar", NY)
            self.assertTrue(self.sources.remove_upload("activities"))
            self.assertEqual(self.sources.configured("activities"), ("env", url))

    def test_nothing_configured(self):
        with patch.dict(os.environ, {"EMBER_FACILITY_MENU_CSV": ""}):
            self.assertEqual(self.sources.load("menu", NY), (None, {}))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.clock = Clock("2026-11-13T08:00:00")  # A Friday.
        self.log = CareLog(root / "care.sqlite3", self.clock)
        self.reminders = Store(root / "everyday.sqlite3", self.clock)
        self.sources = Sources(root / "facility", self.clock)
        for p in (patch.dict(os.environ, ON), patch.object(care_store, "log", self.log),
                  patch.object(care_store, "forward"), patch.object(tools, "clock", self.clock),
                  patch.object(tools, "sources", self.sources), patch.object(everyday, "store", self.reminders)):
            p.start()
            self.addCleanup(p.stop)
        self.sources.save_upload("activities", "date,time,title,location\nfriday,14:00,Bingo,Lounge\n"
                                 "friday,14:00,Bingo for beginners,Room 2\n2026-11-13,08:10,Coffee social,Cafe\n"
                                 "saturday,10:00,Church bus,Front door\n", tools.local_zone())

    def test_day_selection(self):
        self.assertEqual(tools.resolve_day(None), date(2026, 11, 13))
        self.assertEqual(tools.resolve_day("Tomorrow"), date(2026, 11, 14))
        self.assertEqual(tools.resolve_day("friday"), date(2026, 11, 13))
        self.assertEqual(tools.resolve_day("monday"), date(2026, 11, 16))
        self.assertEqual(tools.resolve_day("2026-11-20"), date(2026, 11, 20))
        for bad in ("next blue moon", "2027-06-01", 5):
            with self.assertRaises(ValueError):
                tools.resolve_day(bad)

    def test_facility_info_answers_only_from_the_source(self):
        result = tools.execute("facility_info", {"topic": "activities", "day": "tomorrow"})
        self.assertEqual([(i["title"], i["time"], i["location"]) for i in result["items"]],
                         [("Church bus", "10:00", "Front door")])
        self.assertEqual(result["weekday"], "Saturday")
        menu = tools.execute("facility_info", {"topic": "menu"})
        self.assertFalse(menu["available"])
        self.assertIn("front desk", menu["note"])
        self.assertIn("error", tools.execute("facility_info", {"topic": "weather"}))
        self.assertIn("error", tools.execute("facility_info", {"topic": "menu", "extra": 1}))
        self.assertIn("Don't guess", tools.execute("facility_info", {"topic": "activities", "day": "sunday"})["note"])

    def test_remind_activity_uses_the_everyday_reminder_store(self):
        partial = tools.execute("remind_activity", {"title": "bingo for"})
        self.assertTrue(partial["saved"])
        self.assertEqual(partial["activity"], "Bingo for beginners")
        result = tools.execute("remind_activity", {"title": "Bingo", "minutes_before": 30})
        self.assertEqual((result["saved"], result["time"], result["remind_at"]), (True, "14:00", "13:30"))
        pending = self.reminders.reminder("list")["reminders"]
        self.assertEqual([r["text"] for r in pending], ["Bingo starts at 14:00 in Lounge",
                                                        "Bingo for beginners starts at 14:00 in Room 2"])
        self.assertTrue(pending[0]["due_at"].startswith("2026-11-13T13:30"))
        events = self.log.events(audience="staff")
        self.assertEqual([(e["kind"], e["status"]) for e in events], [("activity", "info")] * 2)
        self.assertEqual(events[1]["data"]["title"], "Bingo")
        self.assertIn("too soon", tools.execute("remind_activity", {"title": "Coffee social"})["note"])
        self.assertFalse(tools.execute("remind_activity", {"title": "Poker"})["saved"])
        self.assertIn("error", tools.execute("remind_activity", {"title": "Bingo", "minutes_before": 500}))
        with patch.dict(os.environ, {"EMBER_EVERYDAY_ENABLED": "0"}):
            self.assertIn("error", tools.execute("remind_activity", {"title": "Bingo"}))

    def test_remind_activity_asks_when_two_match_equally(self):
        self.sources.save_upload("activities", "date,time,title\nfriday,14:00,Bingo\nfriday,19:00,Bingo\n",
                                 tools.local_zone())
        result = tools.execute("remind_activity", {"title": "bingo"})
        self.assertFalse(result["saved"])
        self.assertEqual([c["time"] for c in result["choices"]], ["14:00", "19:00"])

    def test_staff_request_routes(self):
        expected = {"maintenance": ("request", "maintenance"), "housekeeping": ("request", "housekeeping"),
                    "front_desk": ("request", "the front desk"), "dining": ("request", "the dining team"),
                    "care_aide": ("help_request", "the care staff")}
        for category, (kind, told) in expected.items():
            result = tools.execute("staff_request", {"category": category, "details": "Bathroom light is out"})
            self.assertTrue(result["logged"])
            self.assertEqual(result["tell_resident"], f"I've let {told} know. I can't say exactly when they'll come.")
            event = self.log.events(audience="ember")[-1]
            self.assertEqual((event["kind"], event["status"], event["category"]), (kind, "open", category))
            self.assertEqual(event["data"], {"route": category, "urgent": False, "source": "staff_request"})
            self.assertNotIn("quote", event)
        for bad in ({"category": "plumbing", "details": "Leak"}, {"category": "dining", "details": "x"},
                    {"category": "dining"}, {"category": "dining", "details": "Cold soup", "urgent": "yes"}):
            self.assertEqual(tools.execute("staff_request", bad).get("logged"), None)
        self.assertEqual(len(self.log.events(audience="ember")), 5)

    def test_emergency_tells_them_to_use_the_pendant_and_still_logs(self):
        result = tools.execute("staff_request", {"category": "maintenance", "details": "Fell and hurt my hip", "urgent": True})
        self.assertTrue(result["emergency"])
        self.assertIn("pendant", result["tell_resident"])
        self.assertIn("not an emergency system", result["tell_resident"])
        event = self.log.events()[-1]
        self.assertEqual((event["kind"], event["status"], event["data"]["urgent"], event["data"]["route"]),
                         ("help_request", "open", True, "care_aide"))
        self.assertTrue(event["summary"].startswith("URGENT: "))
        with patch.dict(os.environ, {"EMBER_EMERGENCY_WORDING": "Press the red call button by your bed now."}):
            self.assertEqual(tools.execute("staff_request", {"category": "care_aide", "details": "Can't breathe",
                                                             "urgent": True})["tell_resident"],
                             "Press the red call button by your bed now.")
        with patch.object(care_store.log, "record", side_effect=OSError):  # Logging fails: still the safety wording.
            failed = tools.execute("staff_request", {"category": "care_aide", "details": "Chest pain", "urgent": True})
            self.assertEqual((failed["logged"], failed["emergency"]), (False, True))
            plain = tools.execute("staff_request", {"category": "housekeeping", "details": "Towels please"})
            self.assertFalse(plain["logged"])
            self.assertIn("call button", plain["tell_resident"])

    def test_care_off_tools_absent_and_inert(self):
        from features.toolkits import gemini_tools
        def names(config):
            return {d["name"] for t in config.get("tools", []) for d in t.get("function_declarations", [])}
        on = gemini_tools.extend_config({"system_instruction": "base"})
        self.assertTrue(tools.NAMES <= names(on))
        self.assertIn("[FACILITY TOOLS]", on["system_instruction"])
        self.assertTrue(gemini_tools.execute("staff_request", {"category": "dining", "details": "Cold soup"})["logged"])
        for env in ({"EMBER_CARE_ENABLED": "0"}, {"EMBER_FACILITY_ENABLED": "0"}):
            with patch.dict(os.environ, env):
                off = gemini_tools.extend_config({"system_instruction": "base"})
                self.assertFalse(tools.NAMES & names(off))
                self.assertNotIn("[FACILITY TOOLS]", off["system_instruction"])
                self.assertIn("error", gemini_tools.execute("staff_request", {"category": "dining", "details": "Cold soup"}))
        self.assertEqual(len(self.log.events()), 1)


class RouteEmailTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.clock = Clock("2026-11-13T08:00:00")
        self.log = CareLog(root / "care.sqlite3", self.clock)
        self.sent = []
        for p in (patch.dict(os.environ, {**ON, "EMBER_ROUTE_EMAIL_MAINTENANCE": "workorders@example.org",
                                          "EMBER_ROUTE_EMAIL_CARE_AIDE": "aides@example.org"}),
                  patch.object(care_store, "forward"), patch.object(dashboard, "outbox", dashboard.Outbox(root / "out.sqlite3"))):
            p.start()
            self.addCleanup(p.stop)

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def run_once(self, send=None):
        return dashboard.send_route_emails(self.log, {"name": "Apt 214"}, send or self.send, self.clock)

    def test_each_open_routed_request_is_emailed_once_without_quotes(self):
        light = self.log.record("request", "Maintenance: Bathroom light is out", status="open", category="maintenance",
                                quote="My bathroom light is out, dear", data={"route": "maintenance", "urgent": False})
        self.log.record("request", "Housekeeping: Towels", status="open", data={"route": "housekeeping"})  # No address.
        self.log.record("request", "Private thing", status="open", private=True, data={"route": "maintenance"})
        self.log.record("help_request", "URGENT: Care aide: Fell", status="open", data={"route": "care_aide", "urgent": True})
        handled = self.log.record("request", "Maintenance: Leak", status="open", data={"route": "maintenance"})
        self.log.handle(handled, "maria")
        self.assertEqual(self.run_once(), 2)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual([to for to, _, _ in self.sent], ["workorders@example.org", "aides@example.org"])
        to, subject, body = self.sent[0]
        self.assertEqual(subject, "Ember request (Maintenance): Apt 214")
        self.assertIn("Bathroom light is out", body)
        self.assertIn(f"#{light}", body)
        self.assertNotIn("dear", body)
        self.assertTrue(self.sent[1][1].startswith("URGENT "))
        self.assertIn("pendant or pull cord", self.sent[1][2])

    def test_failures_retry_then_stop_and_old_history_is_skipped(self):
        self.log.record("request", "Maintenance: Old", status="open", data={"route": "maintenance"})
        self.clock.now += 2 * 86400
        self.log.record("request", "Maintenance: Door sticks", status="open", data={"route": "maintenance"})
        def broken(*_):
            raise OSError("smtp down")
        for _ in range(dashboard.MAX_ATTEMPTS + 2):
            self.assertEqual(self.run_once(broken), 0)
        self.assertEqual(self.run_once(), 0)  # Gave up after MAX_ATTEMPTS; no flood later.
        self.log.record("request", "Maintenance: Window", status="open", data={"route": "maintenance"})
        self.assertEqual(self.run_once(), 1)
        self.assertEqual([s[2].count("Window") for s in self.sent], [1])

    def test_retry_succeeds_after_one_failure(self):
        self.log.record("request", "Maintenance: Door sticks", status="open", data={"route": "maintenance"})
        self.assertEqual(self.run_once(lambda *_: (_ for _ in ()).throw(OSError())), 0)
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.run_once(), 0)

    def test_nothing_sent_when_off_or_smtp_not_configured(self):
        self.log.record("request", "Maintenance: Door", status="open", data={"route": "maintenance"})
        with patch.dict(os.environ, {"EMBER_CARE_ENABLED": "0"}):
            self.assertEqual(self.run_once(), 0)
        with patch.dict(os.environ, {"BAYMAX_EMAIL_FROM": "", "BAYMAX_EMAIL_PASSWORD": ""}), \
                patch.object(dashboard, "smtp_send") as smtp:
            self.assertEqual(dashboard.send_route_emails(self.log, {}, None, self.clock), 0)
            smtp.assert_not_called()
        self.assertEqual(self.run_once(), 1)  # Still pending, so it goes once email works.


class UploadRouteTests(unittest.TestCase):
    def setUp(self):
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"FLASK_SECRET_KEY": "test"}):
            import baymax_app
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.users = [{"id": "1", "username": "maria", "email": "nurse@example.com", "devices": [baymax_app.DEVICE_SERIAL]},
                      {"id": "2", "username": "son", "email": "son@example.com", "devices": [baymax_app.DEVICE_SERIAL]}]
        for p in (patch.object(baymax_app, "_load_users", lambda: self.users), patch.dict(os.environ, ON),
                  patch.object(dashboard, "sources", Sources(Path(self.dir.name) / "facility"))):
            p.start()
            self.addCleanup(p.stop)
        self.client = baymax_app.app.test_client()

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def test_staff_only_upload_validation_and_page(self):
        menu = b"date,meal,items\nfriday,lunch,Fish and chips\n"
        self.assertEqual(self.client.post("/api/facility/upload/menu", data=menu).status_code, 401)
        self.login("2")
        self.assertEqual(self.client.post("/api/facility/upload/menu", data=menu).status_code, 403)
        self.assertEqual(self.client.get("/care/facility").status_code, 403)
        self.login("1")
        with patch.dict(os.environ, {"EMBER_CARE_ENABLED": "0"}):
            self.assertEqual(self.client.post("/api/facility/upload/menu", data=menu).status_code, 404)
        bad = self.client.post("/api/facility/upload/menu", data=b"nonsense")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.post("/api/facility/upload/other", data=menu).status_code, 404)
        self.assertEqual(self.client.post("/api/facility/upload/menu", data=menu).get_json(),
                         {"saved": True, "rows": 1, "skipped": 0})
        import io
        form = self.client.post("/api/facility/upload/activities", content_type="multipart/form-data",
                                data={"from_page": "1", "file": (io.BytesIO(b"date,time,title\nfriday,2pm,Bingo\n"), "a.csv")})
        self.assertEqual(form.status_code, 302)
        status = self.client.get("/api/facility/status").get_json()
        self.assertEqual((status["sources"]["menu"]["configured"], status["sources"]["activities"]["rows"]), ("upload", 1))
        self.assertEqual(status["routes"]["maintenance"], False)
        page = self.client.get("/care/facility")
        self.assertIn(b"Menu and activity calendar", page.data)
        self.assertIn(b"Remove upload", page.data)
        self.assertEqual(self.client.delete("/api/facility/upload/menu").get_json(), {"removed": True})


if __name__ == "__main__":
    unittest.main()
