"""
Logic tests for Baymax-main/face_id.py — no camera, no models: a fake model
returns scripted faces with controlled embeddings, and a fake clock drives
time. (Real-model accuracy is covered by test_face_models.py.)

    python3 -m unittest test/test_face_id.py -v
"""
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))
import face_id as fi  # noqa: E402

RNG = np.random.default_rng(42)


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def identity():
    """A random person's 'true' face direction."""
    return unit(RNG.normal(size=128))


def sample(base, similarity):
    """An embedding with cosine `similarity` to `base` (exactly)."""
    noise = RNG.normal(size=128)
    noise -= (noise @ base) * base
    noise = unit(noise)
    return unit(similarity * base + np.sqrt(1 - similarity ** 2) * noise)


def face(emb, x=200, size=120, frontal=True, score=0.95):
    """A scripted face for FakeModels."""
    return {"emb": unit(emb), "box": (x, 100, size, size), "frontal": frontal, "score": score}


class FakeModels:
    """detect(frame) where frame is a list of face() dicts."""
    def detect(self, frame):
        out = []
        for f in frame:
            x, y, w, h = f["box"]
            if f["frontal"]:
                lm = [(x + .3 * w, y + .4 * h), (x + .7 * w, y + .4 * h), (x + .5 * w, y + .6 * h),
                      (x + .35 * w, y + .8 * h), (x + .65 * w, y + .8 * h)]
            else:   # nose outside the eyes: turned away
                lm = [(x + .3 * w, y + .4 * h), (x + .45 * w, y + .4 * h), (x + .6 * w, y + .6 * h),
                      (x + .35 * w, y + .8 * h), (x + .5 * w, y + .8 * h)]
            det = fi.Detection(box=f["box"], landmarks=np.array(lm, np.float32),
                               score=f["score"], row=None)
            det.emb = f["emb"]
            out.append(det)
        return out

    def embed(self, frame, det):
        return det.emb


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


PROFILE = {"user_name": "Margaret", "about": "",
           "household": [{"name": "Sarah", "relationship": "daughter", "notes": ""}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "people.json")
        self.clock = Clock()
        self.store = fi.PeopleStore(self.path, clock=self.clock)
        self.ident = fi.FaceIdentifier(FakeModels(), self.store, clock=self.clock,
                                       profile=dict(PROFILE))

    def tearDown(self):
        self.dir.cleanup()

    def frames(self, make_frame, seconds, fps=2):
        """Feed frames for `seconds` at `fps`; make_frame() builds each frame."""
        for _ in range(int(seconds * fps)):
            self.ident.process(make_frame())
            self.clock.t += 1 / fps

    def enroll(self, name, base, n=5, sim=0.75):
        self.store.add_samples(name, [sample(base, sim) for _ in range(n)], source="app")


# ─── Store ───────────────────────────────────────────────────────────────────
class StoreTests(Base):
    def test_add_create_append_and_case_insensitive_names(self):
        a = identity()
        pid, created = self.store.add_samples("Margaret", [sample(a, .8)], source="app")
        self.assertTrue(created)
        pid2, created2 = self.store.add_samples("  margaret ", [sample(a, .8)], source="conversation")
        self.assertEqual((pid2, created2), (pid, False))
        self.assertEqual(len(self.store.get(pid)["embeddings"]), 2)
        self.assertEqual(self.store.find_by_name("MARGARET")["id"], pid)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.store.add_samples("", [identity()], source="app")
        with self.assertRaises(ValueError):
            self.store.add_samples("Bob", [], source="app")

    def test_sample_limit_keeps_diverse(self):
        a = identity()
        dupes = [sample(a, .99) for _ in range(25)]
        distinct = [sample(a, .6) for _ in range(5)]
        pid, _ = self.store.add_samples("A", dupes + distinct, source="app")
        kept = np.array(self.store.get(pid)["embeddings"])
        self.assertEqual(len(kept), fi.MAX_SAMPLES)
        # all 5 distinct looks survive; near-duplicates were the ones dropped
        for d in distinct:
            self.assertGreater(max(kept @ d), 0.999)

    def test_match_and_remove(self):
        a, b = identity(), identity()
        pa, _ = self.store.add_samples("A", [sample(a, .8)], source="app")
        pb, _ = self.store.add_samples("B", [sample(b, .8)], source="app")
        pid, score = self.store.match(sample(a, .9))
        self.assertEqual(pid, pa)
        self.assertGreater(score, 0.6)
        self.assertTrue(self.store.remove(pa))
        self.assertFalse(self.store.remove(pa))
        self.assertNotEqual(self.store.match(sample(a, .9))[0], pa)
        self.assertEqual(fi.PeopleStore(self.path).people()[0]["id"], pb)

    def test_empty_store_matches_nobody(self):
        self.assertEqual(self.store.match(identity()), (None, 0.0))

    def test_summary_has_no_embeddings(self):
        self.store.add_samples("A", [identity()], source="app", relationship="neighbor")
        s = self.store.summary()[0]
        self.assertNotIn("embeddings", s)
        self.assertEqual((s["name"], s["samples"], s["relationship"]), ("A", 1, "neighbor"))

    def test_other_process_changes_are_picked_up(self):
        other = fi.PeopleStore(self.path)          # e.g. the dashboard
        other.add_samples("A", [identity()], source="app")
        self.assertEqual(self.store.people(), [])  # not until reload
        self.assertTrue(self.store.reload_if_changed())
        self.assertEqual([p["name"] for p in self.store.people()], ["A"])
        self.assertFalse(self.store.reload_if_changed())

    def test_concurrent_writers_lose_nothing(self):
        stores = [fi.PeopleStore(self.path) for _ in range(4)]
        def write(store, k):
            for i in range(10):
                store.add_samples(f"P{k}-{i}", [identity()], source="app")
        threads = [threading.Thread(target=write, args=(s, k)) for k, s in enumerate(stores)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(fi.PeopleStore(self.path).people()), 40)

    def test_concurrent_processes_lose_nothing(self):
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_write_people, args=(self.path, k)) for k in range(3)]
        for p in procs: p.start()
        for p in procs: p.join(30)
        self.assertEqual([p.exitcode for p in procs], [0, 0, 0])
        self.assertEqual(len(fi.PeopleStore(self.path).people()), 30)

    def test_corrupt_or_wrong_shape_file(self):
        for content in ("{nope", "[]", '{"people": "x"}'):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertEqual(fi.PeopleStore(self.path).people(), [])


