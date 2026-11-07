"""
Voice recognition — Ember knows who is speaking from their voice, so it can
tell who said what with the camera off, or when several people are in view.

Works like face_id.py, with a voice model in place of a face model:
  enroll:    a few clips of someone's voice -> embeddings, saved with that
             person's record in ember_people.json ("voice_embeddings")
  identify:  a new utterance -> embedding -> cosine similarity with each
             person's voiceprint (the average of their voice embeddings)
             -> "sure" / "maybe" / "unknown"

Model: SpeechBrain ECAPA-TDNN (spkrec-ecapa-voxceleb, Apache-2.0), 192-d
embeddings, 16 kHz mono audio. Downloaded once into voice_models/.

Thresholds (first calibration, see voice_proto/): on MDVR-KCL (6 speakers,
read speech for enrollment, conversation for testing, 30 test clips per
length) a cosine of >= 0.35 named 97% of 3-4 s clips correctly with 0% wrong
names; 0.30 or lower gave a wrong name. 1 s clips are often below 0.35, so
they come out "maybe"/"unknown" rather than misnamed. To be re-checked with
more speakers and on Ember's own microphone.

Privacy: like faces, voice embeddings (not recordings) are stored only on the
robot, in the person's record, and only with consent. Forgetting a person
(PeopleStore.remove) removes their voice too; forget_voice() removes only
the voice.

Storage: voiceprints live in the same person records as faces, under a
separate key, because PeopleStore loads every "embeddings" entry into a
128-wide face matrix. All writes go through the store's locked
read-modify-write (PeopleStore._update), so the dashboard and the AI core
never overwrite each other.
"""
import os
import threading
import time
import uuid
from dataclasses import dataclass

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(SCRIPT_DIR, "voice_models", "ecapa")
MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"

SAMPLE_RATE = 16000           # what the model expects
EMBED_DIM = 192
VOICE_KEY = "voice_embeddings"

# ─── Tuning (see calibration notes above) ───────────────────────────────────
SURE_THRESHOLD = 0.40         # confident: "Stella is speaking"
MAYBE_THRESHOLD = 0.25        # "might be Stella" — ask before assuming
MIN_SECONDS = 1.0             # shorter speech is too little to go on
ENROLL_MIN_SECONDS = 2.0      # enrollment clips must be at least this long
MAX_VOICE_SAMPLES = 20        # per person; the most redundant sample is dropped
# A new sample must resemble the person's existing voiceprint, so one wrong
# sample (someone else talking) can't creep in. Placeholder, not calibrated
# yet: set at the "maybe" threshold.
VOICE_CONSISTENCY = 0.25
MAX_NAME = 60


