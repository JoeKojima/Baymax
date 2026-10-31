"""
Live voice recognition — listens to a copy of Ember's microphone stream and
says who is speaking, utterance by utterance, without slowing the conversation.

How it works (all in one background thread):
  1. feed(): the mic callback hands over each audio chunk (just a queue put —
     the callback never waits).
  2. While Ember itself is talking, audio is skipped, so Ember never tries to
     identify its own voice.
  3. A voice activity detector (Silero VAD; a simple loudness detector if it's
     not installed) finds where someone starts and stops talking.
  4. voice_id.identify() runs:
       - "early": once ~2 s of speech has been heard, while the person is
         still talking, so a name is usually ready before Gemini replies;
       - "final": when the utterance ends (on all of it).
  5. on_result(match, stage) receives each VoiceMatch (default: print it).

Telling Gemini (see realtime_gemini_10.py):
  - SpeakerNotes decides which results become a "[SPEAKER] ..." message, sent
    while the person is still talking (no repeats of the same speaker).
  - The identify_speaker tool lets Gemini ask "who is talking?" itself, e.g.
    for "Ember, who am I?" — short questions often end before a note is ready,
    so the tool waits briefly for the result of what was just said.

Test it on a laptop mic, without Ember:
    python voice_live.py --enroll Stella      # record 3 clips and learn your voice
    python voice_live.py                      # then talk; Ctrl+C to stop
(uses voice_proto/dev_people.json, never the robot's ember_people.json)
"""
import queue
import threading
import time
from collections import deque

import numpy as np

import voice_id

VAD_RATE = 16000
VAD_FRAME = 512               # Silero VAD takes 512-sample frames at 16 kHz (32 ms)
SPEECH_PROB = 0.5             # VAD probability that counts as speech
END_SILENCE_S = 0.5           # this much silence ends an utterance
PRE_ROLL_S = 0.3              # keep a little audio from just before speech started
EARLY_ID_S = 2.0              # identify early once this much speech is heard
MAX_UTTERANCE_S = 15.0        # cut very long utterances (identify them in pieces)
ECHO_TAIL_S = 0.4             # keep skipping briefly after Ember stops talking
QUEUE_CHUNKS = 2000           # ~40 s of 1024-sample chunks at 48 kHz
REPEAT_NOTE_S = 60.0          # re-send the same speaker's note at most this often
TOOL_WAIT_S = 1.5             # identify_speaker waits this long for an utterance to finish
RESULT_MAX_AGE_S = 15.0       # older results don't say who is talking *now*


# ─── Gemini tool and instructions ───────────────────────────────────────────
VOICE_TOOLS = [
    {
        "name": "identify_speaker",
        "description": (
            "Find out who is talking right now, from their voice. Call this when "
            "someone asks who they are (e.g. \"who am I?\", \"do you know who this "
            "is?\") or when you need to know who just spoke and no recent [SPEAKER] "
            "message tells you."
        ),
    },
]

VOICE_INSTRUCTIONS = (
    "# Recognizing voices\n"
    "You also recognize people you know by their voice, even when they're not in "
    "front of your camera. A message starting with \"[SPEAKER]\" tells you who is "
    "talking right now: \"[SPEAKER] Sarah (voice, sure)\" means it's Sarah; "
    "\"might be Sarah (voice, not sure)\" means check politely (\"Is that you, "
    "Sarah?\") before assuming; \"unknown voice\" means someone whose voice you "
    "don't know is talking. [SELF] updates say who is in view, which can be "
    "someone else: the [SPEAKER] message is about who is talking.\n"
    "If someone asks who they are, or you need to know who is talking and no "
    "recent [SPEAKER] message says, call identify_speaker. Never guess a name you "
    "aren't sure of — ask instead. Don't mention [SPEAKER] messages or scores."
)