def _write_people(path, k):
    store = fi.PeopleStore(path)
    rng = np.random.default_rng(k)
    for i in range(10):
        store.add_samples(f"proc{k}-{i}", [unit(rng.normal(size=128))], source="app")


# ─── Recognition & what's in view ────────────────────────────────────────────
class RecognitionTests(Base):
    def test_known_person_named_after_two_confident_frames(self):
        m = identity()
        self.enroll("Margaret", m)
        self.ident.process([face(sample(m, .7))])
        self.assertIn("don't recognize", self.ident.describe_in_view())   # 1 vote isn't enough
        self.ident.process([face(sample(m, .7))])
        self.assertEqual(self.ident.describe_in_view(), "Margaret is in front of you.")

    def test_relationship_labels(self):
        s = identity()
        self.enroll("Sarah", s)
        self.frames(lambda: [face(sample(s, .7))], 1.5)
        self.assertEqual(self.ident.describe_in_view(), "Sarah (Margaret's daughter) is in front of you.")
        self.ident.profile = {"user_name": "", "household": PROFILE["household"]}
        self.assertEqual(self.ident.describe_in_view(), "Sarah (their daughter) is in front of you.")

    def test_maybe_band(self):
        m = identity()
        self.enroll("Margaret", m, sim=1.0, n=1)
        self.frames(lambda: [face(sample(m, .45))], 1.5)       # 0.40 <= score < 0.50
        self.assertEqual(self.ident.describe_in_view(),
                         "Someone who might be Margaret (not sure) is in front of you.")

    def test_stranger_unknown(self):
        self.enroll("Margaret", identity())
        self.frames(lambda: [face(identity())], 1.5)
        self.assertEqual(self.ident.describe_in_view(), "Someone you don't recognize is in front of you.")

    def test_group_grammar(self):
        m, s = identity(), identity()
        self.enroll("Margaret", m)
        self.enroll("Sarah", s)
        x1, x2 = identity(), identity()
        self.frames(lambda: [face(sample(m, .7), x=0), face(sample(s, .7), x=200),
                             face(sample(x1, .9), x=400), face(sample(x2, .9), x=600)], 1.5)
        self.assertEqual(self.ident.describe_in_view(),
                         "Margaret, Sarah (Margaret's daughter) and 2 people you don't recognize "
                         "are in front of you.")
        self.clock.t += 5
        self.frames(lambda: [face(sample(x1, .9), x=400), face(sample(x2, .9), x=600)], 1.0)
        self.assertEqual(self.ident.describe_in_view(),
                         "2 people you don't recognize are in front of you.")

    def test_nobody_after_leaving(self):
        m = identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7))], 1.5)
        self.frames(lambda: [], 2.5)
        self.assertEqual(self.ident.describe_in_view(), "Nobody is in front of your camera right now.")

    def test_tiny_faces_ignored(self):
        self.frames(lambda: [face(identity(), size=fi.MIN_FACE_PX - 1)], 2)
        self.assertEqual(self.ident.describe_in_view(), "Nobody is in front of your camera right now.")

    def test_identity_sticks_through_a_bad_frame(self):
        m = identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7))], 1.5)
        self.ident.process([face(sample(m, .1))])               # turned head / blur
        self.assertEqual(self.ident.describe_in_view(), "Margaret is in front of you.")

    def test_two_people_tracked_separately_while_moving(self):
        m, s = identity(), identity()
        self.enroll("Margaret", m)
        self.enroll("Sarah", s)
        for i in range(6):
            self.ident.process([face(sample(m, .7), x=50 + 10 * i), face(sample(s, .7), x=400 - 10 * i)])
            self.clock.t += .5
        self.assertEqual(sorted(t.person_id for t in self.ident.in_view()),
                         sorted([self.store.find_by_name("Margaret")["id"],
                                 self.store.find_by_name("Sarah")["id"]]))


