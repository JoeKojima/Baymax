"""
Face recognition — Ember knows who is in front of it, and asks new people
who they are (with permission) before remembering them.

Models (OpenCV Zoo, downloaded once into face_models/ and hash-checked):
  YuNet  face detector    — MIT license
  SFace  face recognizer  — Apache-2.0 license, 128-d embeddings
Both run through OpenCV's built-in cv2.FaceDetectorYN / cv2.FaceRecognizerSF,
so no extra packages are needed.

Thresholds were calibrated on the LFW benchmark (2,771 images, 150 enrolled
people, 1,500 strangers, 5 samples per person): cosine >= 0.50 recognised
98.5% of probes with 0% misidentification and 0.07% of strangers wrongly
named. 0.40–0.50 is treated as "might be" — Ember asks before assuming.
Real home cameras (dim light, angles, distance) score lower than LFW, which is
why Ember keeps learning a known person's face from confident sightings.

Privacy: face embeddings (not photos) are stored only on the robot in
ember_people.json. Nobody is remembered without consent — either the family
adds photos in the app, or the person says yes when Ember asks. Strangers who
decline are remembered only in memory, for this visit, so Ember doesn't ask
again, and are never written to disk.
"""
import fcntl
import hashlib
import json
import os
import threading
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(SCRIPT_DIR, "face_models")
PEOPLE_PATH = os.path.join(SCRIPT_DIR, "ember_people.json")

MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}

# ─── Tuning (see calibration notes above) ───────────────────────────────────
MATCH_THRESHOLD = 0.50        # confident: "Margaret is here"
MAYBE_THRESHOLD = 0.40        # "might be Margaret" — ask before assuming
DETECTION_SCORE = 0.80
MIN_FACE_PX = 40              # smaller faces are too blurry to recognise
LEARN_FACE_PX = 64            # minimum face size to store as a sample
MAX_SAMPLES = 20              # per person; the most redundant sample is dropped
AUTO_LEARN_RANGE = (0.55, 0.85)   # confident but not a near-duplicate
AUTO_LEARN_INTERVAL = 60.0    # seconds between learned samples per person
CONFIRM_VOTES = 2             # confident frames needed before naming someone
IN_VIEW_SECONDS = 2.0         # a face seen this recently is "in front of you"
TRACK_EXPIRY = 10.0           # forget a track this long after it leaves
REMEMBER_WINDOW = 120.0       # remember_person can still target a face seen this recently
INTRO_AFTER = 4.0             # an unknown face must stay this long before Ember asks
INTRO_MIN_SAMPLES = 3
INTRO_COOLDOWN = 90.0         # seconds between "who are you?" questions
VISIT_MEMORY = 30 * 60        # RAM-only memory of strangers asked this visit
SAME_VISITOR = 0.45           # similarity to treat a returning stranger as the same one
LAST_SEEN_SAVE_INTERVAL = 300.0


# ─── Gemini tools and instructions ──────────────────────────────────────────
PEOPLE_TOOLS = [
    {
        "name": "remember_person",
        "description": (
            "Remember the face of the person you just asked about, so you recognise "
            "them next time. ONLY call this after they clearly said yes to you "
            "remembering their face. Also call it when someone confirms they are a "
            "person you only thought they 'might be'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Their name, as they said it"},
                "relationship": {
                    "type": "STRING",
                    "description": "How they know the person you care for, if they said "
                                   "(e.g. 'neighbor', 'nurse')",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "decline_to_be_remembered",
        "description": "The person you asked does not want their face remembered.",
    },
    {
        "name": "forget_person",
        "description": (
            "Delete a remembered face. Only when that person asks you to forget them, "
            "or a family member asks."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"name": {"type": "STRING", "description": "Who to forget"}},
            "required": ["name"],
        },
    },
]

PEOPLE_INSTRUCTIONS = (
    "# Recognizing people\n"
    "You recognize people by their face. [SELF] updates tell you who is in front "
    "of you — trust them. If one says someone \"might be\" a person, check "
    "politely (\"Is that you, Sarah?\") before assuming, and if they confirm, "
    "call remember_person with that name.\n"
    "A message starting with \"[NEW PERSON]\" means someone you don't recognize "
    "is in front of you. Greet them warmly, say you're Ember, and ask their name. "
    "Then ask whether it's okay for you to remember their face so you'll know "
    "them next time. Only if they clearly say yes, call remember_person. If they "
    "say no or seem unsure, call decline_to_be_remembered and don't ask again.\n"
    "Never remember a face without permission. Faces you remember stay on this "
    "robot only. If someone asks you to forget them, call forget_person."
)


