"""
Talk-over detection for audiobooks: pauses the book when someone speaks over it.

While a book plays, the microphone hears the narrator all the time, so Gemini
never hears the silence that ends the user's sentence and "Ember, pause the
book" can't get through. This predicts, moment by moment, how loud the book
should sound in the mic, and flags what the book can't explain:

- The book reports the level of everything it plays (book_level), the mic
  reports what it hears (mic_level).
- Every couple of seconds it finds the delay between the two (speaker
  buffering plus the room) by matching their loudness patterns, and how
  loud the echo is relative to the book (the echo gain).
- The mic being clearly louder than the predicted echo for a moment means
  someone is talking. The book is then held, so Gemini hears the user
  followed by real silence. If no request follows, it continues once the
  room has been quiet for a few seconds.

Measured on unit 001 (2026-10-21): the book's echo is ~530 RMS (median),
the user's voice ~1,100, so a plain loudness threshold can't separate them.
In simulation of that room, the book alone stays under ~1.0x the prediction,
so 1.5x leaves margin.

Pure logic with an injectable clock; realtime_gemini_10.py feeds it:
    detector.book_level(samples)     # each chunk the book writes to the speaker
    detector.mic_level(rms, seconds) # each mic block while the book is playing
    detector.holding()               # True while the book should stay paused
"""
import collections
import threading
import time

import numpy as np

# ─── Config ──────────────────────────────────────────────────────────────────
DEFAULT_RATIO = 1.5          # mic louder than ratio × predicted echo = talking
FIT_EXCLUDE_RATIO = 1.2      # with a model, learn only from moments it explains (not talking)
MIN_TALK_RMS = 300           # ...and at least this loud (quiet sounds never count)
WINDOW = 0.15                # levels are compared as energy over this window
TALK_SECONDS = 0.3           # the window must stay above for this long (not a cough)
NOISE_RMS = 60               # room noise added to the prediction
HOLD_SECONDS = 4.0           # hold lasts this long after the last loud moment
STEP = 0.02                  # envelope resolution (seconds)
MAX_DELAY = 0.6              # longest speaker-to-mic delay considered
HISTORY = 8.0                # seconds of levels kept for fitting
FIT_EVERY = 2.0              # refit delay and gain this often
MIN_FIT_SECONDS = 2.0        # book playback needed before the first fit
REF_MIN_RMS = 150            # book moments quieter than this don't teach the gain
SMEAR_STEPS = 1              # prediction averages the book's energy within ±this many steps


