"""Documentation drafts, fall timelines, falls reported to the dashboard and the weekly family update (offline)."""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from features.care import drafts as drafts_module, drafts_dashboard, family_digest, store
from features.care.drafts import DraftError, Drafts
from features.care.store import CareLog

ON = {"EMBER_CARE_ENABLED": "1", "EMBER_STAFF_EMAILS": "nurse@example.com", "EMBER_CARE_RESIDENT_NAME": "Rose"}


class Clock:
    def __init__(self, at):
        self.now = datetime.fromisoformat(at).astimezone().timestamp()

    def __call__(self):
        return self.now

    def at(self, value):
        self.now = datetime.fromisoformat(value).astimezone().timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock("2026-11-12T08:00:00")
        self.log = CareLog(Path(self.dir.name) / "care.sqlite3", self.clock)
        self.drafts = Drafts(self.log)
        forward, env = patch.object(store, "forward"), patch.dict(os.environ, ON)
        self.forward = forward.start()
        env.start()
        self.addCleanup(forward.stop)
        self.addCleanup(env.stop)

    def day(self):
        """Fixture day, Thursday 12 November 2026."""
        c = self.clock
        c.at("2026-11-12T08:00:00"); self.log.record("check_in", "Morning check-in fine", status="ok")
        c.at("2026-11-12T09:15:00")
        towels = self.log.record("request", "Fresh towels", status="open", quote="Towels please", category="housekeeping")
        c.at("2026-11-12T09:45:00"); self.log.handle(towels, "Maria")
        c.at("2026-11-12T11:00:00"); self.log.record("request", "Bathroom light out", status="open", category="maintenance")
        c.at("2026-11-12T14:00:00"); self.log.record("activity", "Joined bingo")
        c.at("2026-11-12T15:00:00"); self.log.record("activity", "Enjoyed the music hour")
        c.at("2026-11-12T12:00:00"); self.log.record("meal", "Pre-ordered the fish for dinner")
        c.at("2026-11-12T16:00:00"); self.log.record("note", "Talked about her son", private=True)
        c.at("2026-11-12T23:00:00"); self.log.record("night_wake", "Up and about at 11pm")
        c.at("2026-11-13T07:30:00"); self.log.record("activity", "Next day's chair yoga")