# ─── Models ──────────────────────────────────────────────────────────────────
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ensure_models(model_dir=None):
    """Download the models if missing and verify their hashes.
    Returns {"detector": path, "recognizer": path}. Raises on failure."""
    model_dir = model_dir or MODEL_DIR
    os.makedirs(model_dir, exist_ok=True)
    paths = {}
    for key, (filename, url, sha) in MODELS.items():
        path = os.path.join(model_dir, filename)
        if not os.path.exists(path) or _sha256(path) != sha:
            print(f"[FACE] Downloading {filename}...")
            tmp = path + ".part"
            urllib.request.urlretrieve(url, tmp)
            if _sha256(tmp) != sha:
                os.remove(tmp)
                raise RuntimeError(f"{filename} failed its integrity check")
            os.replace(tmp, path)
        paths[key] = path
    return paths


@dataclass
class Detection:
    box: tuple          # x, y, w, h in pixels
    landmarks: np.ndarray   # 5x2: right eye, left eye, nose, mouth right, mouth left
    score: float
    row: np.ndarray     # raw YuNet row, needed for alignment

    @property
    def size(self):
        return min(self.box[2], self.box[3])

    def frontal(self):
        """Nose between the eyes and eyes roughly level — a usable, front-on face."""
        (rx, ry), (lx, ly), nx = self.landmarks[0], self.landmarks[1], self.landmarks[2][0]
        eye_dist = abs(lx - rx)
        if eye_dist < 1:
            return False
        return (min(rx, lx) < nx < max(rx, lx)) and abs(ly - ry) < 0.35 * eye_dist


class FaceModels:
    """Thin wrapper over OpenCV's YuNet detector and SFace recognizer."""

    def __init__(self, paths=None):
        import cv2
        paths = paths or ensure_models()
        self._cv2 = cv2
        self._detector = cv2.FaceDetectorYN.create(paths["detector"], "", (320, 320),
                                                   DETECTION_SCORE, 0.3, 50)
        self._recognizer = cv2.FaceRecognizerSF.create(paths["recognizer"], "")
        self._lock = threading.Lock()   # the OpenCV objects aren't thread-safe

    def detect(self, frame):
        h, w = frame.shape[:2]
        with self._lock:
            self._detector.setInputSize((w, h))
            _, rows = self._detector.detect(frame)
        if rows is None:
            return []
        return [Detection(box=tuple(int(v) for v in r[:4]),
                          landmarks=r[4:14].reshape(5, 2), score=float(r[14]), row=r)
                for r in rows]

    def embed(self, frame, det):
        with self._lock:
            aligned = self._recognizer.alignCrop(frame, det.row)
            feat = self._recognizer.feature(aligned).flatten().astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9)