# ─── Asking who someone is ───────────────────────────────────────────────────
class IntroductionTests(Base):
    def test_waits_for_four_seconds_and_three_samples(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.frames(lambda: [face(sample(x, .9))], 3.5)
        self.assertIsNone(self.ident.pending_introduction())
        self.frames(lambda: [face(sample(x, .9))], 1)
        prompt = self.ident.pending_introduction()
        self.assertTrue(prompt.startswith("[NEW PERSON] Someone you don't recognize"))
        self.assertIn("Only if they clearly say yes, call remember_person", prompt)
        self.assertIsNone(self.ident.pending_introduction())       # asked once

    def test_turned_away_faces_dont_count_as_samples(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.frames(lambda: [face(sample(x, .9), frontal=False)], 6)
        self.assertIsNone(self.ident.pending_introduction())

    def test_small_faces_recognised_but_not_learned(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.frames(lambda: [face(sample(x, .9), size=fi.LEARN_FACE_PX - 1)], 6)
        self.assertIn("don't recognize", self.ident.describe_in_view())
        self.assertIsNone(self.ident.pending_introduction())        # no usable samples

    def test_known_people_never_asked(self):
        m = identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7))], 10)
        self.assertIsNone(self.ident.pending_introduction())

    def test_cooldown_between_strangers(self):
        self.enroll("Margaret", identity())
        x, y = identity(), identity()
        self.frames(lambda: [face(sample(x, .9))], 5)
        self.assertIsNotNone(self.ident.pending_introduction())
        self.clock.t += 15
        self.frames(lambda: [face(sample(y, .9), x=500)], 5)
        self.assertIsNone(self.ident.pending_introduction())        # within 90 s
        self.frames(lambda: [face(sample(y, .9), x=500)], fi.INTRO_COOLDOWN - 20)  # stays put
        self.assertIsNotNone(self.ident.pending_introduction())

    def test_primary_user_hint_until_her_face_is_known(self):
        x = identity()
        self.frames(lambda: [face(sample(x, .9))], 5)
        self.assertIn('You haven\'t learned the face of Margaret, the person you care for, yet',
                      self.ident.pending_introduction())

    def test_maybe_prompt_asks_is_that_you(self):
        m = identity()
        self.enroll("Sarah", m, sim=1.0, n=1)
        self.enroll("Margaret", identity())
        self.frames(lambda: [face(sample(m, .45))], 5)
        prompt = self.ident.pending_introduction()
        self.assertIn('might be Sarah (Margaret\'s daughter)', prompt)
        self.assertIn('"Is that you, Sarah?"', prompt)

    def test_prompt_mentions_who_they_are_with(self):
        m, x = identity(), identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7), x=0), face(sample(x, .9), x=400)], 5)
        self.assertIn("They're with Margaret.", self.ident.pending_introduction())


