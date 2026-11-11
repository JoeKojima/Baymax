"""Ember answers only the latest remark: the policy alone, then v10's real receive loop."""
import asyncio
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from features import persona
from features.conversational_ability import latest_reply
from features.conversational_ability.addressing import AddressGate
from features.conversational_ability.latest_reply import LatestReply
from test_address_gate import content
from test_public_core_integration import load_function


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def policy(**kwargs):
    clock = Clock()
    return LatestReply(clock=clock, log=Mock(), **kwargs), clock


class LatestReplyPolicyTests(unittest.TestCase):
    def test_a_new_remark_after_a_reply_starts_replaces_the_queued_reply(self):
        latest, clock = policy()
        self.assertFalse(latest.new_reply())  # Nothing earlier to replace.
        clock.now += 1.5
        latest.heard("Are you ready?")
        self.assertTrue(latest.new_reply())
        self.assertFalse(latest.new_reply())  # Each remark replaces at most once.

    def test_asking_again_before_any_answer_makes_the_first_answer_stale(self):
        # Gemini is slow: "Are you okay?" ... 6 s ... "Ember, are you okay?" ... both answers arrive.
        latest, clock = policy()
        latest.heard("Are you")
        clock.now += .2
        latest.heard(" okay?")
        clock.now += 6
        latest.heard("Ember, are you okay?")
        self.assertFalse(latest.new_reply())  # First answer: nothing earlier to replace.
        clock.now += 3
        self.assertTrue(latest.new_reply())   # Second answer replaces the first.

    def test_one_question_in_fragments_is_one_utterance(self):
        latest, clock = policy()
        for word in ("If", " it", " runs", " into", " ten problems"):
            latest.heard(word)
            clock.now += .3
        latest.new_reply()
        clock.now += 3
        self.assertFalse(latest.new_reply())  # e.g. a reminder after the answer: kept.

    def test_trailing_transcription_of_the_same_utterance_is_not_a_new_remark(self):
        latest, clock = policy()
        latest.new_reply()
        clock.now += latest_reply.SETTLE_S / 2
        latest.heard("capabilities.")
        self.assertFalse(latest.new_reply())

    def test_noise_and_speech_before_any_reply_do_not_count(self):
        latest, clock = policy()
        latest.heard("Hello Ember")  # No reply yet: nothing could be queued.
        self.assertFalse(latest.new_reply())
        clock.now += 2
        latest.heard("<noise>")
        latest.heard("  ")
        self.assertFalse(latest.new_reply())

    def test_the_answer_after_a_tool_call_is_never_dropped(self):
        latest, clock = policy()
        latest.new_reply()  # "Let me check the weather."
        clock.now += 2
        latest.heard("thanks")
        latest.tool_call()
        self.assertFalse(latest.new_reply())

    def test_disabled_keeps_every_reply_and_reset_forgets_the_old_session(self):
        latest, clock = policy(enabled=False)
        latest.new_reply()
        clock.now += 2
        latest.heard("Are you ready?")
        self.assertFalse(latest.new_reply())
        latest, clock = policy()
        latest.new_reply()
        latest.reset()
        clock.now += 2
        latest.heard("Are you ready?")
        self.assertFalse(latest.new_reply())

    def test_drop_is_logged_and_recorded(self):
        latest, _ = policy()
        with patch.object(latest_reply.monitor, "record") as record:
            latest.dropped(7.86)
        self.assertIn("Dropped 7.9s", latest.log.call_args.args[0])
        record.assert_called_once_with("conversation",
                                       "Dropped 7.9s of a reply to an earlier question; answering the latest")

    def test_setting(self):
        for env, expected in [({}, True), ({"BAYMAX_LATEST_REPLY_ONLY": "0"}, False),
                              ({"BAYMAX_LATEST_REPLY_ONLY": "1"}, True)]:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(latest_reply.enabled(), expected)