class TemplateTests(Base):
    def test_daily_note_sections_handling_and_sources_without_private_or_quotes(self):
        self.day()
        note, activities, service = self.drafts.generate_day(date(2026, 11, 12))
        body = note["body"]
        self.assertTrue(body.startswith("Daily note (non-clinical)\nResident/unit: Rose\nDate: Thursday 12 November 2026"))
        self.assertIn("Not a clinical record", body)
        self.assertIn("Overview: 1 check-in, 2 requests (1 handled), 2 activities.", body)
        self.assertIn("- 09:15 Fresh towels (housekeeping); handled by Maria at Thu 12 Nov 09:45 [#2]", body)
        self.assertIn("- 11:00 Bathroom light out (maintenance); still open [#3]", body)
        self.assertIn("Meals\n- 12:00 Pre-ordered the fish for dinner [#6]", body)
        self.assertIn("Night activity\n- 23:00 Up and about at 11pm [#8]", body)
        for absent in ("Towels please", "Talked about her son", "chair yoga"):
            self.assertNotIn(absent, body)
        self.assertEqual(note["source_event_ids"], [1, 2, 3, 6, 4, 5, 8])  # In time order.
        self.assertEqual((note["state"], note["version"], note["key"]), ("draft", 1, "daily_note:2026-11-12"))

        self.assertIn("2 entries:\n- 14:00 Joined bingo [#4]\n- 15:00 Enjoyed the music hour [#5]", activities["body"])
        self.assertEqual(activities["source_event_ids"], [4, 5])
        self.assertIn("09:15 | Fresh towels (housekeeping) | handled by Maria at Thu 12 Nov 09:45 (30 min) [#2]", service["body"])
        self.assertIn("11:00 | Bathroom light out (maintenance) | still open [#3]", service["body"])
        self.assertIn("1 of 2 handled.", service["body"])

    def test_empty_day(self):
        [note, activities, service] = self.drafts.generate_day(date(2026, 11, 10))
        self.assertIn("No events were logged for this day.", note["body"])
        self.assertIn("No activities", activities["body"])
        self.assertEqual(service["source_event_ids"], [])

    def test_fall_timeline_follows_up_for_72_hours_and_updates_until_edited(self):
        self.clock.at("2026-11-12T10:00:00")
        fall = self.log.record("fall", "Possible fall detected by Ember's camera", status="attention",
                               data={"source": "fall detection toolkit (camera, on the robot)"})
        self.clock.at("2026-11-12T10:00:05")
        self.log.record("note", "Ember asked the resident whether they were okay after the possible fall",
                        data={"fall_event_id": fall})
        self.log.record("note", "Unrelated note")
        self.clock.at("2026-11-12T10:12:00"); self.log.handle(fall, "Maria")
        first = self.drafts.generate_fall(fall)
        self.assertIn("- Thu 12 Nov 10:00 Fall logged: Possible fall detected by Ember's camera "
                      "(source: fall detection toolkit (camera, on the robot)) [#1]", first["body"])
        self.assertIn("- Thu 12 Nov 10:00 Ember: Ember asked the resident whether they were okay", first["body"])
        self.assertIn("- Thu 12 Nov 10:12 Marked handled by staff (Maria) [#1]", first["body"])
        self.assertIn("72-hour follow-up window is still open", first["body"])
        self.assertNotIn("Unrelated", first["body"])
        self.clock.at("2026-11-13T08:00:00"); self.log.record("check_in", "Morning check-in fine", status="ok")
        self.clock.at("2026-11-16T08:00:00"); self.log.record("check_in", "Outside the window", status="ok")
        updated = self.drafts.generate_fall(fall)
        self.assertEqual((updated["id"], updated["version"]), (first["id"], 1))
        self.assertIn("- Fri 13 Nov 08:00 Check-in: Morning check-in fine [#4]", updated["body"])
        self.assertNotIn("Outside the window", updated["body"])
        self.assertNotIn("still open", updated["body"])
        self.assertEqual(updated["source_event_ids"], [1, 2, 4])
        self.drafts.edit(first["id"], updated["body"] + "\nStaff: resident seen, fine.", "Maria")
        self.clock.at("2026-11-12T12:00:00"); self.log.record("check_in", "Late check-in", status="ok")
        self.assertIn("Staff: resident seen", self.drafts.generate_fall(fall)["body"])  # Edits are never overwritten.
        with self.assertRaises(DraftError):
            self.drafts.generate_fall(999)