# ─── Consent tools ───────────────────────────────────────────────────────────
class ToolTests(Base):
    def meet(self, x, seconds=5, **kw):
        self.frames(lambda: [face(sample(x, .9), **kw)], seconds)
        return self.ident.pending_introduction()

    def test_remember_after_yes_then_recognised(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.meet(x)
        result = self.ident.handle_tool_call("remember_person", {"name": "Dave", "relationship": "neighbor"})
        self.assertEqual(result["status"], "remembered")
        self.assertTrue(result["new_person"])
        self.assertEqual(self.store.find_by_name("Dave")["relationship"], "neighbor")
        self.assertEqual(self.ident.describe_in_view(), "Dave (Margaret's neighbor) is in front of you.")
        # comes back tomorrow: recognised from new frames
        self.clock.t += 86400
        self.frames(lambda: [face(sample(x, .8), x=300)], 1.5)
        self.assertEqual(self.ident.describe_in_view(), "Dave (Margaret's neighbor) is in front of you.")
        self.assertIsNone(self.ident.pending_introduction())

    def test_household_name_links_relationship(self):
        self.enroll("Margaret", identity())
        self.meet(identity())
        self.ident.handle_tool_call("remember_person", {"name": "sarah"})
        self.assertEqual(self.store.find_by_name("sarah")["name"], "Sarah")   # family's spelling
        self.assertEqual(self.ident.describe_in_view(), "Sarah (Margaret's daughter) is in front of you.")

    def test_confirming_a_maybe_adds_samples_to_that_person(self):
        s = identity()
        self.enroll("Sarah", s, sim=1.0, n=1)
        self.enroll("Margaret", identity())
        self.meet(s * 0 + sample(s, .45))   # one look, scored in the maybe band
        before = len(self.store.find_by_name("Sarah")["embeddings"])
        result = self.ident.handle_tool_call("remember_person", {"name": "Sarah"})
        self.assertEqual((result["status"], result["new_person"]), ("remembered", False))
        self.assertGreater(len(self.store.find_by_name("Sarah")["embeddings"]), before)
        self.assertEqual(len(self.store.people()), 2)

    def test_remember_with_nobody_unfamiliar(self):
        result = self.ident.handle_tool_call("remember_person", {"name": "Dave"})
        self.assertEqual(result["status"], "not_saved")
        self.assertIn("can't see an unfamiliar face", result["reason"])
        self.assertEqual(self.store.people(), [])

    def test_remember_with_two_strangers_and_no_question_asked(self):
        x, y = identity(), identity()
        self.frames(lambda: [face(sample(x, .9), x=0), face(sample(y, .9), x=400)], 2)
        result = self.ident.handle_tool_call("remember_person", {"name": "Dave"})
        self.assertIn("one at a time", result["reason"])
        self.assertEqual(self.store.people(), [])

    def test_wont_rename_a_known_face(self):
        m = identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7))], 5)
        self.ident._last_asked_track = self.ident.in_view()[0].id
        result = self.ident.handle_tool_call("remember_person", {"name": "Dave"})
        self.assertIn("already remembered as Margaret", result["reason"])

    def test_empty_name(self):
        self.meet(identity())
        self.assertIn("error", self.ident.handle_tool_call("remember_person", {"name": " "}))

    def test_decline_is_never_written_and_never_reasked(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.meet(x)
        result = self.ident.handle_tool_call("decline_to_be_remembered", {})
        self.assertEqual(result["status"], "ok")
        with open(self.path) as f:
            self.assertEqual(len(json.load(f)["people"]), 1)        # only Margaret on disk
        self.frames(lambda: [face(sample(x, .9))], 5)
        self.clock.t += fi.INTRO_COOLDOWN
        self.frames(lambda: [], 15)                                 # leaves the room...
        self.frames(lambda: [face(sample(x, .9), x=450)], 6)        # ...and comes back
        self.assertIsNone(self.ident.pending_introduction())
        self.clock.t += fi.VISIT_MEMORY + 1                         # a new visit, much later
        self.frames(lambda: [face(sample(x, .9), x=100)], 6)
        self.assertIsNotNone(self.ident.pending_introduction())

    def test_forget(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.meet(x)
        self.ident.handle_tool_call("remember_person", {"name": "Dave"})
        result = self.ident.handle_tool_call("forget_person", {"name": "dave"})
        self.assertEqual(result, {"status": "forgotten", "name": "Dave"})
        self.assertIsNone(self.store.find_by_name("Dave"))
        self.assertIn("don't recognize", self.ident.describe_in_view())
        self.clock.t += fi.INTRO_COOLDOWN
        self.frames(lambda: [face(sample(x, .9))], 6)
        self.assertIsNone(self.ident.pending_introduction())        # just asked to be forgotten
        self.assertEqual(self.ident.handle_tool_call("forget_person", {"name": "Zed"}),
                         {"status": "not_found", "remembered_people": ["Margaret"]})

    def test_deleted_in_app_is_no_longer_named(self):
        m = identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7))], 1.5)
        fi.PeopleStore(self.path).remove(self.store.find_by_name("Margaret")["id"])
        self.frames(lambda: [face(sample(m, .7))], 1)
        self.assertIn("don't recognize", self.ident.describe_in_view())

    def test_unknown_tool(self):
        self.assertIn("error", self.ident.handle_tool_call("nope", {}))


