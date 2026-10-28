"""Ember plays a reply only when spoken to: the gate alone, then v10's real receive loop."""
import asyncio
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from features.conversational_ability import addressing
from features.conversational_ability.addressing import AddressGate
from test_public_core_integration import load_function


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def gate(**kwargs):
    clock = Clock()
    log = Mock()
    return AddressGate(follow_up=20, clock=clock, log=log, **kwargs), clock, log


def turn(g, user, reply="ok"):
    """One exchange: the user's words, then Gemini's reply. Returns what played."""
    played = []
    for word in user:
        for held in g.heard(word):
            held()
    g.reply(lambda: played.append(reply), reply)
    g.end()
    return played


class AddressGateTests(unittest.TestCase):
    def test_name_lets_a_reply_through_and_mishearings_count(self):
        for words in (["Ember,", " can you hear me?"], ["Hey", " A", "mber"], ["amber what time is it"],
                      ["Hey A mber , wha t's the weather"], ["Hey , E mperor , what 's the weather"],
                      ["E mber"]):
            g, _, _ = gate()
            self.assertEqual(turn(g, words), ["ok"], words)

    def test_side_conversation_and_noise_are_dropped_and_logged(self):
        g, _, log = gate()
        self.assertEqual(turn(g, ["thanks for calling. I did finish my homework."], "That's good to hear."), [])
        self.assertEqual(turn(g, ["<noise>"]), [])
        self.assertIn("That's good to hear.", log.call_args_list[0].args[0])
        self.assertTrue(addressing.names_ember("Ember's here"))
        for text in ("remember this", "a member of the club", "the member", "in November", "a chamber"):
            self.assertFalse(addressing.names_ember(text), text)

    def test_follow_up_window_starts_when_ember_stops_talking(self):
        g, clock, _ = gate()
        self.assertEqual(turn(g, ["Ember, hello"]), ["ok"])
        g.spoke(clock.now + 5)          # five seconds of reply still buffered
        clock.now += 24                 # the person answers 19 s after Ember finished
        self.assertEqual(turn(g, ["is my voice clear?"]), ["ok"])
        g.spoke(clock.now)
        clock.now += 21
        self.assertEqual(turn(g, ["is my voice clear?"]), [])

    def test_noise_inside_the_window_is_not_an_answer(self):
        g, clock, _ = gate()
        turn(g, ["Ember, hi"])
        g.spoke(clock.now)
        clock.now += 5
        self.assertEqual(turn(g, ["<noise>"], "You look tired."), [])

    def test_embers_own_messages_always_get_their_reply_once(self):
        g, clock, _ = gate()
        g.prompted()
        self.assertEqual(turn(g, []), ["ok"])
        clock.now += 100
        self.assertEqual(turn(g, ["people chatting"]), [])
        g.prompted()
        clock.now += addressing.PROMPT_TTL_S + 1   # Gemini never answered that one
        self.assertEqual(turn(g, ["people chatting"]), [])

    def test_late_transcription_releases_the_held_reply_in_order(self):
        g, _, _ = gate()
        played = []
        g.heard("Hey")
        g.reply(lambda: played.append(1), "one")
        g.reply(lambda: played.append(2), "two")
        self.assertEqual(played, [])
        for held in g.heard(" Ember"):
            held()
        g.reply(lambda: played.append(3))
        g.end()
        self.assertEqual(played, [1, 2, 3])

    def test_prompt_sent_mid_reply_does_not_release_an_overheard_reply(self):
        g, _, _ = gate()
        played = []
        g.heard("people chatting")
        g.reply(lambda: played.append("overheard"))
        g.prompted()
        g.reply(lambda: played.append("overheard"))
        g.end()
        self.assertEqual(played, [])
        self.assertEqual(turn(g, [], "alert answer"), ["alert answer"])  # the next reply answers it
        self.assertEqual(turn(g, ["people chatting"]), [])

    def test_tool_call_from_an_addressed_turn_keeps_its_answer(self):
        g, _, _ = gate()
        g.heard("Ember, what's the weather?")
        g.tool_call()
        g.end()                         # the tool-call turn ends before the result is spoken
        self.assertEqual(turn(g, [], "Sunny."), ["Sunny."])
        g.heard("they said it's sunny")
        g.tool_call()
        g.end()
        self.assertEqual(turn(g, [], "Sunny."), [])

    def test_disabled_gate_plays_everything(self):
        g, _, _ = gate(enabled=False)
        self.assertEqual(turn(g, ["<noise>"]), ["ok"])

    def test_watched_session_notes_complete_messages_only(self):
        sent = []
        async def send_client_content(**kwargs):
            sent.append(kwargs)
        g, _, _ = gate()
        session = g.watch(SimpleNamespace(send_client_content=send_client_content, other="kept"))
        asyncio.run(session.send_client_content(turns={"parts": [{"text": "memory"}]}, turn_complete=False))
        self.assertEqual(turn(g, ["chatting"]), [])
        asyncio.run(session.send_client_content(turns={"parts": [{"text": "[FALL ALERT]"}]}, turn_complete=True))
        self.assertEqual(turn(g, []), ["ok"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(session.other, "kept")

    def test_follow_up_setting(self):
        with patch.dict(os.environ, {"BAYMAX_FOLLOW_UP_SECONDS": "35"}):
            self.assertEqual(addressing.follow_up_seconds(), 35)
        with patch.dict(os.environ, {"BAYMAX_FOLLOW_UP_SECONDS": "soon"}):
            self.assertEqual(addressing.follow_up_seconds(), 20)


def content(user=None, ember=None, audio=None, done=False):
    model_turn = None
    if audio is not None:
        model_turn = SimpleNamespace(parts=[SimpleNamespace(
            text=None, inline_data=SimpleNamespace(mime_type="audio/pcm;rate=24000", data=audio))])
    return SimpleNamespace(tool_call=None, session_resumption_update=None, go_away=None,
                           server_content=SimpleNamespace(
                               input_transcription=SimpleNamespace(text=user) if user else None,
                               output_transcription=SimpleNamespace(text=ember) if ember else None,
                               model_turn=model_turn, turn_complete=done, interrupted=False))


class V10ReceiveLoopTests(unittest.TestCase):
    """The 2026-10-21 ember-002 transcript through v10's real receive_audio."""

    def run_receive(self, responses):
        class Session:
            def receive(self):
                async def stream():
                    for response in responses:
                        if callable(response):
                            response(clock)  # time passes between messages
                            continue
                        yield response
                    raise asyncio.CancelledError()
                return stream()
        played, said = [], []
        g, clock, log = gate()
        namespace = {
            "asyncio": asyncio, "np": np, "_address_gate": g,
            "time": SimpleNamespace(monotonic=clock, perf_counter=time.perf_counter),
            "public_toolkit": SimpleNamespace(), "monitor": SimpleNamespace(emit=Mock(), transcribe=Mock(),
                                                                           finish_transcripts=Mock()),
            "_transcript_add": lambda speaker, text: speaker == "gemini" and said.append(text.strip()),
            "_TurnTranscript": lambda *a: SimpleNamespace(add=Mock(), flush=Mock()),
            "_initiation_ts_lock": threading.Lock(), "_recent_user_words_lock": threading.Lock(),
            "_recent_user_words": [], "MEMORY_WORD_WINDOW": 50, "_memory_embedder": None,
            "_gemini_speaking_lock": threading.Lock(), "_append_playback": played.append,
            "_playback_buffer": b"", "RECEIVE_SAMPLE_RATE": 48000, "OUTPUT_CHANNELS": 1,
            "tracker_receive": SimpleNamespace(record=Mock()), "tracker_first_audio": SimpleNamespace(record=Mock()),
            "tracker_roundtrip": SimpleNamespace(record=Mock()), "_last_mic_send_ts": 0.0,
            "_last_robot_speech_ts": 0.0, "_last_user_speech_ts": 0.0, "_flush_playback": Mock(),
        }
        receive = load_function("receive_audio", namespace)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(receive(Session()))
        return played, said, log

    def test_addressed_reply_plays_and_side_talk_is_silent(self):
        pcm = np.array([1, 2], dtype=np.int16).tobytes()
        played, said, log = self.run_receive([
            content(user="Ember, can you hear me?"), content(ember="Yes, I can hear you.", audio=pcm),
            content(done=True), lambda clock: setattr(clock, "now", clock.now + 60),
            content(user="thanks for calling. I did finish my homework."),
            content(ember="That's good to hear.", audio=pcm), content(done=True),
        ])
        self.assertEqual(played, [np.repeat(np.frombuffer(pcm, dtype=np.int16), 2).tobytes()])
        self.assertEqual(said, ["Yes, I can hear you."])
        self.assertIn("That's good to hear.", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