# ─── Audio ───────────────────────────────────────────────────────────────────
def to_model_audio(audio, sample_rate):
    """Any mic audio -> float32 mono at 16 kHz, values in [-1, 1].
    Accepts int16 or float arrays, mono (n,) or multi-channel (n, channels),
    e.g. Ember's 48 kHz int16 mic stream."""
    audio = np.asarray(audio)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    else:
        audio = audio.astype(np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)            # channels -> mono
    if sample_rate != SAMPLE_RATE:
        from scipy.signal import resample_poly
        g = int(np.gcd(int(sample_rate), SAMPLE_RATE))
        audio = resample_poly(audio, SAMPLE_RATE // g, int(sample_rate) // g).astype(np.float32)
    return audio


# ─── Model ───────────────────────────────────────────────────────────────────
class VoiceModel:
    """Thin wrapper over SpeechBrain's ECAPA speaker encoder."""

    def __init__(self, model_dir=None, num_threads=None):
        import torch
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy
        if num_threads:
            torch.set_num_threads(num_threads)
        self._torch = torch
        self._encoder = EncoderClassifier.from_hparams(
            source=MODEL_SOURCE,
            savedir=model_dir or MODEL_DIR,
            run_opts={"device": "cpu"},
            local_strategy=LocalStrategy.COPY,   # copies, not symlinks (Windows-safe)
        )
        self._encoder.eval()
        self._lock = threading.Lock()

    def embed(self, audio):
        """16 kHz float32 mono audio -> unit-length 192-d embedding."""
        wav = self._torch.from_numpy(np.ascontiguousarray(audio, np.float32)).unsqueeze(0)
        with self._lock, self._torch.no_grad():
            emb = self._encoder.encode_batch(wav).squeeze().cpu().numpy().astype(np.float32)
        return _normalize(emb)


# ─── Results ─────────────────────────────────────────────────────────────────
@dataclass
class VoiceMatch:
    level: str                 # "sure" | "maybe" | "unknown" | "too_short"
    person_id: str = None      # set for "sure" and "maybe"
    name: str = None
    score: float = 0.0         # best cosine similarity
    seconds: float = 0.0       # length of the audio judged
    embedding: np.ndarray = None   # kept so the caller can learn from it
    ms: float = 0.0            # time spent embedding

    def describe(self):
        """Short text for Gemini, e.g. '[SPEAKER] Stella (voice, sure)'."""
        if self.level == "sure":
            return f"[SPEAKER] {self.name} (voice, sure)"
        if self.level == "maybe":
            return f"[SPEAKER] might be {self.name} (voice, not sure)"
        return "[SPEAKER] unknown voice"


# ─── Identification & enrollment ─────────────────────────────────────────────
class VoiceIdentifier:
    """Who is speaking, from enrolled voiceprints in the people store.

    `store` is face_id.PeopleStore (or anything with people(), get(),
    find_by_name(), reload_if_changed() and _update(change))."""

    def __init__(self, model, store, clock=time.time):
        self.model = model
        self.store = store
        self._clock = clock

    # ── Queries ──────────────────────────────────────────────────────────────
    def voiceprints(self, candidates=None):
        """[(person_id, name, voiceprint)] for everyone with voice samples.
        `candidates`: optional person ids to limit the search to."""
        self.store.reload_if_changed()
        out = []
        for p in self.store.people():
            samples = p.get(VOICE_KEY) or []
            if not samples or (candidates is not None and p["id"] not in candidates):
                continue
            out.append((p["id"], p["name"], _normalize(np.mean(np.array(samples, np.float32), axis=0))))
        return out

    def has_voice(self, person_id):
        person = self.store.get(person_id)
        return bool(person and person.get(VOICE_KEY))

    # ── Identify ─────────────────────────────────────────────────────────────
    def identify(self, audio, sample_rate=SAMPLE_RATE, candidates=None):
        """Who is speaking in `audio` (one utterance) -> VoiceMatch."""
        audio = to_model_audio(audio, sample_rate)
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_SECONDS:
            return VoiceMatch("too_short", seconds=seconds)
        t0 = time.perf_counter()
        emb = self.model.embed(audio)
        ms = (time.perf_counter() - t0) * 1000
        prints = self.voiceprints(candidates)
        if not prints:
            return VoiceMatch("unknown", seconds=seconds, embedding=emb, ms=ms)
        scores = np.stack([v for _, _, v in prints]) @ emb
        i = int(np.argmax(scores))
        score = float(scores[i])
        level = ("sure" if score >= SURE_THRESHOLD
                 else "maybe" if score >= MAYBE_THRESHOLD else "unknown")
        pid, name = (prints[i][0], prints[i][1]) if level != "unknown" else (None, None)
        return VoiceMatch(level, pid, name, score, seconds, emb, ms)

    # ── Enroll ───────────────────────────────────────────────────────────────
    def embed_clips(self, clips, sample_rate=SAMPLE_RATE):
        """Clips long enough to enroll -> embeddings (too-short ones skipped)."""
        out = []
        for clip in clips:
            audio = to_model_audio(clip, sample_rate)
            if len(audio) / SAMPLE_RATE >= ENROLL_MIN_SECONDS:
                out.append(self.model.embed(audio))
        return out

    def enroll(self, name, clips, sample_rate=SAMPLE_RATE, consent=False, relationship=None,
               allow_new=False):
        """Explicit enrollment ("Ember, learn my voice"): add voice samples to
        the person with this name. Only with consent.
        allow_new=False (default): the person must already be known (by face),
        because face_id.remember() can't add a face to a person who has no
        face samples yet. allow_new=True creates a voice-only person (testing).
        Returns {"status", "person_id", "added", "created"}."""
        if not consent:
            raise ValueError("Ask before remembering someone's voice.")
        name = clean_name(name)
        if not name:
            raise ValueError("A name is needed.")
        embeddings = self.embed_clips(clips, sample_rate)
        if not embeddings:
            return {"status": "not_saved",
                    "reason": f"No clip was at least {ENROLL_MIN_SECONDS:.0f} seconds long."}
        existing = self.store.find_by_name(name)
        if existing and existing.get(VOICE_KEY):
            # Adding to a known voice: the new clips must sound like them
            added = self.add_samples(existing["id"], embeddings)
            if not added:
                return {"status": "not_saved", "person_id": existing["id"],
                        "reason": f"This voice doesn't match the {name} I already know."}
            return {"status": "saved", "person_id": existing["id"], "added": added, "created": False}
        if len(embeddings) > 1 and not _consistent_set(embeddings):
            return {"status": "not_saved",
                    "reason": "The clips don't sound like one person. Record them again, "
                              "one person at a time, somewhere quiet."}
        if existing:     # known by face, first voice samples
            added = self.add_samples(existing["id"], embeddings, check_consistency=False)
            return {"status": "saved", "person_id": existing["id"], "added": added, "created": False}
        if not allow_new:
            return {"status": "not_saved",
                    "reason": f"I don't know {name}'s face yet. Learn their face first, "
                              "then their voice."}
        person_id = self._create_person(name, embeddings, relationship)
        return {"status": "saved", "person_id": person_id, "added": len(embeddings), "created": True}

    def add_samples(self, person_id, embeddings, check_consistency=True):
        """Add voice samples to an existing person (by id; never creates one,
        so a person deleted meanwhile stays deleted). With check_consistency,
        only samples that sound like their stored voice are kept. Also used for
        passive learning, e.g. when the face confirms who is talking.
        Returns how many were kept."""
        embeddings = [_normalize(e) for e in embeddings]
        now = self._clock()

        def change(data):
            person = next((p for p in data["people"] if p["id"] == person_id), None)
            if person is None:
                return 0
            stored = [np.asarray(e, np.float32) for e in person.get(VOICE_KEY) or []]
            voiceprint = _normalize(np.mean(stored, axis=0)) if stored else None
            keep = [e for e in embeddings if not check_consistency or voiceprint is None
                    or float(voiceprint @ e) >= VOICE_CONSISTENCY]
            if keep:
                person[VOICE_KEY] = [[round(float(v), 5) for v in e]
                                     for e in _keep_diverse(stored + keep, MAX_VOICE_SAMPLES)]
                person["voice_consent"] = True
                person["voice_updated"] = now
            return len(keep)

        return self.store._update(change)

    def _create_person(self, name, embeddings, relationship=None):
        """A new person known only by voice so far (no face samples)."""
        now = self._clock()

        def change(data):
            person = next((p for p in data["people"] if _name_key(p["name"]) == _name_key(name)),
                          None)
            if person is None:
                person = {"id": "p_" + uuid.uuid4().hex[:8], "name": name, "source": "voice",
                          "created": now, "last_seen": None, "embeddings": []}
                data["people"].append(person)
            if relationship:
                person["relationship"] = " ".join(str(relationship).split())[:60]
            person[VOICE_KEY] = [[round(float(v), 5) for v in e]
                                 for e in _keep_diverse(embeddings, MAX_VOICE_SAMPLES)]
            person["voice_consent"] = True
            person["voice_updated"] = now
            return person["id"]

        return self.store._update(change)

    # ── Forget ───────────────────────────────────────────────────────────────
    def forget_voice(self, person_id):
        """Remove only someone's voice (their face and memories stay).
        To forget a person entirely, use PeopleStore.remove()."""
        def change(data):
            for p in data["people"]:
                if p["id"] == person_id and VOICE_KEY in p:
                    for key in (VOICE_KEY, "voice_consent", "voice_updated"):
                        p.pop(key, None)
                    return True
            return False
        return self.store._update(change)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def clean_name(name):
    """Same as face_id.clean_name (repeated so this file imports on any OS)."""
    return " ".join(str(name or "").split())[:MAX_NAME]


def _name_key(name):
    return " ".join(str(name or "").lower().split())


def _normalize(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def _consistent_set(embeddings):
    """Every clip resembles the average of the others (one speaker)."""
    m = np.array(embeddings)
    for i in range(len(m)):
        others = _normalize(np.delete(m, i, axis=0).mean(axis=0))
        if float(others @ m[i]) < VOICE_CONSISTENCY:
            return False
    return True


def _keep_diverse(samples, limit):
    """Trim to `limit` samples like face_id._keep_diverse: first drop outliers
    (unlike all the others — likely a mistake), then the most redundant."""
    samples = [np.asarray(s, np.float32) for s in samples]
    while len(samples) > limit:
        m = np.array(samples) @ np.array(samples).T
        np.fill_diagonal(m, -1)
        best_match = m.max(axis=1)
        if best_match.min() < VOICE_CONSISTENCY:
            samples.pop(int(np.argmin(best_match)))
        else:
            samples.pop(int(np.argmax(best_match)))
    return samples