class SpeakerNotes:
    """Decides which results are worth telling Gemini: a name or a 'might be'
    when the speaker changes (or every REPEAT_NOTE_S for the same speaker), and
    'unknown voice' only right after someone known was talking."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = None            # (level, person_id)
        self._last_t = -1e9

    def note_for(self, match, stage=None):
        if match.level not in ("sure", "maybe", "unknown"):
            return None                          # too short: nothing to say
        key = (match.level, match.person_id)
        if match.level == "unknown" and (self._last is None or self._last[0] == "unknown"):
            return None                          # nothing new to tell
        now = self._clock()
        if key == self._last and now - self._last_t < REPEAT_NOTE_S:
            return None
        self._last, self._last_t = key, now
        return match.describe()


# ─── Voice activity detection ───────────────────────────────────────────────
class SileroVAD:
    """Silero VAD (MIT license, ~2 MB, model bundled in the pip package)."""

    def __init__(self):
        import torch
        from silero_vad import load_silero_vad
        self._torch = torch
        self._model = load_silero_vad()

    def speech_prob(self, frame):
        with self._torch.no_grad():
            return float(self._model(self._torch.from_numpy(frame), VAD_RATE).item())

    def reset(self):
        self._model.reset_states()


class EnergyVAD:
    """Fallback: speech = clearly louder than the recent background noise."""

    def __init__(self, ratio=3.0, floor=0.003):
        self._ratio = ratio
        self._floor = floor
        self._noise = None

    def speech_prob(self, frame):
        rms = float(np.sqrt(np.mean(frame ** 2)))
        if self._noise is None:
            self._noise = rms
        loud = rms > max(self._floor, self._noise * self._ratio)
        if not loud:      # track the background level only during quiet frames
            self._noise = 0.95 * self._noise + 0.05 * rms
        return 1.0 if loud else 0.0

    def reset(self):
        pass


def make_vad():
    try:
        return SileroVAD()
    except Exception as e:
        print(f"[VOICE] Silero VAD unavailable ({e}); using a simple loudness detector.")
        return EnergyVAD()


def _to_vad_rate(audio, sample_rate):
    """Cheap per-chunk conversion to 16 kHz for the VAD only (identification
    uses the original audio, resampled properly)."""
    if sample_rate == VAD_RATE:
        return audio
    if sample_rate % VAD_RATE == 0:              # e.g. 48 kHz: average each 3 samples
        k = sample_rate // VAD_RATE
        n = len(audio) // k * k
        return audio[:n].reshape(-1, k).mean(axis=1)
    from scipy.signal import resample_poly
    g = int(np.gcd(int(sample_rate), VAD_RATE))
    return resample_poly(audio, VAD_RATE // g, int(sample_rate) // g).astype(np.float32)


# ─── Listener ────────────────────────────────────────────────────────────────
def print_result(match, stage):
    if match.level in ("sure", "maybe"):
        who = match.name if match.level == "sure" else f"might be {match.name}"
    else:
        who = match.level
    print(f"[VOICE] {who:<22} score={match.score:.2f}  {stage:<5} "
          f"{match.seconds:4.1f}s audio  {match.ms:4.0f} ms", flush=True)


class VoiceListener:
    """Who is speaking, live. Give it mono audio chunks with feed()."""

    def __init__(self, identifier, sample_rate, is_ember_speaking=None, on_result=None,
                 vad=None, clock=time.monotonic):
        self.identifier = identifier
        self.sample_rate = int(sample_rate)
        self._is_ember_speaking = is_ember_speaking or (lambda: False)
        self._on_result = on_result or print_result
        self._vad = vad
        self._clock = clock
        self._queue = queue.Queue(maxsize=QUEUE_CHUNKS)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._latest = None          # (VoiceMatch, stage, when)
        self._results = 0
        self.dropped = 0
        self._reset_utterance()
        self._pre = deque()              # recent quiet audio, up to PRE_ROLL_S
        self._pre_samples = 0
        self._vad_buf = np.zeros(0, np.float32)
        self._ember_last = -1e9

    # ── Called from the mic callback: must never block ───────────────────────
    def feed(self, chunk):
        """chunk: mono int16 bytes, or a mono numpy array (int16 or float).
        Whether Ember is talking is checked now, when the audio was captured,
        not later when the thread gets to it."""
        try:
            self._queue.put_nowait((chunk, bool(self._is_ember_speaking()), self._clock()))
        except queue.Full:
            self.dropped += 1            # identification falls behind; audio to Gemini is unaffected

    # ── Results ──────────────────────────────────────────────────────────────
    def latest(self, max_age=None):
        """The most recent (VoiceMatch, stage, when), or None (or too old)."""
        with self._lock:
            latest = self._latest
        if latest and max_age is not None and self._clock() - latest[2] > max_age:
            return None
        return latest

    def speaker_report(self, wait=TOOL_WAIT_S, max_age=RESULT_MAX_AGE_S):
        """For the identify_speaker tool: who is talking, as a dict for Gemini.
        If someone is mid-sentence (e.g. "...who am I?"), waits up to `wait`
        seconds for that sentence's result."""
        with self._cond:
            start = self._results
            if self._in_speech or self._queue.qsize() > 5:
                self._cond.wait_for(lambda: self._results > start and not self._in_speech,
                                    timeout=wait)
            latest = self._latest
        if not latest or self._clock() - latest[2] > max_age:
            return {"speaker": None,
                    "note": "You haven't heard enough of their voice yet. If you need to "
                            "know who it is, ask them."}
        m = latest[0]
        if m.level == "sure":
            return {"speaker": m.name, "confidence": "sure"}
        if m.level == "maybe":
            return {"speaker": m.name, "confidence": "not sure",
                    "note": f"It might be {m.name}. Check politely before assuming."}
        return {"speaker": None,
                "note": "You don't recognize this voice. If it matters, ask who they are."}

    # ── Thread ───────────────────────────────────────────────────────────────
    def start(self):
        if self._vad is None:
            self._vad = make_vad()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice-id", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process(*chunk)
            except Exception as e:          # one bad chunk mustn't stop recognition
                print(f"[VOICE] Error: {e}", flush=True)
                self._reset_utterance()

    # ── Core logic (also called directly by tests) ───────────────────────────
    def _reset_utterance(self):
        self._utt = []                   # audio chunks at the source rate
        self._utt_samples = 0
        self._speech_s = 0.0             # seconds of frames judged as speech
        self._silence_s = 0.0            # silence since the last speech frame
        self._in_speech = False
        self._early_done = False

    def process(self, chunk, ember_speaking=None, captured=None):
        audio = (np.frombuffer(chunk, np.int16) if isinstance(chunk, (bytes, bytearray))
                 else np.asarray(chunk))
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        audio = audio.astype(np.float32).ravel()

        now = self._clock() if captured is None else captured
        if ember_speaking is None:
            ember_speaking = self._is_ember_speaking()
        if ember_speaking:
            self._ember_last = now
        if now - self._ember_last < ECHO_TAIL_S:
            # Ember is talking (or just stopped): ignore, and drop any half utterance
            if self._in_speech:
                self._finish(identify=False)
            self._clear_pre()
            self._vad_buf = np.zeros(0, np.float32)
            return

        # VAD on 512-sample frames at 16 kHz
        self._vad_buf = np.concatenate([self._vad_buf, _to_vad_rate(audio, self.sample_rate)])
        frames = len(self._vad_buf) // VAD_FRAME
        speech_frames = 0
        for i in range(frames):
            frame = self._vad_buf[i * VAD_FRAME:(i + 1) * VAD_FRAME]
            speech_frames += self._vad.speech_prob(frame) >= SPEECH_PROB
        self._vad_buf = self._vad_buf[frames * VAD_FRAME:]
        frame_s = VAD_FRAME / VAD_RATE
        chunk_has_speech = speech_frames > 0

        if not self._in_speech:
            if chunk_has_speech:
                self._in_speech = True
                for pre in self._pre:            # include the start of the first word
                    self._add(pre)
                self._clear_pre()
            else:
                self._pre.append(audio)
                self._pre_samples += len(audio)
                while self._pre and self._pre_samples - len(self._pre[0]) >= PRE_ROLL_S * self.sample_rate:
                    self._pre_samples -= len(self._pre.popleft())
                return
        self._add(audio)

        if chunk_has_speech:
            self._speech_s += speech_frames * frame_s
            self._silence_s = 0.0
        else:
            self._silence_s += len(audio) / self.sample_rate

        if not self._early_done and self._speech_s >= EARLY_ID_S:
            self._early_done = True
            self._identify("early")
        if self._silence_s >= END_SILENCE_S:
            self._finish()
        elif self._utt_samples / self.sample_rate >= MAX_UTTERANCE_S:
            self._finish()

    def _clear_pre(self):
        self._pre.clear()
        self._pre_samples = 0

    def _add(self, audio):
        self._utt.append(audio)
        self._utt_samples += len(audio)

    def _finish(self, identify=True):
        if identify and self._speech_s >= voice_id.MIN_SECONDS:
            self._identify("final")
        with self._cond:
            self._reset_utterance()
            self._cond.notify_all()
        self._vad.reset()

    def _identify(self, stage):
        audio = np.concatenate(self._utt) if self._utt else np.zeros(0, np.float32)
        match = self.identifier.identify(audio, self.sample_rate)
        with self._cond:
            self._latest = (match, stage, self._clock())
            self._results += 1
            self._cond.notify_all()
        self._on_result(match, stage)