class LatestReplyPromptTests(unittest.TestCase):
    def test_section_is_added_once_to_the_persona_only(self):
        config = {"system_instruction": persona.PERSONA, "other": {"kept": True}}
        with patch.dict(os.environ, {}, clear=True):
            once = latest_reply.apply(config)
            self.assertEqual(once["system_instruction"], persona.PERSONA + latest_reply.INSTRUCTION)
            self.assertIs(latest_reply.apply(once), once)
            other = {"system_instruction": "upstream prompt"}
            self.assertIs(latest_reply.apply(other), other)
        self.assertEqual(config["system_instruction"], persona.PERSONA)  # Original untouched.
        with patch.dict(os.environ, {"BAYMAX_LATEST_REPLY_ONLY": "0"}):
            self.assertIs(latest_reply.apply(config), config)

    def test_session_config_carries_the_section(self):
        from features.toolkits import gemini_tools
        with patch.dict(os.environ, {"BAYMAX_LATEST_REPLY_ONLY": "1"}):
            config = gemini_tools.extend_config({"system_instruction": persona.PERSONA})
        self.assertIn(latest_reply.MARKER, config["system_instruction"])


def pcm(value, samples=2400):  # 0.1 s of 24 kHz reply audio from Gemini
    return np.full(samples, value, dtype=np.int16).tobytes()


def wait(seconds):
    return lambda clock: setattr(clock, "now", clock.now + seconds)


def tool_call():
    return SimpleNamespace(tool_call=SimpleNamespace(function_calls=[]), server_content=None,
                           session_resumption_update=None, go_away=None)