class VersioningTests(Base):
    def test_draft_edited_signed_is_final_and_edits_start_a_new_version(self):
        self.day()
        note = self.drafts.generate_day(date(2026, 11, 12), kinds=["daily_note"])[0]
        edited = self.drafts.edit(note["id"], note["body"] + "\nStaff addition.", "Maria")
        self.assertEqual((edited["id"], edited["state"], edited["edited_by"]), (note["id"], "edited", "Maria"))
        signed = self.drafts.sign(note["id"], "Maria Lopez, LPN", "nurse@example.com")
        self.assertEqual((signed["state"], signed["signed_by"]), ("signed", "Maria Lopez, LPN"))
        with self.assertRaises(DraftError):
            self.drafts.sign(note["id"], "Someone else")
        # Regeneration leaves a signed draft alone.
        self.assertEqual(self.drafts.generate_day(date(2026, 11, 12), kinds=["daily_note"])[0]["body"], signed["body"])
        # The database itself refuses changes to a signed row.
        with sqlite3.connect(self.log.path) as raw, self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE care_drafts SET body='tampered' WHERE id=?", (note["id"],))
        with sqlite3.connect(self.log.path) as raw, self.assertRaises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM care_drafts WHERE id=?", (note["id"],))
        second = self.drafts.edit(note["id"], "Corrected text", "Maria")
        self.assertEqual((second["version"], second["state"], second["supersedes"]), (2, "edited", note["id"]))
        self.assertEqual(self.drafts.get(note["id"])["body"], signed["body"])
        with self.assertRaises(DraftError):
            self.drafts.edit(note["id"], "Edit an old version", "Maria")
        self.assertEqual([d["version"] for d in self.drafts.list()], [2])
        self.assertEqual([d["version"] for d in self.drafts.list(all_versions=True)], [2, 1])
        with self.assertRaises(ValueError):
            self.drafts.edit(second["id"], "  ", "Maria")
        with self.assertRaises(KeyError):
            self.drafts.get(999)

    def test_every_change_is_forwarded_once_to_the_facility_page(self):
        self.day()
        self.forward.reset_mock()
        note = self.drafts.generate_day(date(2026, 11, 12), kinds=["daily_note"])[0]
        self.drafts.generate_day(date(2026, 11, 12), kinds=["daily_note"])  # Unchanged: nothing re-sent.
        self.drafts.edit(note["id"], "Edited", "Maria")
        self.drafts.sign(note["id"], "Maria Lopez")
        sent = [c.args for c in self.forward.call_args_list]
        self.assertEqual([endpoint for endpoint, _ in sent], ["/api/device/care-draft"] * 3)
        self.assertEqual([payload["state"] for _, payload in sent], ["draft", "edited", "signed"])
        payload = sent[-1][1]
        self.assertEqual(set(payload), {"id", "key", "version", "kind", "title", "state", "period_start", "period_end",
                                        "body", "source_event_ids", "generated_at", "body_sha256", "supersedes",
                                        "edited_by", "edited_at", "signed_by", "signed_at"})
        self.assertEqual(payload["signed_by"], "Maria Lopez")


class ScheduleTests(Base):
    def test_morning_drafts_once_a_day_and_fall_timelines_every_tick(self):
        self.day()
        self.clock.at("2026-11-13T07:40:00")
        fall = self.log.record("fall", "Possible fall", status="attention")
        early = datetime(2026, 11, 13, 6, 59).astimezone()
        self.assertFalse(drafts_module.run_schedule(self.drafts, early))
        self.assertEqual([d["kind"] for d in self.drafts.list()], ["fall_timeline"])
        morning = datetime(2026, 11, 13, 7, 5).astimezone()
        self.assertTrue(drafts_module.run_schedule(self.drafts, morning))
        self.assertFalse(drafts_module.run_schedule(self.drafts, morning))
        self.assertEqual(sorted(d["key"] for d in self.drafts.list()),
                         ["activity_log:2026-11-12", "daily_note:2026-11-12", f"fall_timeline:{fall}",
                          "service_log:2026-11-12"])
        with patch.dict(os.environ, {"EMBER_CARE_DRAFTS": "0"}):
            self.assertFalse(drafts_module.run_schedule(self.drafts, datetime(2026, 11, 14, 8, 0).astimezone()))


class ExportTests(Base):
    def test_plain_text_marks_unsigned_and_signed_with_fingerprint(self):
        self.day()
        note = self.drafts.generate_day(date(2026, 11, 12), kinds=["daily_note"])[0]
        text = drafts_module.export_text(note, {"name": "Apt 214"})
        self.assertIn("UNSIGNED DRAFT", text)
        self.assertIn("Ember unit Apt 214", text)
        signed = self.drafts.sign(note["id"], "Maria Lopez, LPN")
        text = drafts_module.export_text(signed, {"name": "Apt 214"})
        self.assertIn("Signed by Maria Lopez, LPN on 13 November 2026 07:30", text)
        self.assertIn(signed["body_sha256"], text)
        self.assertNotIn("UNSIGNED", text)


