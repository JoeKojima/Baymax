"""
Real-model tests for face_id.py: the actual YuNet + SFace models on real
photos, placed into 640x480 frames at the face sizes the robot's camera sees.

Needs a folder of face photos laid out one sub-folder per person (e.g. the
public LFW benchmark, http://vis-www.cs.umass.edu/lfw/ — not included in the
repo) and the models (downloaded automatically into face_models/):

    EMBER_FACE_TEST_DATA=/path/to/lfw_funneled python3 -m unittest test/test_face_models.py -v

Skipped when EMBER_FACE_TEST_DATA isn't set.
"""
import glob
import os
import random
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))
import face_id as fi  # noqa: E402

DATA = os.getenv("EMBER_FACE_TEST_DATA")

try:
    import cv2
except ImportError:      # pragma: no cover
    cv2 = None


def _people(min_photos):
    out = []
    for d in sorted(glob.glob(os.path.join(DATA, "*"))):
        photos = sorted(glob.glob(os.path.join(d, "*.jpg")))
        if len(photos) >= min_photos:
            out.append((os.path.basename(d), photos))
    return out


def robot_frame(photos, face_px=80, dim=1.0, seed=0):
    """Place photos (LFW: face ≈ 45% of a 250px image) into a 640x480 frame so
    each face is ~face_px wide, as the robot's camera would see a person
    1–2 m away. `dim` < 1 darkens the frame (gamma) to mimic evening light."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(90, 140, (480, 640, 3), dtype=np.uint8)
    if not photos:
        return frame
    slot_w = 640 // len(photos)
    for k, path in enumerate(photos):
        img = cv2.imread(path)
        scale = min(face_px / (0.45 * img.shape[1]), slot_w / img.shape[1], 480 / img.shape[0])
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        x, y = k * slot_w + (slot_w - w) // 2, (480 - h) // 2
        frame[y:y + h, x:x + w] = img
    if dim != 1.0:
        frame = np.clip(255 * (frame / 255.0) ** (1 / dim), 0, 255).astype(np.uint8)
    return frame


def jpg(img):
    return cv2.imencode(".jpg", img)[1].tobytes()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@unittest.skipUnless(DATA and cv2 is not None, "set EMBER_FACE_TEST_DATA to a face-photo folder")
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = fi.FaceModels(fi.ensure_models())
        random.seed(3)
        # "On camera" tests use photos with exactly one person in them, so a
        # bystander in the original photo doesn't count as a second person.
        cls.regulars = []
        for name, photos in _people(12):
            solo = [p for p in photos if len(cls.models.detect(cv2.imread(p))) == 1]
            if len(solo) >= 11:
                cls.regulars.append((name, solo))
            if len(cls.regulars) == 25:
                break
        singles = [p for p in _people(1) if len(p[1]) == 1]
        random.shuffle(singles)
        cls.strangers = [p[1][0] for p in singles[:200]]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = fi.PeopleStore(os.path.join(self.dir.name, "people.json"), clock=self.clock)
        self.ident = fi.FaceIdentifier(self.models, self.store, clock=self.clock,
                                       profile={"user_name": "", "household": []})

    def tearDown(self):
        self.dir.cleanup()

    def enroll_from_photos(self, name, photos, count=None):
        """Enrol like a family member would: photos the app rejects (side-on,
        too small) are skipped. Returns the photos actually used."""
        embs, used = [], []
        for p in photos:
            with open(p, "rb") as f:
                emb, reason = fi.embeddings_from_photo(self.models, f.read())
            if emb is not None:
                embs.append(emb)
                used.append(p)
            if count and len(embs) == count:
                break
        self.assertTrue(embs, f"no usable photos for {name}")
        self.store.add_samples(name, embs, source="app")
        return used

    def feed(self, frames, fps=2):
        for frame in frames:
            self.ident.process(frame)
            self.clock.t += 1 / fps

    # ── Detection ────────────────────────────────────────────────────────────
    def test_detects_faces_in_robot_frames(self):
        photos = [p[1][0] for p in self.regulars[:3]]
        for n in (1, 2, 3):
            for px in (60, 90, 120):
                found = self.models.detect(robot_frame(photos[:n], face_px=px))
                self.assertEqual(len(found), n, f"{n} faces at {px}px")

    def test_empty_frame_has_no_faces(self):
        self.assertEqual(self.models.detect(np.full((480, 640, 3), 120, np.uint8)), [])
        self.assertEqual(self.models.detect(robot_frame([], face_px=80)), [])

    # ── End-to-end with the identifier ──────────────────────────────────────
    def test_family_photos_then_recognised_on_camera(self):
        name, photos = self.regulars[0]
        self.enroll_from_photos("Margaret", photos[:5])
        self.feed([robot_frame([p], seed=i) for i, p in enumerate(photos[5:9])])
        self.assertEqual(self.ident.describe_in_view(), "Margaret is in front of you.")

    def test_stranger_asked_then_remembered_then_recognised(self):
        _, margaret = self.regulars[0]
        _, dave = self.regulars[1]
        self.enroll_from_photos("Margaret", margaret[:5])
        # Dave stands in front of the robot for 5 s (10 frames, varied photos)
        self.feed([robot_frame([dave[i % 4]], seed=i) for i in range(10)])
        self.assertEqual(self.ident.describe_in_view(),
                         "Someone you don't recognize is in front of you.")
        prompt = self.ident.pending_introduction()
        self.assertTrue(prompt and prompt.startswith("[NEW PERSON]"), prompt)
        result = self.ident.handle_tool_call("remember_person", {"name": "Dave"})
        self.assertEqual(result["status"], "remembered", result)
        # A day later, different photos of Dave, standing next to Margaret
        self.clock.t += 86400
        self.feed([robot_frame([margaret[6 + i % 3], dave[5 + i % 4]], seed=50 + i)
                   for i in range(6)])
        self.assertEqual(self.ident.describe_in_view(), "Margaret and Dave are in front of you.")
        self.assertIsNone(self.ident.pending_introduction())

    def test_decline_not_saved_and_not_reasked(self):
        _, dave = self.regulars[1]
        self.feed([robot_frame([dave[i % 4]], seed=i) for i in range(10)])
        self.assertIsNotNone(self.ident.pending_introduction())
        self.ident.handle_tool_call("decline_to_be_remembered", {})
        self.assertEqual(self.store.people(), [])
        self.clock.t += fi.INTRO_COOLDOWN + 30          # leaves, comes back later
        self.feed([robot_frame([dave[5 + i % 4]], seed=90 + i) for i in range(12)])
        self.assertIsNone(self.ident.pending_introduction())

    # ── Accuracy at robot scale ──────────────────────────────────────────────
    def test_accuracy_at_robot_distance(self):
        gallery = self.regulars[:20]
        probes = {}
        for name, photos in gallery:
            used = self.enroll_from_photos(name, photos, count=5)
            probes[name] = [p for p in photos if p not in used][:6]
        ids = {p["name"]: p["id"] for p in self.store.people()}
        recognised = wrong = total = 0
        for name, photos in gallery:
            for i, p in enumerate(probes[name]):
                frame = robot_frame([p], face_px=75, seed=i)
                dets = self.models.detect(frame)
                self.assertTrue(dets, p)
                main = max(dets, key=lambda d: d.size)   # the subject, not a bystander
                pid, score = self.store.match(self.models.embed(frame, main))
                total += 1
                if score >= fi.MATCH_THRESHOLD:
                    recognised += pid == ids[name]
                    wrong += pid != ids[name]
        named_strangers = 0
        for i, p in enumerate(self.strangers):
            frame = robot_frame([p], face_px=75, seed=i)
            dets = self.models.detect(frame)
            if dets:
                _, score = self.store.match(self.models.embed(frame, dets[0]))
                named_strangers += score >= fi.MATCH_THRESHOLD
        rec, wr, st = recognised / total, wrong / total, named_strangers / len(self.strangers)
        print(f"\n[robot-scale accuracy] 20 people x 5 photos enrolled; {total} probes at 75px: "
              f"recognised {100*rec:.1f}%, named as someone else {100*wr:.2f}%; "
              f"{len(self.strangers)} strangers named as someone {100*st:.2f}%")
        self.assertGreaterEqual(rec, 0.85)
        self.assertLessEqual(wr, 0.01)
        self.assertLessEqual(st, 0.02)

    def test_dim_light_falls_back_to_asking_not_misnaming(self):
        gallery = self.regulars[:10]
        probes = {}
        for name, photos in gallery:
            used = self.enroll_from_photos(name, photos, count=5)
            probes[name] = [p for p in photos if p not in used][:4]
        ids = {p["name"]: p["id"] for p in self.store.people()}
        outcomes = {"right": 0, "maybe": 0, "unknown": 0, "wrong": 0}
        for name, photos in gallery:
            for i, p in enumerate(probes[name]):
                frame = robot_frame([p], face_px=75, dim=0.45, seed=i)
                dets = self.models.detect(frame)
                if not dets:
                    outcomes["unknown"] += 1
                    continue
                main = max(dets, key=lambda d: d.size)
                pid, score = self.store.match(self.models.embed(frame, main))
                if score >= fi.MATCH_THRESHOLD:
                    outcomes["right" if pid == ids[name] else "wrong"] += 1
                elif score >= fi.MAYBE_THRESHOLD:
                    outcomes["maybe"] += 1
                else:
                    outcomes["unknown"] += 1
        print(f"\n[dim light, gamma 0.45] {outcomes}")
        self.assertEqual(outcomes["wrong"], 0)
        self.assertGreater(outcomes["right"] + outcomes["maybe"], sum(outcomes.values()) // 2)

    # ── Photo uploads (dashboard) ────────────────────────────────────────────
    def test_photo_validation(self):
        good = self.regulars[0][1][0]
        with open(good, "rb") as f:
            emb, reason = fi.embeddings_from_photo(self.models, f.read())
        self.assertIsNone(reason)
        self.assertAlmostEqual(float(np.linalg.norm(emb)), 1.0, places=4)
        self.assertEqual(fi.embeddings_from_photo(self.models, b"not an image")[1],
                         "Not an image we can read.")
        blank = np.full((400, 400, 3), 128, np.uint8)
        self.assertIn("No face found", fi.embeddings_from_photo(self.models, jpg(blank))[1])
        two = robot_frame([self.regulars[0][1][0], self.regulars[1][1][0]], face_px=110)
        self.assertIn("More than one face", fi.embeddings_from_photo(self.models, jpg(two))[1])
        # A bystander in the background (much smaller face) is ignored
        main = cv2.resize(cv2.imread(good), (500, 500))
        bystander = cv2.resize(cv2.imread(self.regulars[1][1][0]), (150, 150))
        main[340:490, 340:490] = bystander
        emb_main, reason = fi.embeddings_from_photo(self.models, jpg(main))
        self.assertIsNone(reason)
        self.assertGreater(float(emb_main @ emb), 0.7)            # it's the main person
        tiny = robot_frame([good], face_px=50)
        self.assertIn("too small", fi.embeddings_from_photo(self.models, jpg(tiny))[1])
        for side in (500, 1280, 2000, 4000):                       # close-up phone photos
            big = cv2.resize(cv2.imread(good), (side, side))
            t0 = time.perf_counter()
            emb_big, reason = fi.embeddings_from_photo(self.models, jpg(big))
            self.assertIsNone(reason, f"{side}px close-up: {reason}")
            self.assertGreater(float(emb_big @ emb), 0.8)        # same face either way
            self.assertLess(time.perf_counter() - t0, 3.0)
        # face ~300 px in a 4000 px photo: too small at 640 (47 px), usable at 1280 (96 px)
        wide = np.full((3000, 4000, 3), 120, np.uint8)
        wide[1200:1950, 1800:2550] = cv2.resize(cv2.imread(good), (750, 750))
        emb_small, reason = fi.embeddings_from_photo(self.models, jpg(wide))
        self.assertIsNone(reason)
        self.assertGreater(float(emb_small @ emb), 0.8)

    # ── Speed ────────────────────────────────────────────────────────────────
    def test_processing_speed(self):
        frames = {n: robot_frame([p[1][0] for p in self.regulars[:n]], face_px=80) for n in (0, 1, 2)}
        for n, frame in frames.items():
            self.ident.process(frame)                            # warm-up
            t0 = time.perf_counter()
            for _ in range(10):
                self.ident.process(frame)
            ms = (time.perf_counter() - t0) * 100
            print(f"\n[speed] 640x480 frame, {n} face(s): {ms:.1f} ms per frame")
            self.assertLess(ms, 250)        # robot runs this at 2 fps → 500 ms budget


if __name__ == "__main__":
    unittest.main()