class V10LatestReplyTests(unittest.TestCase):
    """Replies through v10's real receive_audio with a playback buffer that drains in real time."""

    def run_receive(self, responses, enabled=True):
        clock = Clock()
        latest = LatestReply(enabled=enabled, clock=clock, log=Mock())
        latest.dropped = Mock()
        namespace, heard = {}, []  # heard: what the speaker played, in order.

        class Session:
            def receive(self):
                async def stream():
                    for response in responses:
                        if callable(response):
                            start = clock.now
                            response(clock)
                            played = int((clock.now - start) * 48000) * 2  # The speaker drains it.
                            heard.append(namespace["_playback_buffer"][:played])
                            namespace["_playback_buffer"] = namespace["_playback_buffer"][played:]
                            continue
                        yield response
                    raise asyncio.CancelledError()
                return stream()

        def append_playback(data, *, new_turn=False):
            namespace["_playback_buffer"] += data

        def flush_playback():
            namespace["_playback_buffer"] = b""

        namespace.update({
            "asyncio": asyncio, "np": np, "_address_gate": AddressGate(enabled=False, clock=clock),
            "_latest_reply": latest,
            "time": SimpleNamespace(monotonic=clock, perf_counter=time.perf_counter),
            "public_toolkit": SimpleNamespace(handle_tool_call=AsyncMock()),
            "monitor": SimpleNamespace(emit=Mock(), transcribe=Mock(), finish_transcripts=Mock()),
            "_transcript_lock": threading.Lock(), "_transcript_user": [], "_transcript_gemini": [],
            "_TurnTranscript": lambda *a: SimpleNamespace(add=Mock(), flush=Mock()),
            "_initiation_ts_lock": threading.Lock(), "_recent_user_words_lock": threading.Lock(),
            "_recent_user_words": [], "MEMORY_WORD_WINDOW": 50, "_memory_embedder": None,
            "_gemini_speaking_lock": threading.Lock(), "_gemini_speaking": False,
            "_append_playback": append_playback, "_flush_playback": flush_playback,
            "_playback_buffer": b"", "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1,
            "tracker_receive": SimpleNamespace(record=Mock()), "tracker_first_audio": SimpleNamespace(record=Mock()),
            "tracker_roundtrip": SimpleNamespace(record=Mock()), "_last_mic_send_ts": 0.0,
            "_last_robot_speech_ts": 0.0, "_last_user_speech_ts": 0.0,
        })
        receive = load_function("receive_audio", namespace)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(receive(Session()))
        heard.append(namespace["_playback_buffer"])  # The rest plays after the last message.
        return np.frombuffer(b"".join(heard), dtype=np.int16), latest

    def first_reply(self):
        # 2026-11-10 18:50 on the robot: the first answer is generated in full
        # (4 s of audio queued) before the person adds "Are you ready?".
        return [content(user="Yes, I want to test your language speaking capabilities."),
                content(ember="That sounds like fun.", audio=pcm(1)),
                *[content(audio=pcm(1)) for _ in range(39)],
                content(ember="What language would you like to try?", done=True)]

    def test_slow_gemini_answers_only_the_question_asked_again(self):
        # Ask, no answer, ask again; Gemini then sends an answer to each, back to back.
        played, latest = self.run_receive([
            content(user="What's the capital of Peru?"), wait(6),
            content(user="Ember, what is the capital of Peru?"), wait(2),
            content(ember="The capital of Peru is Lima.", audio=pcm(1)),
            *[content(audio=pcm(1)) for _ in range(29)], content(done=True),
            content(ember="It's Lima.", audio=pcm(2)), content(audio=pcm(2)), content(done=True)])
        np.testing.assert_array_equal(played, np.full(9600, 2))  # Only the second answer.
        latest.dropped.assert_called_once_with(3.0)

    def test_one_answer_to_both_questions_plays_in_full(self):
        played, latest = self.run_receive([
            content(user="What's the capital of Peru?"), wait(6),
            content(user="Ember, what is the capital of Peru?"), wait(2),
            content(ember="It's Lima.", audio=pcm(1)), content(audio=pcm(1)), content(done=True),
            wait(5), content(user="Thanks."), wait(1),
            content(ember="You're welcome.", audio=pcm(2)), content(done=True)])
        self.assertEqual(np.count_nonzero(played == 1), 2 * 4800)
        self.assertEqual(np.count_nonzero(played == 2), 4800)
        latest.dropped.assert_not_called()

    def test_only_the_reply_to_the_latest_remark_is_left_to_play(self):
        played, latest = self.run_receive([
            *self.first_reply(), wait(1.5), content(user="Are you ready?"),
            content(ember="I'm ready.", audio=pcm(2)), content(audio=pcm(2)), content(done=True)])
        # 1.5 s of the first answer played while they spoke; the rest gave way to "I'm ready."
        np.testing.assert_array_equal(played, np.concatenate((np.full(72000, 1), np.full(9600, 2))))
        latest.dropped.assert_called_once_with(2.5)

    def test_disabled_queues_both_replies_as_before(self):
        played, latest = self.run_receive([
            *self.first_reply(), wait(1.5), content(user="Are you ready?"),
            content(ember="I'm ready.", audio=pcm(2)), content(done=True)], enabled=False)
        self.assertEqual(np.count_nonzero(played == 1), 40 * 4800)
        self.assertEqual(np.count_nonzero(played == 2), 4800)
        latest.dropped.assert_not_called()

    def test_embers_own_follow_up_without_new_speech_keeps_the_queued_reply(self):
        # e.g. a reminder or a reply after Ember's own message: nobody spoke, nothing replaced.
        played, latest = self.run_receive([
            *self.first_reply(), wait(1.5), content(user="<noise>"),
            content(ember="By the way, it's time for your tablets.", audio=pcm(3)), content(done=True)])
        self.assertEqual(np.count_nonzero(played == 1), 40 * 4800)
        self.assertEqual(np.count_nonzero(played == 3), 4800)
        latest.dropped.assert_not_called()

    def test_answer_after_a_tool_call_follows_the_queued_sentence(self):
        played, latest = self.run_receive([
            content(user="What's the weather?"), content(ember="Let me check.", audio=pcm(1)),
            wait(1.0), content(user="in Philadelphia"), tool_call(), content(done=True),
            content(ember="It's 15 degrees.", audio=pcm(2)), content(done=True)])
        self.assertEqual(np.count_nonzero(played == 1), 4800)
        self.assertEqual(np.count_nonzero(played == 2), 4800)
        latest.dropped.assert_not_called()


if __name__ == "__main__":
    unittest.main()