# ─── Learning & persistence ──────────────────────────────────────────────────
class LearningTests(Base):
    def test_auto_learn_rules(self):
        m = identity()
        self.enroll("Margaret", m, n=1, sim=1.0)
        pid = self.store.find_by_name("Margaret")["id"]
        count = lambda: len(self.store.get(pid)["embeddings"])
        self.frames(lambda: [face(sample(m, .7))], 1.5)             # in range → learn once
        self.assertEqual(count(), 2)
        self.frames(lambda: [face(sample(m, .7))], 30)              # < 60 s later: no
        self.assertEqual(count(), 2)
        self.clock.t += 60
        self.frames(lambda: [face(sample(m, .95))], 1)              # near-duplicate: no
        self.assertEqual(count(), 2)
        self.frames(lambda: [face(sample(m, .7), frontal=False)], 1)   # not front-on: no
        self.assertEqual(count(), 2)
        self.frames(lambda: [face(sample(m, .7))], 1)
        self.assertEqual(count(), 3)

    def test_last_seen_saved_every_five_minutes(self):
        m = identity()
        self.enroll("Margaret", m)
        self.frames(lambda: [face(sample(m, .7))], 2)
        self.assertIsNone(fi.PeopleStore(self.path).people()[0]["last_seen"])
        self.clock.t += fi.LAST_SEEN_SAVE_INTERVAL
        self.frames(lambda: [face(sample(m, .7))], 1)
        self.assertIsNotNone(fi.PeopleStore(self.path).people()[0]["last_seen"])


