"""Answer only the latest remark: drop Ember's stale queued reply when a newer one starts.

Gemini sends reply audio faster than it plays, so Ember queues it locally. The MVP
runs Gemini with interruptions off (NO_INTERRUPTION), so when the person adds
something while Ember is answering ("...test your languages." / "Are you ready?"),
Gemini finishes the first reply and then answers again, and both replies played
back to back - up to ~8 s of queued audio on the robot (2026-11-10).

With this on (default), when a new reply starts and the person spoke after the
previous reply began, whatever is still queued of the previous reply is dropped
and only the new one plays. Nothing is cut while the person merely speaks (that
stays Gemini's interruption setting, and keeps Ember's own echo from silencing
it), the continuation after a tool call is never dropped, and replies to Ember's
own messages (reminders, fall check-ins) only replace audio if the person spoke.
A prompt section asks Gemini to answer only the most recent remark.

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
If the person says something new before you have answered, or while you are still
talking, answer only their most recent remark or question. Do not go back to answer
their earlier ones unless they ask. They may not have heard the end of your last reply.
"""
# Transcription of the utterance that started a reply can trail its first audio;
# speech that soon after a reply starts is that same utterance, not a new remark.
SETTLE_S = 0.7


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
    def __init__(self, enabled=True, clock=time.monotonic, log=print, settle_s=SETTLE_S):
        self.enabled, self.clock, self.log, self.settle_s = enabled, clock, log, settle_s
        self.reset()

    def reset(self):
        """New session: nothing queued belongs to a reply this object has seen."""
        self.reply_started = None
        self.spoke_since_reply = False
        self.continuation = False

    def heard(self, text):
        """Gemini's transcription of the person (fragments arrive word by word)."""
        if (self.enabled and self.reply_started is not None and is_speech(text)
                and self.clock() - self.reply_started >= self.settle_s):
            self.spoke_since_reply = True

    def tool_call(self):
        self.continuation = True  # The next reply finishes the current answer.

    def new_reply(self):
        """First audio of a new reply. True: drop what is still queued of the old one."""
        drop = self.enabled and self.spoke_since_reply and not self.continuation
        self.reply_started = self.clock()
        self.spoke_since_reply = self.continuation = False
        return drop

    def dropped(self, seconds):
        message = f"Dropped {seconds:.1f}s of an earlier reply; answering the latest remark"
        self.log(f"[LATEST] {message}.", flush=True)
        monitor.record("conversation", message)