class FamilyDigestTests(Base):
    def setUp(self):
        super().setUp()
        self.hooks = {"send_email": Mock()}
        self.sunday = datetime(2026, 11, 15, 18, 30).astimezone()

    def send(self, now=None, family=("son@example.com",), log=None):
        with patch.dict(os.environ, {"EMBER_CARE_FAMILY_DIGEST": "sun 18:00"}):
            return family_digest.send_if_due(self.hooks, lambda: list(family), now or self.sunday, log or self.log)

    def test_nothing_without_consent_then_only_consented_kinds_without_quotes_or_private(self):
        self.day()
        self.log.record("activity", "Joined bingo", quote="I love bingo")
        self.log.record("activity", "Private chat about money", private=True)
        self.clock.at("2026-11-14T10:00:00")
        self.log.record("request", "Towels", status="open")
        self.assertFalse(self.send())
        self.hooks["send_email"].assert_not_called()
        self.log.set_family_kinds(["activity", "meal"], "Maria")
        self.assertTrue(self.send())
        recipients, subject, body, kind = self.hooks["send_email"].call_args.args
        self.assertEqual((recipients, subject, kind), (["son@example.com"], "A note about Rose's week", "family update"))
        self.assertIn("Activities:\n- Joined bingo (twice)\n- Enjoyed the music hour\n- Next day's chair yoga", body)
        self.assertIn("Meals:\n- Pre-ordered the fish for dinner", body)
        for absent in ("I love bingo", "Private chat", "Towels", "Bathroom", "Talked about her son", "night"):
            self.assertNotIn(absent, body)
        self.assertIn("not a medical device", body)

    def test_once_per_week_across_restarts_and_catch_up_later_in_the_week(self):
        self.day()
        self.log.set_family_kinds(["activity"], "Maria")
        self.assertFalse(self.send(datetime(2026, 11, 15, 17, 59).astimezone()))
        self.assertTrue(self.send())
        self.assertFalse(self.send())
        # A restarted dashboard reads the sent week back from the care log's settings.
        self.assertFalse(self.send(datetime(2026, 11, 15, 23, 0).astimezone(), log=CareLog(self.log.path, self.clock)))
        self.assertEqual(self.log.setting("family_digest_week"), "2026-W46")
        self.assertEqual(self.hooks["send_email"].call_count, 1)
        self.assertTrue(family_digest.due(datetime(2026, 11, 17, 9, 0).astimezone(), "2026-W46", ("mon", 9, 0)))

    def test_off_without_schedule_or_family_and_counts_for_sensitive_kinds(self):
        self.day()
        self.log.set_family_kinds(["check_in", "night_wake"], "Maria")
        self.assertFalse(family_digest.send_if_due(self.hooks, lambda: ["son@example.com"], self.sunday, self.log))
        self.assertFalse(self.send(family=()))
        self.assertTrue(self.send())
        body = self.hooks["send_email"].call_args.args[2]
        self.assertIn("This week Rose chatted with Ember at 1 daily check-in; was up during the night 1 time.", body)
        self.assertNotIn("Up and about", body)
        with patch.dict(os.environ, {"EMBER_CARE_FAMILY_DIGEST": "someday"}), patch("builtins.print"):
            self.assertIsNone(family_digest.schedule())