# ─── Arrivals: greeting people Ember knows ───────────────────────────────────
class ArrivalTests(Base):
    def visit(self, base, seconds=1.5, x=200):
        self.frames(lambda: [face(sample(base, .7), x=x)], seconds)

    def test_first_sighting_after_enrollment_is_greeted_once(self):
        s = identity()
        self.enroll("Sarah", s)
        self.visit(s)
        a = self.ident.pending_arrival()
        self.assertEqual((a["name"], a["label"], a["away"], a["is_primary"]),
                         ("Sarah", "Sarah (Margaret's daughter)", None, False))
        self.visit(s, 30)
        self.assertIsNone(self.ident.pending_arrival())        # still here: no second hello

    def test_visitor_greeted_after_twenty_minutes_away(self):
        s = identity()
        self.enroll("Sarah", s)
        self.visit(s)
        self.ident.pending_arrival()
        self.clock.t += 10 * 60                                 # popped out for 10 min
        self.visit(s)
        self.assertIsNone(self.ident.pending_arrival())
        self.clock.t += fi.AWAY_OTHERS
        self.visit(s)
        a = self.ident.pending_arrival()
        self.assertAlmostEqual(a["away"], fi.AWAY_OTHERS + 1.5, delta=1)
        self.assertIsNotNone(a["last_seen"])

    def test_primary_user_only_after_hours_away(self):
        m = identity()
        self.enroll("Margaret", m)
        self.visit(m)
        self.assertTrue(self.ident.pending_arrival()["is_primary"])
        self.clock.t += 3600                                    # back from the kitchen
        self.visit(m)
        self.assertIsNone(self.ident.pending_arrival())
        self.clock.t += fi.AWAY_PRIMARY
        self.visit(m)
        self.assertIsNotNone(self.ident.pending_arrival())

    def test_last_seen_survives_restart(self):
        s = identity()
        self.enroll("Sarah", s)
        self.visit(s)
        self.ident.pending_arrival()
        self.clock.t += fi.LAST_SEEN_SAVE_INTERVAL
        self.visit(s)                                           # last_seen saved to disk
        restarted = fi.FaceIdentifier(FakeModels(), fi.PeopleStore(self.path, clock=self.clock),
                                      clock=self.clock, profile=dict(PROFILE))
        self.clock.t += 60
        for _ in range(3):
            restarted.process([face(sample(s, .7))])
            self.clock.t += .5
        self.assertIsNone(restarted.pending_arrival())          # seen a minute ago, not "back"

    def test_cooldown_and_drop_if_they_leave(self):
        s, t = identity(), identity()
        self.enroll("Sarah", s)
        self.enroll("Tom", t)
        self.frames(lambda: [face(sample(s, .7), x=0), face(sample(t, .7), x=400)], 1.5)
        first = self.ident.pending_arrival()["name"]
        self.assertIsNone(self.ident.pending_arrival())         # 30 s between greetings
        self.clock.t += fi.GREET_COOLDOWN
        self.frames(lambda: [face(sample(s, .7), x=0), face(sample(t, .7), x=400)], 1)
        second = self.ident.pending_arrival()["name"]
        self.assertEqual({first, second}, {"Sarah", "Tom"})
        u = identity()
        self.enroll("Ursula", u)
        self.visit(u)
        self.frames(lambda: [], fi.ARRIVAL_MAX_AGE + 5)         # walked straight past
        self.visit(u)
        self.assertIsNone(self.ident.pending_arrival())

    def test_no_welcome_back_right_after_being_introduced(self):
        self.enroll("Margaret", identity())
        x = identity()
        self.frames(lambda: [face(sample(x, .9))], 5)
        self.ident.pending_introduction()
        self.ident.handle_tool_call("remember_person", {"name": "Dave"})
        self.frames(lambda: [face(sample(x, .8))], 3)
        self.assertIsNone(self.ident.pending_arrival())

    def test_present_names(self):
        s = identity()
        self.enroll("Sarah", s)
        self.frames(lambda: [face(sample(s, .7), x=0), face(identity(), x=400)], 1.5)
        self.assertEqual(self.ident.present_names(), ["Sarah", "someone unrecognized"])

    def test_crop_and_appearance_storage(self):
        frame = np.zeros((480, 640, 3), np.uint8)
        det = FakeModels().detect([face(identity(), x=300, size=100)])[0]
        jpg = fi._crop_jpeg(frame, det)
        self.assertTrue(jpg.startswith(b"\xff\xd8"))                # a JPEG
        self.assertIsNone(fi._crop_jpeg([], det))                  # not an image
        pid, _ = self.store.add_samples("Sarah", [identity()], source="app")
        self.store.set_appearance(pid, "Long brown hair")
        self.clock.t += 86400
        self.store.set_appearance(pid, "Short brown hair")
        p = fi.PeopleStore(self.path).get(pid)
        self.assertEqual((p["appearance"]["text"], p["appearance_before"]["text"]),
                         ("Short brown hair", "Long brown hair"))
        self.assertFalse(self.store.set_appearance("p_nobody", "x"))


# ─── Models & tools plumbing (no network) ───────────────────────────────────
class ModelDownloadTests(unittest.TestCase):
    def test_download_verify_and_reject_tampered(self):
        payloads = {k: os.urandom(64) for k in fi.MODELS}
        import hashlib
        fake_models = {k: (f"{k}.onnx", f"https://example/{k}", hashlib.sha256(v).hexdigest())
                       for k, v in payloads.items()}

        def fake_retrieve(url, dest, data=None):
            key = url.rsplit("/", 1)[1]
            with open(dest, "wb") as f:
                f.write(payloads[key])

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fi, "MODELS", fake_models), \
                mock.patch.object(fi.urllib.request, "urlretrieve", side_effect=fake_retrieve) as get:
            paths = fi.ensure_models(d)
            self.assertEqual(get.call_count, 2)
            fi.ensure_models(d)                       # already present and valid
            self.assertEqual(get.call_count, 2)
            with open(paths["detector"], "wb") as f:  # corrupted on disk → re-downloaded
                f.write(b"junk")
            fi.ensure_models(d)
            self.assertEqual(get.call_count, 3)
            payloads["recognizer"] = b"tampered"      # server returns wrong bytes → refuse
            os.remove(paths["recognizer"])
            with self.assertRaises(RuntimeError):
                fi.ensure_models(d)
            self.assertFalse(os.path.exists(paths["recognizer"]))

    def test_tool_declarations_valid_for_gemini(self):
        from google.genai import types
        cfg = types.LiveConnectConfig(tools=[{"function_declarations": fi.PEOPLE_TOOLS}])
        self.assertEqual([f.name for f in cfg.tools[0].function_declarations],
                         ["remember_person", "decline_to_be_remembered", "forget_person"])


if __name__ == "__main__":
    unittest.main()
