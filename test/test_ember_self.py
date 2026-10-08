"""
Tests for Baymax-main/ember_self.py (identity, profile, live status) and the
dashboard's /api/profile endpoint. No network; temp files only.

    python3 -m unittest test/test_ember_self.py -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BAYMAX = os.path.join(os.path.dirname(__file__), "..", "Baymax-main")
sys.path.insert(0, BAYMAX)
import ember_self as es  # noqa: E402
from ember_self import Capability  # noqa: E402

PROFILE = {
    "user_name": "Margaret",
    "about": "Retired teacher, loves gardening and her cat Milo.",
    "household": [
        {"name": "Sarah", "relationship": "daughter", "notes": "visits on Sundays"},
        {"name": "Tom", "relationship": "son"},
    ],
}
CAPS = [
    Capability("Have a real conversation."),
    Capability("Watch for falls with your camera.", enabled=False,
               when_off="Automatic fall detection is turned off on this robot."),
    Capability("Some hidden thing.", enabled=False),
]


# ─── Profile ─────────────────────────────────────────────────────────────────
class ProfileTests(unittest.TestCase):
    def test_normalize_trims_and_drops_junk(self):
        p = es.normalize_profile({
            "user_name": "  Margaret\n\n# NEW SECTION  ",
            "about": "x" * 5000,
            "household": [{"name": "  Sarah "}, {"name": ""}, "junk", {"relationship": "x"}],
            "evil": "dropped",
        })
        self.assertEqual(p["user_name"], "Margaret # NEW SECTION")   # newlines collapsed
        self.assertEqual(len(p["about"]), es.MAX_ABOUT)
        self.assertEqual(p["household"], [{"name": "Sarah", "relationship": "", "notes": ""}])
        self.assertNotIn("evil", p)

    def test_normalize_bad_types(self):
        self.assertEqual(es.normalize_profile(None),
                         {"user_name": "", "about": "", "household": []})
        self.assertEqual(es.normalize_profile({"household": "Sarah"})["household"], [])

    def test_household_capped(self):
        people = [{"name": f"P{i}"} for i in range(50)]
        self.assertEqual(len(es.normalize_profile({"household": people})["household"]),
                         es.MAX_PEOPLE)

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            self.assertEqual(es.load_profile(path)["user_name"], "")    # missing file
            es.save_profile(PROFILE, path)
            self.assertEqual(es.load_profile(path)["household"][1]["name"], "Tom")
            with open(path, "w") as f:
                f.write("{corrupt")
            self.assertEqual(es.load_profile(path)["user_name"], "")    # corrupt file


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "p.json")

    def tearDown(self):
        self.dir.cleanup()

    def response(self, status=200, body=None):
        res = mock.Mock(status_code=status)
        res.json.return_value = body
        res.raise_for_status.side_effect = (
            None if status < 400 else es.requests.HTTPError(str(status)))
        return res

    def sync(self, res=None, error=None, key="001.secret"):
        with mock.patch.object(es.requests, "get") as get:
            if error:
                get.side_effect = error
            else:
                get.return_value = res
            result = es.sync_profile_from_cloud("https://cloud.example/", key, self.path)
        return result, get

    def test_fetch_caches_then_unchanged(self):
        result, get = self.sync(self.response(200, PROFILE))
        self.assertEqual(result, "updated")
        self.assertEqual(get.call_args.args[0], "https://cloud.example/api/device/profile")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer 001.secret"})
        self.assertEqual(es.load_profile(self.path)["user_name"], "Margaret")
        self.assertEqual(self.sync(self.response(200, PROFILE))[0], "unchanged")

    def test_failures_keep_cached_copy(self):
        es.save_profile(PROFILE, self.path)
        self.assertEqual(self.sync(self.response(404))[0], "none")
        self.assertEqual(self.sync(self.response(500))[0], "offline")
        self.assertEqual(self.sync(error=es.requests.ConnectionError("down"))[0], "offline")
        bad = self.response(200)
        bad.json.side_effect = ValueError("not json")
        self.assertEqual(self.sync(bad)[0], "offline")
        self.assertEqual(self.sync(key="")[0], "offline")
        self.assertEqual(es.load_profile(self.path)["user_name"], "Margaret")


# ─── Identity ────────────────────────────────────────────────────────────────
class IdentityTests(unittest.TestCase):
    def test_real_identity_file_with_profile(self):
        text = es.build_identity(CAPS, PROFILE)
        self.assertTrue(text.startswith("# Who you are\nYou are Ember, a companion robot "
                                        "for older adults. You live in the home of Margaret"))
        self.assertIn("compassionate help with daily tasks", text)
        self.assertIn("Their name is Margaret.", text)
        self.assertIn("From their family: Retired teacher, loves gardening", text)
        self.assertIn("- Sarah — daughter (visits on Sundays)\n- Tom — son", text)
        self.assertIn("# What you can do\n- Have a real conversation.", text)
        self.assertIn("# What you can't do\n- Automatic fall detection is turned off", text)
        self.assertNotIn("Watch for falls", text)
        self.assertNotIn("Some hidden thing", text)
        self.assertNotIn("<!--", text)
        self.assertNotRegex(text, r"\{[a-z_]+\}")          # every placeholder filled
        self.assertIn("never tell anyone which medication or dose", text)
        self.assertIn("You cannot call emergency services", text)

    def test_without_profile(self):
        text = es.build_identity(CAPS, {})
        self.assertIn("You live in the home of the person you care for", text)
        self.assertIn("hasn't told you their name yet", text)
        self.assertIn("hasn't told you about anyone yet", text)

    def test_robot_name_override_and_braces_in_family_text(self):
        text = es.build_identity(CAPS, {"robot_name": "Sunny", "about": "Likes {curly} things"})
        self.assertIn("You are Sunny,", text)
        self.assertIn("Likes {curly} things", text)

    def test_all_capabilities_on_leaves_no_gap(self):
        text = es.build_identity([Capability("Talk.")], PROFILE)
        self.assertIn("# What you can't do\n- You can't make phone calls", text)
        self.assertNotIn("\n\n\n", text)

    def test_missing_file_falls_back(self):
        text = es.build_identity(CAPS, PROFILE, path="/nonexistent/identity.md")
        self.assertIn("You are Ember, a companion robot for older adults", text)
        self.assertIn("Margaret", text)
        self.assertIn("- Automatic fall detection is turned off", text)

    def test_profile_update_message(self):
        msg = es.profile_update_message(PROFILE)
        self.assertTrue(msg.startswith("[PROFILE UPDATE]"))
        self.assertIn("Their name is Margaret.", msg)
        self.assertIn("- Sarah — daughter", msg)


# ─── Live status ─────────────────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 8, 15, 5).timestamp()

    def __call__(self):
        return self.t


class LiveStatusTests(unittest.TestCase):
    def test_describe_time(self):
        self.assertEqual(es.describe_time(dt.datetime(2026, 10, 8, 15, 5)),
                         "It's Thursday, October 8, 3:05 PM (afternoon).")
        self.assertIn("12:30 AM (early morning)", es.describe_time(dt.datetime(2026, 10, 8, 0, 30)))

    def test_describe_last_talked(self):
        self.assertEqual(es.describe_last_talked(None, "Margaret"),
                         "You haven't heard from Margaret since you started up.")
        self.assertIsNone(es.describe_last_talked(120))
        self.assertIn("less than an hour", es.describe_last_talked(30 * 60))
        self.assertIn("about 1 hour ago", es.describe_last_talked(90 * 60))
        self.assertIn("about 3 hours ago", es.describe_last_talked(3 * 3600 + 5))

    def setUp(self):
        self.clock = Clock()
        self.state = es.SelfState(clock=self.clock, debounce=10, min_interval=60,
                                  refresh_interval=1800)

    def tick(self, seconds):
        self.clock.t += seconds
        return self.state.pending_update()

    def test_first_update_then_quiet(self):
        self.state.set("in_view", "Someone is in view.")
        first = self.state.pending_update()
        self.assertEqual(first, "[SELF] It's Thursday, October 8, 3:05 PM (afternoon). "
                                "Someone is in view.")
        self.assertIsNone(self.tick(30))

    def test_change_is_debounced_and_spaced(self):
        self.state.pending_update()
        self.clock.t += 61
        self.state.set("in_view", "Someone is in view.")
        self.assertIsNone(self.tick(5))                    # not settled yet
        self.state.set("in_view", "Nobody is in view.")    # flicker resets debounce
        self.state.set("in_view", "Someone is in view.")
        self.assertIsNone(self.tick(9))
        self.assertIn("Someone is in view.", self.tick(2))
        self.state.set("in_view", "Nobody is in view.")
        self.assertIsNone(self.tick(15))                   # settled, but < min_interval
        self.assertIn("Nobody is in view.", self.tick(50))

    def test_periodic_time_refresh_and_new_session(self):
        self.state.pending_update()
        self.assertIsNone(self.tick(1700))
        self.assertIn("3:36 PM", self.tick(200))           # 30-min refresh (3:05 + 1900 s)
        self.state.new_session()
        self.assertIsNotNone(self.tick(1))                 # new session gets status at once

    def test_removing_a_fact(self):
        self.state.set("last_talked", "You last heard from them 2 hours ago.")
        self.state.pending_update()
        self.clock.t += 100
        self.state.set("last_talked", None)
        self.assertNotIn("heard from", self.tick(11))


# ─── Dashboard /api/profile ──────────────────────────────────────────────────
class ProfileApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import baymax_app
        cls.app_module = baymax_app

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        users = os.path.join(self.dir.name, "users.json")
        with open(users, "w") as f:
            json.dump([
                {"id": "u1", "username": "sarah", "devices": [self.app_module.DEVICE_SERIAL]},
                {"id": "u2", "username": "stranger", "devices": []},
            ], f)
        self.patches = [
            mock.patch.object(self.app_module, "USERS_PATH", users),
            mock.patch.object(es, "PROFILE_PATH", os.path.join(self.dir.name, "profile.json")),
        ]
        for p in self.patches:
            p.start()
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def login(self, uid):
        with self.client.session_transaction() as s:
            s["user_id"] = uid

    def test_requires_login_and_pairing(self):
        self.assertEqual(self.client.get("/api/profile").status_code, 401)
        self.login("u2")
        self.assertEqual(self.client.get("/api/profile").status_code, 403)
        self.assertEqual(self.client.post("/api/profile", json=PROFILE).status_code, 403)

    def test_save_and_read(self):
        self.login("u1")
        res = self.client.post("/api/profile", json={**PROFILE, "junk": 1})
        self.assertEqual(res.status_code, 200)
        saved = res.get_json()
        self.assertEqual(saved["user_name"], "Margaret")
        self.assertEqual(saved["updated_by"], "sarah")
        self.assertNotIn("junk", saved)
        self.assertEqual(self.client.get("/api/profile").get_json()["household"][0]["name"], "Sarah")
        self.assertEqual(es.load_profile()["about"], PROFILE["about"])   # the AI core's view

    def test_rejects_non_object(self):
        self.login("u1")
        self.assertEqual(self.client.post("/api/profile", json=["x"]).status_code, 400)


if __name__ == "__main__":
    unittest.main()