# ─── Laptop test mode ────────────────────────────────────────────────────────
def _main():
    import argparse
    import os
    import sys
    import sounddevice as sd

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "voice_proto"))
    from dev_store import DevPeopleStore

    p = argparse.ArgumentParser(description="Live voice ID on this computer's mic.")
    dev_store = os.path.join(here, "voice_proto", "dev_people.json")
    p.add_argument("--store", default=dev_store,
                   help="people file (default: the test store; use ember_people.json only "
                        "while Ember is stopped)")
    p.add_argument("--rate", type=int, default=48000, help="mic sample rate (Ember uses 48000)")
    p.add_argument("--enroll", metavar="NAME", help="record 3 clips and learn this voice first")
    args = p.parse_args()

    print("Loading voice model...")
    ids = voice_id.VoiceIdentifier(voice_id.VoiceModel(), DevPeopleStore(args.store))

    if args.enroll:
        clips = []
        for i in range(3):
            input(f"[{i + 1}/3] Press Enter and talk naturally for 4 seconds...")
            clip = sd.rec(int(4 * args.rate), samplerate=args.rate, channels=1, dtype="int16")
            sd.wait()
            clips.append(clip[:, 0])
        # In the test store anyone can be added; in Ember's real file the person
        # must already be known by face (see voice_id.enroll)
        new_ok = os.path.abspath(args.store) == os.path.abspath(dev_store)
        print(ids.enroll(args.enroll, clips, args.rate, consent=True, allow_new=new_ok))

    names = [n for _, n, _ in ids.voiceprints()]
    print(f"Known voices: {', '.join(names) or 'none (use --enroll NAME)'}")
    listener = VoiceListener(ids, args.rate).start()
    print("Listening... talk, pause, talk again. Ctrl+C to stop.\n")
    with sd.InputStream(samplerate=args.rate, channels=1, dtype="int16", blocksize=1024,
                        callback=lambda indata, *_: listener.feed(indata[:, 0].tobytes())):
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    listener.stop()
    if listener.dropped:
        print(f"(dropped {listener.dropped} chunks: identification fell behind)")


if __name__ == "__main__":
    _main()
