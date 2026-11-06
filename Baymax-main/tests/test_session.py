"""Context compression and session resumption config; no Gemini connection."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.conversational_ability import session


def update(handle, resumable=True):
    return SimpleNamespace(new_handle=handle, resumable=resumable)


class SessionTests(unittest.TestCase):
    def setUp(self):
        session.forget()
        self.addCleanup(session.forget)
        self.now = 1000.0
        env = patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "1", "BAYMAX_CONTEXT_COMPRESSION": "1"})
        env.start()
        self.addCleanup(env.stop)

    def build(self):
        return session.extend_config({"response_modalities": ["AUDIO"]}, clock=lambda: self.now)

    def test_first_connection_compresses_and_asks_for_resume_handles(self):
        config = self.build()
        self.assertEqual(config["context_window_compression"],
                         {"trigger_tokens": 32000, "sliding_window": {"target_tokens": 16000}})
        self.assertEqual(config["session_resumption"], {})  # Empty: start fresh, but send handles.

    def test_reconnect_resumes_with_the_newest_handle(self):
        session.remember(update("h1"), clock=lambda: self.now)
        session.remember(update("h2"), clock=lambda: self.now)
        session.remember(update("h3", resumable=False))       # Not resumable right now: keep h2.
        session.remember(SimpleNamespace(new_handle="", resumable=True))
        self.assertEqual(self.build()["session_resumption"], {"handle": "h2"})

    def test_failed_resume_falls_back_to_a_fresh_session(self):
        session.remember(update("h1"), clock=lambda: self.now)
        self.assertEqual(self.build()["session_resumption"], {"handle": "h1"})
        # That connection failed before any new handle arrived: next attempt starts fresh.
        self.assertEqual(self.build()["session_resumption"], {})
        session.remember(update("h2"), clock=lambda: self.now)  # A live session sends new ones.
        self.assertEqual(self.build()["session_resumption"], {"handle": "h2"})

    def test_expired_handles_are_not_used(self):
        session.remember(update("old"), clock=lambda: self.now)
        self.now += 2 * 3600
        self.assertEqual(self.build()["session_resumption"], {})

    def test_each_part_can_be_disabled_and_input_is_not_mutated(self):
        base = {"response_modalities": ["AUDIO"]}
        with patch.dict(os.environ, {"BAYMAX_SESSION_RESUMPTION": "0", "BAYMAX_CONTEXT_COMPRESSION": "0"}):
            self.assertEqual(session.extend_config(base), base)
        self.assertEqual(base, {"response_modalities": ["AUDIO"]})
        with patch.dict(os.environ, {"BAYMAX_CONTEXT_TRIGGER_TOKENS": "nonsense"}):
            self.assertEqual(self.build()["context_window_compression"]["trigger_tokens"], 32000)

    def test_server_messages_store_handles_and_note_go_away(self):
        with patch("runtime_monitor.record") as record:
            session.on_server_message(SimpleNamespace(session_resumption_update=update("h9"), go_away=None))
            session.on_server_message(SimpleNamespace(session_resumption_update=None,
                                                      go_away=SimpleNamespace(time_left="10s")))
        record.assert_called_once()
        session.extend_config({})  # Offers h9 once.
        self.assertEqual(session._state["handle"], "h9")

    def test_full_ember_config_is_accepted_by_the_sdk(self):
        from google.genai import types
        from features.toolkits import gemini_tools
        session.remember(update("h1"))  # Real clock, like the runtime.
        config = gemini_tools.extend_config({"system_instruction": "base", "response_modalities": ["AUDIO"]})
        validated = types.LiveConnectConfig.model_validate(config)
        self.assertEqual(validated.session_resumption.handle, "h1")
        self.assertEqual(validated.context_window_compression.sliding_window.target_tokens, 16000)


if __name__ == "__main__":
    unittest.main()