class RobotRouteTests(unittest.TestCase):
    def setUp(self):
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"FLASK_SECRET_KEY": "test"}):
            import baymax_app
        self.app = baymax_app
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = CareLog(Path(self.dir.name) / "care.sqlite3")
        self.users = [{"id": "1", "username": "maria", "email": "nurse@example.com", "devices": [baymax_app.DEVICE_SERIAL]},
                      {"id": "2", "username": "son", "email": "son@example.com", "devices": [baymax_app.DEVICE_SERIAL]}]
        self.forward = Mock()
        for p in (patch.object(store, "log", self.log), patch.object(store, "forward", self.forward),
                  patch.object(baymax_app, "_load_users", lambda: self.users), patch.dict(os.environ, ON),
                  patch.object(baymax_app, "FALL_LOG_PATH", str(Path(self.dir.name) / "fall_log.json")),
                  patch.object(baymax_app, "_send_alert_email")):
            p.start()
            self.addCleanup(p.stop)
        self.client = baymax_app.app.test_client()

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def test_fall_post_is_recorded_once_and_only_when_facility_mode_is_on(self):
        with patch.dict(os.environ, {"EMBER_CARE_ENABLED": "0"}):
            self.assertEqual(self.client.post("/api/fall", json={"trunk_angle": 80}).status_code, 201)
        self.assertEqual(self.log.events(), [])
        self.assertEqual(self.client.post("/api/fall", json={"trunk_angle": 80}).status_code, 201)
        [fall] = self.log.events()
        self.assertEqual((fall["kind"], fall["status"]), ("fall", "attention"))
        self.assertEqual(fall["data"]["source"], "camera (runtime report to the dashboard)")
        self.assertNotIn("trunk_angle", fall["data"])

    def test_drafts_are_staff_only_and_export_cleanly(self):
        self.log.record("activity", "Joined bingo")
        self.login("2")
        for method, url in (("get", "/api/care/drafts"), ("post", "/api/care/drafts/generate"), ("get", "/care/drafts"),
                            ("get", "/api/care/drafts/1/export.txt"), ("post", "/api/care/drafts/1/sign")):
            self.assertEqual(getattr(self.client, method)(url).status_code, 403, url)
        self.login("1")
        today = datetime.now().date().isoformat()
        made = self.client.post("/api/care/drafts/generate", json={"day": today}).get_json()
        self.assertEqual([d["kind"] for d in made], ["daily_note", "activity_log", "service_log"])
        self.assertEqual(self.client.post("/api/care/drafts/generate", json={"day": "bad"}).status_code, 400)
        draft_id = made[0]["id"]
        view = self.client.get(f"/api/care/drafts/{draft_id}").get_json()
        self.assertEqual([e["summary"] for e in view["sources"]], ["Joined bingo"])
        self.assertEqual(self.client.post(f"/api/care/drafts/{draft_id}/edit", json={"body": "<b>Edited</b>"}).get_json()["edited_by"], "maria")
        signed = self.client.post(f"/api/care/drafts/{draft_id}/sign", json={"name": "Maria Lopez, LPN"}).get_json()
        self.assertEqual(signed["signed_by"], "Maria Lopez, LPN")
        self.assertEqual(self.client.post(f"/api/care/drafts/{draft_id}/sign").status_code, 409)
        text = self.client.get(f"/api/care/drafts/{draft_id}/export.txt")
        self.assertEqual(text.mimetype, "text/plain")
        self.assertIn(f"daily_note-{today}-v1.txt", text.headers["Content-Disposition"])
        self.assertIn(b"Signed by Maria Lopez, LPN", text.data)
        printable = self.client.get(f"/api/care/drafts/{draft_id}/print").data
        self.assertIn(b"&lt;b&gt;Edited&lt;/b&gt;", printable)
        self.assertNotIn(b"<script", printable)
        self.assertEqual(self.client.get("/api/care/drafts/999").status_code, 404)
        listed = {d["id"]: d["state"] for d in self.client.get("/api/care/drafts").get_json()["drafts"]}
        self.assertEqual(listed[draft_id], "signed")
        page = self.client.get("/care/drafts")
        self.assertIn(b"Documentation drafts", page.data)
        page.close()

    def test_loop_sends_family_update_only_to_non_staff_paired_accounts(self):
        hooks = {**drafts_dashboard.dashboard._hooks, "send_email": Mock(),
                 "paired_emails": lambda: ["nurse@example.com", "son@example.com"]}
        self.log.set_family_kinds(["activity"], "Maria")
        self.log.record("activity", "Joined bingo")
        with patch.dict(drafts_dashboard.dashboard._hooks, hooks), \
                patch.dict(os.environ, {"EMBER_CARE_FAMILY_DIGEST": "mon 00:00"}):
            drafts_dashboard.tick()
        self.assertEqual(hooks["send_email"].call_args.args[0], ["son@example.com"])


if __name__ == "__main__":
    unittest.main()