# ─── People store (shared with the dashboard) ───────────────────────────────
class PeopleStore:
    """ember_people.json: remembered people and their face embeddings.
    Writers (AI core, dashboard) take an exclusive file lock for every
    read-modify-write; readers pick up changes by checking the file's mtime."""

    def __init__(self, path=None, clock=time.time):
        self.path = path or PEOPLE_PATH
        self._clock = clock
        self._lock = threading.RLock()
        self._data = {"version": 1, "people": []}
        self._mtime = None
        self._matrix = np.zeros((0, 128), np.float32)
        self._owners = []
        self.reload_if_changed(force=True)

    # ── File handling ────────────────────────────────────────────────────────
    def _read_file(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("people"), list):
                raise ValueError("bad format")
            return data
        except FileNotFoundError:
            return {"version": 1, "people": []}
        except (OSError, ValueError) as e:
            print(f"[FACE] Could not read {self.path} ({e}); starting with nobody remembered.")
            return {"version": 1, "people": []}

    def _write_file(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def _file_mtime(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def reload_if_changed(self, force=False):
        mtime = self._file_mtime()
        with self._lock:
            if not force and mtime == self._mtime:
                return False
            self._set(self._read_file(), mtime)
            return True

    def _set(self, data, mtime):
        self._data = data
        self._mtime = mtime
        rows, owners = [], []
        for person in data["people"]:
            for emb in person.get("embeddings", []):
                rows.append(emb)
                owners.append(person["id"])
        self._matrix = (np.array(rows, np.float32) if rows
                        else np.zeros((0, 128), np.float32))
        self._owners = owners

    def _update(self, change):
        """Locked read-modify-write; `change(data)` returns the call's result."""
        with self._lock:
            with open(self.path + ".lock", "a+") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    data = self._read_file()
                    result = change(data)
                    self._write_file(data)
                    self._set(data, self._file_mtime())
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
            return result

    # ── Queries ──────────────────────────────────────────────────────────────
    def people(self):
        with self._lock:
            return [dict(p) for p in self._data["people"]]

    def get(self, person_id):
        with self._lock:
            return next((dict(p) for p in self._data["people"] if p["id"] == person_id), None)

    def find_by_name(self, name):
        key = _name_key(name)
        with self._lock:
            return next((dict(p) for p in self._data["people"]
                         if _name_key(p["name"]) == key), None)

    def match(self, embedding):
        """Best (person_id, score) for an embedding, or (None, 0.0)."""
        with self._lock:
            if not len(self._owners):
                return None, 0.0
            scores = self._matrix @ embedding
            i = int(np.argmax(scores))
            return self._owners[i], float(scores[i])

    def summary(self):
        """For the dashboard — no embeddings."""
        return [{k: p.get(k) for k in ("id", "name", "relationship", "source", "created",
                                       "last_seen")} | {"samples": len(p.get("embeddings", []))}
                for p in self.people()]

    # ── Changes ──────────────────────────────────────────────────────────────
    def add_samples(self, name, embeddings, source, relationship=None):
        """Add face samples to the person with this name (created if new).
        Returns (person_id, created)."""
        name = " ".join(str(name or "").split())[:60]
        if not name:
            raise ValueError("A name is needed.")
        embeddings = [np.asarray(e, np.float32) for e in embeddings]
        if not embeddings:
            raise ValueError("No usable face samples.")
        now = self._clock()

        def change(data):
            person = next((p for p in data["people"] if _name_key(p["name"]) == _name_key(name)),
                          None)
            created = person is None
            if created:
                person = {"id": "p_" + uuid.uuid4().hex[:8], "name": name, "source": source,
                          "created": now, "last_seen": None, "embeddings": []}
                data["people"].append(person)
            if relationship:
                person["relationship"] = " ".join(str(relationship).split())[:60]
            samples = [np.asarray(e, np.float32) for e in person["embeddings"]] + embeddings
            person["embeddings"] = [[round(float(v), 5) for v in e]
                                    for e in _keep_diverse(samples, MAX_SAMPLES)]
            person["updated"] = now
            return person["id"], created

        return self._update(change)

    def remove(self, person_id):
        def change(data):
            before = len(data["people"])
            data["people"] = [p for p in data["people"] if p["id"] != person_id]
            return len(data["people"]) < before
        return self._update(change)

    def touch(self, seen):
        """seen: {person_id: timestamp} — persist last-seen times."""
        def change(data):
            for p in data["people"]:
                if p["id"] in seen:
                    p["last_seen"] = seen[p["id"]]
        self._update(change)


def _name_key(name):
    return " ".join(str(name or "").lower().split())


def _keep_diverse(samples, limit):
    """Drop the most redundant samples (highest similarity to another) until
    at most `limit` remain, so the stored set covers different looks."""
    samples = list(samples)
    while len(samples) > limit:
        m = np.array(samples) @ np.array(samples).T
        np.fill_diagonal(m, -1)
        samples.pop(int(np.argmax(m.max(axis=1))))
    return samples


# ─── Tracking & introductions ────────────────────────────────────────────────
@dataclass
class Track:
    id: int
    first_seen: float
    last_seen: float
    box: tuple
    samples: deque = field(default_factory=lambda: deque(maxlen=10))
    votes: deque = field(default_factory=lambda: deque(maxlen=6))
    person_id: str = None
    maybe_id: str = None
    asked: bool = False
    declined: bool = False


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


class FaceIdentifier:
    """Turns camera frames into "who is in front of me", decides when to ask a
    new person who they are, and handles the remember/decline/forget tools."""

    def __init__(self, models, store, clock=time.time, profile=None):
        self.models = models
        self.store = store
        self._clock = clock
        self._lock = threading.RLock()
        self._tracks = []
        self._next_track = 1
        self._visitors = []          # RAM only: [{"mean", "asked_at", "declined", "last_seen"}]
        self._last_intro = -1e9
        self._last_asked_track = None
        self._last_learned = {}
        self._seen = {}
        self._last_seen_saved = clock()
        self.profile = profile or {}

    # ── Frame processing ─────────────────────────────────────────────────────
    def process(self, frame):
        now = self._clock()
        self.store.reload_if_changed()
        observations = []
        for det in self.models.detect(frame):
            if det.size < MIN_FACE_PX:
                continue
            emb = self.models.embed(frame, det)
            observations.append((det, emb))
        with self._lock:
            self._update_tracks(observations, now)
        self._maybe_save_last_seen(now)

    def _update_tracks(self, observations, now):
        self._tracks = [t for t in self._tracks if now - t.last_seen <= TRACK_EXPIRY]
        used = set()
        for det, emb in observations:
            track = self._assign(det, emb, now, used)
            used.add(track.id)
            track.last_seen = now
            track.box = det.box
            good = det.size >= LEARN_FACE_PX and det.frontal()
            if good:
                track.samples.append(emb)
            pid, score = self.store.match(emb)
            track.votes.append((pid if score >= MAYBE_THRESHOLD else None, score))
            self._resolve(track)
            if track.person_id:
                self._seen[track.person_id] = now
                if (good and pid == track.person_id
                        and AUTO_LEARN_RANGE[0] <= score <= AUTO_LEARN_RANGE[1]
                        and now - self._last_learned.get(pid, -1e9) >= AUTO_LEARN_INTERVAL):
                    self._last_learned[pid] = now
                    person = self.store.get(pid)
                    if person:
                        self.store.add_samples(person["name"], [emb], source="auto")
            elif track.samples and not (track.asked or track.declined):
                self._inherit_visit(track, now)

    def _assign(self, det, emb, now, used):
        live = [t for t in self._tracks if t.id not in used and now - t.last_seen <= 1.5]
        best = max(live, key=lambda t: _iou(t.box, det.box), default=None)
        if best is not None and _iou(best.box, det.box) >= 0.3:
            return best
        for t in self._tracks:
            if t.id not in used and t.samples and float(np.mean(t.samples, axis=0) @ emb) >= 0.5:
                return t
        track = Track(id=self._next_track, first_seen=now, last_seen=now, box=det.box)
        self._next_track += 1
        self._tracks.append(track)
        return track

    def _resolve(self, track):
        confident = [pid for pid, s in track.votes if pid and s >= MATCH_THRESHOLD]
        if confident:
            top = max(set(confident), key=confident.count)
            if confident.count(top) >= CONFIRM_VOTES:
                if track.person_id is None or (track.person_id != top
                                               and confident.count(top) >= 3):
                    track.person_id = top
        if self.store.get(track.person_id) is None:
            track.person_id = None          # forgotten / deleted in the app
        maybes = [pid for pid, s in track.votes if pid and MAYBE_THRESHOLD <= s < MATCH_THRESHOLD]
        top_maybe = max(set(maybes), key=maybes.count) if maybes else None
        track.maybe_id = (top_maybe if not track.person_id and top_maybe
                          and maybes.count(top_maybe) >= CONFIRM_VOTES else None)

    def _inherit_visit(self, track, now):
        """A stranger who stepped away and came back is the same visitor."""
        self._visitors = [v for v in self._visitors if now - v["last_seen"] <= VISIT_MEMORY]
        mean = _normalize(np.mean(track.samples, axis=0))
        for v in self._visitors:
            if float(v["mean"] @ mean) >= SAME_VISITOR:
                track.asked, track.declined = True, v["declined"]
                v["last_seen"] = now
                return

    def _remember_visit(self, track, now, declined=False):
        if not track.samples:
            return
        mean = _normalize(np.mean(track.samples, axis=0))
        for v in self._visitors:
            if float(v["mean"] @ mean) >= SAME_VISITOR:
                v.update(last_seen=now, declined=v["declined"] or declined)
                return
        self._visitors.append({"mean": mean, "asked_at": now, "last_seen": now,
                               "declined": declined})

    def _maybe_save_last_seen(self, now):
        if self._seen and now - self._last_seen_saved >= LAST_SEEN_SAVE_INTERVAL:
            seen, self._seen = self._seen, {}
            self._last_seen_saved = now
            self.store.touch(seen)

    # ── What's in view ───────────────────────────────────────────────────────
    def in_view(self):
        now = self._clock()
        with self._lock:
            return [t for t in self._tracks if now - t.last_seen <= IN_VIEW_SECONDS]

    def _label(self, person_id):
        person = self.store.get(person_id)
        if not person:
            return None
        name = person["name"]
        user = self.profile.get("user_name") or ""
        if user and _name_key(name) == _name_key(user):
            return name
        relationship = person.get("relationship") or next(
            (h.get("relationship") for h in self.profile.get("household", [])
             if _name_key(h.get("name")) == _name_key(name)), None)
        if relationship:
            whose = f"{user}'s" if user else "their"
            return f"{name} ({whose} {relationship})"
        return name

    def describe_in_view(self):
        tracks = self.in_view()
        if not tracks:
            return "Nobody is in front of your camera right now."
        known, maybe, unknown = [], [], 0
        for t in tracks:
            if t.person_id and (label := self._label(t.person_id)):
                if label not in known:
                    known.append(label)
            elif t.maybe_id and (label := self._label(t.maybe_id)):
                maybe.append(f"someone who might be {label} (not sure)")
            else:
                unknown += 1
        parts = known + maybe
        if unknown:
            parts.append("someone you don't recognize" if unknown == 1
                         else f"{unknown} people you don't recognize")
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        people = len(known) + len(maybe) + unknown
        return f"{text[0].upper()}{text[1:]} {'is' if people == 1 else 'are'} in front of you."

    # ── Introductions ────────────────────────────────────────────────────────
    def pending_introduction(self):
        """If someone unfamiliar has been in view long enough and hasn't been
        asked, mark them asked and return the prompt for Gemini; else None."""
        now = self._clock()
        with self._lock:
            if now - self._last_intro < INTRO_COOLDOWN:
                return None
            for t in self._tracks:
                if (now - t.last_seen <= 1.0 and not t.person_id and not t.asked
                        and not t.declined and now - t.first_seen >= INTRO_AFTER
                        and len(t.samples) >= INTRO_MIN_SAMPLES):
                    t.asked = True
                    self._last_intro = now
                    self._last_asked_track = t.id
                    self._remember_visit(t, now)
                    return self._intro_prompt(t)
        return None

    def _intro_prompt(self, track):
        others = [self._label(t.person_id) for t in self.in_view() if t.person_id]
        with_text = f" They're with {', '.join(others)}." if others else ""
        if track.maybe_id and (label := self._label(track.maybe_id)):
            name = self.store.get(track.maybe_id)["name"]
            return (f"[NEW PERSON] Someone who might be {label} is in front of you, but "
                    f"you're not sure.{with_text} Ask kindly, e.g. \"Is that you, {name}?\" "
                    f"If they say yes, call remember_person with the name \"{name}\" so you "
                    "recognize them better. If not, ask their name and whether you may "
                    "remember their face.")
        user = self.profile.get("user_name")
        hint = ""
        if user and not self.store.find_by_name(user) and not others:
            hint = (f" You haven't learned the face of {user}, the person you care for, "
                    f"yet — this could be them, so you might ask \"Are you {user}?\"")
        return (f"[NEW PERSON] Someone you don't recognize has been in front of you for a "
                f"few seconds.{with_text}{hint} Greet them, tell them you're Ember, and ask "
                "their name. Then ask if it's okay to remember their face so you'll know "
                "them next time. Only if they clearly say yes, call remember_person.")

    # ── Tools ────────────────────────────────────────────────────────────────
    def handle_tool_call(self, name, args):
        args = dict(args or {})
        handlers = {
            "remember_person": lambda: self.remember(args.get("name"), args.get("relationship")),
            "decline_to_be_remembered": self.decline,
            "forget_person": lambda: self.forget(args.get("name")),
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return handler()
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            print(f"[FACE] {name} failed: {e}")
            return {"error": str(e)}

    def _target_for_remember(self, name, now):
        recent = [t for t in self._tracks if now - t.last_seen <= REMEMBER_WINDOW]
        asked = next((t for t in recent if t.id == self._last_asked_track), None)
        if asked and asked.samples:
            return asked, None
        existing = self.store.find_by_name(name)
        maybes = [t for t in recent if existing and t.maybe_id == existing["id"] and t.samples]
        if maybes:
            return max(maybes, key=lambda t: t.last_seen), None
        unknown = [t for t in recent if not t.person_id and t.samples
                   and now - t.last_seen <= IN_VIEW_SECONDS]
        if len(unknown) == 1:
            return unknown[0], None
        if len(unknown) > 1:
            return None, ("Several unfamiliar faces are in view. Ask them to look at your "
                          "camera one at a time, then try again.")
        return None, ("I can't see an unfamiliar face clearly right now. Ask them to look "
                      "at your camera for a few seconds, then try again.")

    def _canonical_name(self, name):
        """Use the family's spelling when the name matches someone in the profile."""
        known = [self.profile.get("user_name") or ""] + [
            h.get("name", "") for h in self.profile.get("household", [])]
        return next((k for k in known if k and _name_key(k) == _name_key(name)),
                    " ".join(str(name).split()))

    def remember(self, name, relationship=None):
        if not name or not str(name).strip():
            raise ValueError("Ask for their name first.")
        name = self._canonical_name(name)
        now = self._clock()
        with self._lock:
            track, problem = self._target_for_remember(name, now)
            if problem:
                return {"status": "not_saved", "reason": problem}
            if track.person_id:
                current = self.store.get(track.person_id)
                if current and _name_key(current["name"]) != _name_key(name):
                    return {"status": "not_saved",
                            "reason": f"That face is already remembered as {current['name']}."}
            samples = list(track.samples)
            existing = self.store.find_by_name(name)
            person_id, created = self.store.add_samples(name, samples, source="conversation",
                                                        relationship=relationship)
            track.person_id, track.maybe_id, track.declined = person_id, None, False
            track.votes.clear()
            self._visitors = [v for v in self._visitors
                              if float(v["mean"] @ _normalize(np.mean(samples, axis=0)))
                              < SAME_VISITOR]
        label = self._label(person_id) or name
        print(f"[FACE] Remembered {label} ({len(samples)} samples)", flush=True)
        return {"status": "remembered", "name": self.store.get(person_id)["name"],
                "who": label, "new_person": created and existing is None}

    def decline(self):
        now = self._clock()
        with self._lock:
            track = next((t for t in self._tracks if t.id == self._last_asked_track), None)
            if track is None:
                return {"status": "ok", "note": "Nobody to forget — nothing was saved."}
            track.declined = True
            self._remember_visit(track, now, declined=True)
        return {"status": "ok", "note": "Their face was not saved. You won't ask again "
                                        "during this visit."}

    def forget(self, name):
        person = self.store.find_by_name(name)
        if not person:
            names = [p["name"] for p in self.store.people()]
            return {"status": "not_found", "remembered_people": names}
        self.store.remove(person["id"])
        now = self._clock()
        with self._lock:
            for t in self._tracks:
                if t.person_id == person["id"] or t.maybe_id == person["id"]:
                    t.person_id = t.maybe_id = None
                    t.votes.clear()
                    t.asked = t.declined = True     # they just asked to be forgotten
                    self._remember_visit(t, now, declined=True)
        print(f"[FACE] Forgot {person['name']}", flush=True)
        return {"status": "forgotten", "name": person["name"]}


def _normalize(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


# ─── Enrollment from photos (used by the dashboard) ─────────────────────────
def embeddings_from_photo(models, image_bytes):
    """Decode an uploaded photo and return (embedding, None) or (None, reason).
    Needs one clear, front-facing main face; small faces in the background
    (at most 40% of the main face's area) are ignored."""
    import cv2
    data = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return None, "Not an image we can read."
    # YuNet misses faces larger than ~500 px, so try a 640 px copy first (close-ups,
    # selfies), then 1280 px if no face — or only a too-small one — was found
    # (small faces in a big group photo).
    faces = []
    original = img
    for target in (640, 1280):
        longest = max(original.shape[:2])
        scale = min(1.0, target / longest)
        img = (original if scale == 1.0 else
               cv2.resize(original, (int(original.shape[1] * scale), int(original.shape[0] * scale)),
                          interpolation=cv2.INTER_AREA))
        faces = [d for d in models.detect(img) if d.size >= MIN_FACE_PX]
        if scale == 1.0 or (faces and max(d.size for d in faces) >= LEARN_FACE_PX):
            break
    if not faces:
        return None, "No face found. Use a clear, well-lit photo of their face."
    faces.sort(key=lambda d: d.box[2] * d.box[3], reverse=True)
    if len(faces) > 1 and faces[1].box[2] * faces[1].box[3] > 0.4 * faces[0].box[2] * faces[0].box[3]:
        return None, "More than one face in this photo. Use a photo of just them."
    main = faces[0]
    if main.size < LEARN_FACE_PX or not main.frontal():
        return None, "The face is too small or turned away. Use a closer, front-facing photo."
    return models.embed(img, main), None