class TalkOverDetector:
    def __init__(self, ratio=DEFAULT_RATIO, clock=time.monotonic, log=print):
        self.ratio = ratio
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._ref = collections.deque()      # (time, book rms) per STEP of playback
        self._ref_end = None                 # when the book's queued audio ends
        self._mic = collections.deque()      # (time, mic rms, predicted echo or None) per mic block
        self._window = collections.deque()   # (time, seconds, mic energy, predicted energy)
        self._above_since = None             # when the window first went above the threshold
        self._delay = None
        self._gain = None
        self._next_fit = 0.0
        self._played = 0.0                   # seconds of book audio seen
        self._hold_until = 0.0
        self._holding_logged = False
        self._last_report = 0.0

    # ── Inputs ───────────────────────────────────────────────────────────────
    def book_level(self, samples, sample_rate=48000):
        """A chunk the book just wrote to the speaker (int16 samples, mono)."""
        now = self._clock()
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        step = max(1, int(STEP * sample_rate))
        n = len(x) // step
        if n == 0:
            return
        rms = np.sqrt(np.mean(x[: n * step].reshape(n, step) ** 2, axis=1))
        with self._lock:
            # Writes queue up behind each other; place this chunk right after
            # the previous one unless there was a gap.
            start = now if self._ref_end is None or self._ref_end < now - 0.2 else self._ref_end
            for i, r in enumerate(rms):
                self._ref.append((start + i * STEP, float(r)))
            self._ref_end = start + n * STEP
            self._played += n * STEP
            self._trim(now)

    def mic_level(self, rms, seconds):
        """A mic block of the given length. Returns True when this block starts
        a hold (someone began talking over the book)."""
        now = self._clock()
        with self._lock:
            self._trim(now)
            if self._played >= MIN_FIT_SECONDS and now >= self._next_fit and now >= self._hold_until:
                self._fit()
                self._next_fit = now + FIT_EVERY
            expected = self._predict(now)
            self._mic.append((now, float(rms), expected))
            if expected is not None:
                self._window.append((now, seconds, rms ** 2, expected ** 2 + NOISE_RMS ** 2))
            while self._window and now - self._window[0][0] > WINDOW:
                self._window.popleft()
            above = False
            if expected is not None and self._window:
                total = sum(sec for _, sec, _, _ in self._window)
                mic_e = sum(sec * m for _, sec, m, _ in self._window) / total
                pred_e = sum(sec * p for _, sec, _, p in self._window) / total
                above = mic_e >= MIN_TALK_RMS ** 2 and mic_e > self.ratio ** 2 * pred_e
            if not above:
                self._above_since = None
            elif self._above_since is None:
                self._above_since = now
            talking = above and now - self._above_since >= TALK_SECONDS - 1e-9
            loud = above and now < self._hold_until   # keeps an existing hold going
            started = False
            if talking or (loud and now < self._hold_until):
                started = now >= self._hold_until
                self._hold_until = now + HOLD_SECONDS
            if started:
                self._log(f"[TALK-OVER] Someone is talking over the book; pausing it "
                          f"(mic {rms:.0f}, book echo ~{expected:.0f})")
                self._holding_logged = True
            elif expected is not None and now - self._last_report >= 10.0:
                self._last_report = now
                self._log(f"[TALK-OVER] mic {rms:.0f}, book echo ~{expected:.0f}, "
                          f"delay {self._delay * 1000:.0f} ms, gain {self._gain:.2f}")
            return started

    def holding(self):
        """True while the book should stay paused for someone talking."""
        now = self._clock()
        with self._lock:
            held = now < self._hold_until
            if not held and self._holding_logged:
                self._holding_logged = False
                self._log("[TALK-OVER] Quiet again; the book continues")
            return held

    # ── Model ────────────────────────────────────────────────────────────────
    def _ref_at(self, t):
        """The book's level around time t: RMS over ±SMEAR_STEPS steps (0 if none)."""
        total, n = 0.0, 0
        for when, r in reversed(self._ref):
            if when < t - SMEAR_STEPS * STEP - 1e-9:
                break
            if when <= t + SMEAR_STEPS * STEP + 1e-9:
                total += r * r
                n += 1
        return (total / n) ** 0.5 if n else 0.0

    def _predict(self, now):
        if self._delay is None:
            return None
        return self._gain * self._ref_at(now - self._delay)

    def _fit(self):
        """Find the delay where the mic best follows the book, and the echo gain."""
        # Only moments the current model explains: a person talking would
        # otherwise teach it that the echo is louder, and hide the talking.
        quiet = [(t, r) for t, r, e in self._mic
                 if e is None or r <= FIT_EXCLUDE_RATIO * max(e, MIN_TALK_RMS)]
        if len(quiet) < 50 or len(self._ref) < 50:
            return
        ref_t = np.array([t for t, _ in self._ref])
        ref_r = np.array([r for _, r in self._ref])
        mic_t = np.array([t for t, _ in quiet])
        mic_r = np.array([r for _, r in quiet])
        best = None
        for delay in np.arange(0.0, MAX_DELAY + 1e-9, STEP):
            ref = np.interp(mic_t - delay, ref_t, ref_r, left=np.nan, right=np.nan)
            ok = ~np.isnan(ref)
            if ok.sum() < 50:
                continue
            a, b = mic_r[ok], ref[ok]
            if a.std() == 0 or b.std() == 0:
                continue
            corr = float(np.corrcoef(a, b)[0, 1])
            if best is None or corr > best[0]:
                best = (corr, delay, a, b)
        if best is None or best[0] < 0.2:
            return      # the mic doesn't follow the book (yet): keep what we had
        corr, delay, a, b = best
        loud_ref = b >= REF_MIN_RMS
        if loud_ref.sum() < 20:
            return
        self._delay = float(delay)
        # Energy-weighted: how much of the book's energy reaches the mic.
        self._gain = float(np.sqrt(np.sum(a[loud_ref] ** 2) / np.sum(b[loud_ref] ** 2)))

    def _trim(self, now):
        while self._ref and now - self._ref[0][0] > HISTORY:
            self._ref.popleft()
        while self._mic and now - self._mic[0][0] > HISTORY:
            self._mic.popleft()
