"""Answer only the latest question: drop a stale reply when Gemini answers a newer one.

The MVP runs Gemini with interruptions off (NO_INTERRUPTION), so every separate
thing the person says gets its own reply, in order. When Gemini is slow and the
person asks again ("Are you okay?" ... no answer ... "Ember, are you okay?"), or
adds something while Ember answers, Ember used to answer the first question and
then immediately the second. Reply audio also arrives faster than it plays, so it
queues locally (up to ~8 s on the robot, 2026-11-10).

A reply is stale when the person has said something newer than what it answers:
they said two or more separate things since Ember's previous reply (Gemini answers
the oldest first), or anything new after the reply started. When the next reply
starts, whatever is still queued of a stale reply is dropped and only the newer
reply plays. If Gemini answers everything in one reply, nothing is dropped.

Kept: the answer after a tool call (it finishes the same reply), and replies to
Ember's own messages (reminders, fall check-ins) after a reply nobody superseded.
Speech alone never cuts Ember off; that stays Gemini's interruption setting, so
Ember's own echo cannot silence it. A prompt section asks Gemini to answer only the
person's latest words.

Separate things: speech fragments more than GAP_S apart, or speech starting more
than SETTLE_S after a reply began. Transcription arrives word by word and can
trail the reply it caused; that tail is the same utterance.

Inputs: Gemini's input transcription, reply-start and tool-call events from the
existing receive loop. Output: a flushed playback buffer and a [LATEST] log line.
No device, request or file is added. BAYMAX_LATEST_REPLY_ONLY=0 turns it off.
Tests: tests/test_latest_reply.py.
"""
import copy
import os
import time

import runtime_monitor as monitor
from .addressing import is_speech

MARKER = "[LATEST REMARK]"
INSTRUCTION = """

[LATEST REMARK]
If the person asks again or says something new before you have answered, answer only
their latest words. Do not answer the earlier question separately unless they ask.
They may not have heard the end of your last reply.
"""
GAP_S = 1.0     # Silence between fragments that makes the next one a separate utterance.
SETTLE_S = 0.7  # Transcription this soon after a reply starts is the utterance it answers.


def enabled():
    return os.getenv("BAYMAX_LATEST_REPLY_ONLY", "1") != "0"


def apply(config):
    """Add the latest-remark section once to the MVP persona; leave other prompts alone."""
    from features.persona import PERSONA
    instruction = config.get("system_instruction", "")
    if not enabled() or PERSONA not in instruction or MARKER in instruction:
        return config
    result = copy.deepcopy(config)
    result["system_instruction"] = instruction + INSTRUCTION
    return result


class LatestReply:
    def __init__(self, enabled=True, clock=time.monotonic, log=print, gap_s=GAP_S, settle_s=SETTLE_S):
        self.enabled, self.clock, self.log = enabled, clock, log
        self.gap_s, self.settle_s = gap_s, settle_s
        self.reset()

    def reset(self):
        """New session: nothing queued belongs to a reply this object has seen."""
        self.reply_started = None
        self.last_speech = None
        self.before = 0  # Separate utterances heard before the current reply started.
        self.since = 0   # Separate utterances heard since it started.
        self.continuation = False

    def heard(self, text):
        """Gemini's transcription of the person (fragments arrive word by word)."""
        if not self.enabled or not is_speech(text):
            return
        now, started, last = self.clock(), self.reply_started, self.last_speech
        after_reply = started is not None and (last is None or last <= started)
        if after_reply and now - started < self.settle_s:
            new = False  # Tail of the utterance this reply answers.
        else:
            new = last is None or now - last >= self.gap_s or after_reply
        self.last_speech = now
        if new:
            self.since += 1

    def tool_call(self):
        self.continuation = True  # The next reply finishes the current answer.

    def new_reply(self):
        """First audio of a new reply. True: drop what is still queued of the old one."""
        now = self.clock()
        if self.continuation:
            self.continuation = False
            self.reply_started = now
            return False
        stale = self.enabled and self.reply_started is not None and (self.before >= 2 or self.since >= 1)
        self.before, self.since = self.since, 0
        self.reply_started = now
        return stale

    def dropped(self, seconds):
        message = f"Dropped {seconds:.1f}s of a reply to an earlier question; answering the latest"
        self.log(f"[LATEST] {message}.", flush=True)
        monitor.record("conversation", message)
