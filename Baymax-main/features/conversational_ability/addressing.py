"""Play Gemini's reply only when someone was talking to Ember.

Proactive audio and the [WHEN TO SPEAK] prompt only ask Gemini to stay quiet;
it still answers noise and people talking among themselves. This gate decides
on the robot, from Gemini's own input transcription, whether a reply is played:

- the person said Ember's name (or a usual mishearing such as "Amber"), or
- they spoke within BAYMAX_FOLLOW_UP_SECONDS (default 20) of Ember finishing
  speaking, so they are still in a conversation with it, or
- the reply answers one of Ember's own messages ([FALL ALERT], [REMINDER],
  [STARTUP], ...): any send_client_content(turn_complete=True) through the
  watched session, plus the reply after a tool call from an addressed turn.

Transcription can arrive after the first reply audio, so an undecided reply is
held (audio and transcript) and released if the name shows up before the turn
ends; otherwise it is dropped and logged. Gemini is not told a reply was
dropped. Noise tags such as "<noise>" are not speech.

conversation.only_when_addressed = false in device.toml (or
BAYMAX_ADDRESS_GATE=0) plays every reply again. Nothing is sent anywhere and no
device is opened: the gate only reads the existing transcription.
"""
import os
import re
import threading
import time

PROMPT_TTL_S = 30  # A prompt Gemini never answered must not let a later overheard reply through.
NAMES = r"(?:ember|embers|amber|ambers|emba|embar|emmer|emperor|ember's|amber's)"
NAME = re.compile(rf"\b{NAMES}\b")
WHOLE_NAME = re.compile(rf"{NAMES}")
NOISE = re.compile(r"<[^>]*>")


def follow_up_seconds():
    try:
        return max(0.0, float(os.getenv("BAYMAX_FOLLOW_UP_SECONDS", "20")))
    except ValueError:
        return 20.0


def names_ember(text):
    # Fragments arrive mid-word and sometimes with a space inside the word
    # ("A mber", "E mperor" on ember-002), so two neighbouring pieces that
    # together make the name count too; "a member" or "the member" do not.
    text = text.lower()
    if NAME.search(text):
        return True
    words = re.findall(r"[a-z']+", text)
    return any(WHOLE_NAME.fullmatch(a + b) for a, b in zip(words, words[1:]))


def is_speech(text):
    return bool(re.search(r"\w", NOISE.sub(" ", text)))


class AddressGate:
    def __init__(self, enabled=True, follow_up=None, clock=time.monotonic, log=print):
        self.enabled = enabled
        self.follow_up = follow_up_seconds() if follow_up is None else follow_up
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        self._heard = []          # user transcription since the last reply
        self._heard_at = None     # when that speech started
        self._prompted_at = None  # when Ember's own message, still unanswered, was sent
        self._answers_prompt = False  # this reply was let through for that message
        self._spoke_until = None  # when Ember's last played reply ends
        self._decision = None     # None (undecided) / True (played) for this reply
        self._held = []           # actions held while undecided
        self._held_text = []

    # ── what happened ────────────────────────────────────────────────────────
    def heard(self, text):
        """User transcription fragment. Returns held actions to run now, if released."""
        with self._lock:
            if self._heard_at is None and is_speech(text):
                self._heard_at = self.clock()
            self._heard.append(text)
            return self._release()

    def prompted(self):
        """Ember's own systems sent Gemini a message; the next reply answers it."""
        with self._lock:
            self._prompted_at = self.clock()

    def tool_call(self):
        """A tool call from an addressed turn keeps the reply after its result."""
        with self._lock:
            if self._decision is True or self._addressed():
                self._prompted_at = self.clock()

    def spoke(self, until):
        """Played reply audio is buffered until `until` (clock time)."""
        with self._lock:
            self._spoke_until = max(self._spoke_until or until, until)

    # ── the decision ─────────────────────────────────────────────────────────
    def _release(self):
        # Caller holds the lock. Held actions to run if an undecided reply is now allowed.
        if self._decision is None and self._held and self._addressed(prompt=False):
            self._decision = True
            held, self._held, self._held_text = self._held, [], []
            return held
        return []

    def _prompt_waiting(self):
        return self._prompted_at is not None and self.clock() - self._prompted_at <= PROMPT_TTL_S

    def _addressed(self, prompt=True):
        # A prompt counts only when a reply starts: one sent mid-reply is
        # answered by the next reply, not the one being held.
        if not self.enabled or (prompt and self._prompt_waiting()):
            return True
        heard = "".join(self._heard)
        if names_ember(heard):
            return True
        if not is_speech(heard) or self._spoke_until is None:
            return False
        started = self._heard_at if self._heard_at is not None else self.clock()
        return started - self._spoke_until <= self.follow_up

    def reply(self, action, text=None):
        """Run `action` now if this reply may be heard, else hold it. Returns True if run."""
        with self._lock:
            if self._decision is None and self._addressed(prompt=not self._held):
                self._decision = True
                self._answers_prompt = not self._held and self._prompt_waiting()
            if self._decision is True:
                ready, self._held, self._held_text = self._held + [action], [], []
            else:
                self._held.append(action)
                if text:
                    self._held_text.append(text)
                ready = []
        for queued in ready:
            queued()
        return bool(ready)

    def end(self, interrupted=False):
        """The reply is over (turn_complete or interrupted)."""
        with self._lock:
            dropped = self._decision is not True and bool(self._held)
            text = " ".join("".join(self._held_text).split())
            if self._answers_prompt and not interrupted:
                self._prompted_at = None
            self._answers_prompt = False
            self._heard, self._heard_at = [], None
            self._decision, self._held, self._held_text = None, [], []
        if dropped:
            self.log(f"[ADDRESS] Not spoken to; dropped reply: {text or '(audio only)'}", flush=True)

    # ── session hook ─────────────────────────────────────────────────────────
    def watch(self, session):
        return _WatchedSession(session, self)


class _WatchedSession:
    """The live session, noting each complete message Ember's systems send."""

    def __init__(self, session, gate):
        self._session = session
        self._gate = gate

    async def send_client_content(self, *args, **kwargs):
        # Noted before sending: the reply must never arrive before the note.
        if kwargs.get("turn_complete", True):
            self._gate.prompted()
        return await self._session.send_client_content(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)
