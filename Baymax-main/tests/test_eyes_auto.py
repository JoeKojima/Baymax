"""Local eye expressions from conversation text; no devices or Gemini calls."""
import unittest
from unittest.mock import Mock, patch

import runtime_monitor as monitor
from features.eyes import auto, tools


class AutoEyeTests(unittest.TestCase):
    def test_explicit_requests(self):
        for text, name in [("Can you make a sad face?", "sad"), ("show me your happy eyes", "happy"),
                           ("play dead", "dead"), ("Look surprised!", "surprised"),
                           ("go to sleep, Ember", "sleep"), ("do the love eyes", "love")]:
            self.assertEqual(auto.requested(text), name, text)
        for text in ("I had a sad day", "do you love cats?", "what's the weather?"):
            self.assertIsNone(auto.requested(text), text)

    def test_change_eye_commands_and_transcribed_too(self):
        for text, name in [("Change your eyes too happy.", "happy"),
                           ("change your eyes to sad", "sad"),
                           ("set your expression to curious", "curious"),
                           ("switch your face to surprised", "surprised")]:
            with self.subTest(text=text):
                self.assertEqual(auto.requested(text), name)
                express = Mock(return_value=True)
                eyes = auto.AutoEyes(express)
                eyes.observe('user', {'id': 1, 'text': text})
                eyes.observe('ember', {'id': 2, 'text': 'Here you go.'})
                express.assert_called_once_with(name)
        for text in ("change my essay to happy", "my eyes are happy",
                     "the change to the story made me happy"):
            self.assertIsNone(auto.requested(text), text)

    def test_cues_only_from_the_opening_of_a_reply(self):
        self.assertEqual(auto.cue("I'm so sorry to hear that."), "sad")
        self.assertEqual(auto.cue("Congratulations, that's wonderful!"), "happy")
        self.assertEqual(auto.cue("Good night, sleep well."), "sleep")
        self.assertIsNone(auto.cue("The weather is mild today."))
        self.assertIsNone(auto.cue("x" * 200 + " congratulations"))

    def test_medical_alerts_are_narrow_and_force_exclamation_eyes(self):
        for text in ("I'm in pain", "My chest pain is getting worse", "She can't breathe",
                     "This is a medical emergency", "Someone is bleeding", "My arm hurts"):
            self.assertTrue(auto.medical_alert(text), text)
        for text in ("That book was a pain to finish", "pain au chocolat", "I fell in love",
                     "I'm sad today", "Show me sad eyes"):
            self.assertFalse(auto.medical_alert(text), text)

        express = Mock(return_value=True)
        eyes = auto.AutoEyes(express)
        eyes.observe("user", {"id": 1, "text": "I'm in pain; make a sad face"})
        eyes.observe("ember", {"id": 2, "text": "Oh no, I'm so sorry."})
        express.assert_called_once_with("surprised")
        eyes.observe("user", {"id": 3, "text": "Thanks, I'm okay now."})
        eyes.observe("ember", {"id": 4, "text": "I'm glad to hear that."})
        self.assertEqual(express.call_args.args, ("happy",))

    def test_one_expression_per_reply_request_wins(self):
        express = Mock(return_value=True)
        eyes = auto.AutoEyes(express)
        eyes.observe("user", {"id": 1, "text": "make a sad face"})
        eyes.observe("ember", {"id": 2, "text": "Congratulations"})
        eyes.observe("ember", {"id": 2, "text": "Congratulations, I'm so sorry"})
        express.assert_called_once_with("sad")
        eyes.observe("user", {"id": 3, "text": "thanks"})
        eyes.observe("ember", {"id": 4, "text": "Good night!"})
        self.assertEqual(express.call_args.args, ("sleep",))
        eyes.observe("ember", {"id": 5, "text": "Sure."})
        self.assertEqual(express.call_count, 2)  # Neutral reply: no expression.

    def test_wired_through_monitor_transcripts_and_tool_is_off_by_default(self):
        express = Mock(return_value=True)
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("BAYMAX_EYE_TOOL", None)
            detach = auto.attach(express)
            try:
                with patch.object(monitor, "transcripts", monitor.transcripts.__class__(maxlen=60)), \
                        patch.object(monitor, "active_transcripts", {}), \
                        patch.object(monitor, "eye_expression_handler", express):
                    monitor.transcribe("user", "make a happy face")
                    monitor.transcribe("ember", "Here you go.")
                    config = tools.extend_config({"system_instruction": "base"})
            finally:
                detach()
        express.assert_called_once_with("happy")
        self.assertNotIn("tools", config)  # No Gemini eye tool: no mid-reply tool calls.
        self.assertIn("natural words", config["system_instruction"])
        self.assertIn("whole conversation", config["system_instruction"])
        self.assertIsNone(monitor.transcript_observer)

    def test_natural_conversation_cues_and_curly_apostrophes(self):
        for text, name in [("What are you building?", 'curious'),
                           ("Your sister got married? That's usually a happy time. Are you missing her?", 'curious'),
                           ("Oh no, what's making you feel that way?", 'sad'),
                           ("That sounds interesting.", 'curious'),
                           ("That sounds lovely.", 'happy'),
                           ("Good evening!", 'happy'),
                           ("You did it!", 'excited'),
                           ("I\u2019m so sorry to hear that.", 'sad')]:
            self.assertEqual(auto.cue(text), name, text)
        self.assertIsNone(auto.cue("The train arrives at six."))

    def test_large_sentence_uses_its_opening_and_repeated_cues_do_not_retrigger(self):
        express = Mock(return_value=True)
        eyes = auto.AutoEyes(express)
        eyes.observe('ember', {'id': 1, 'text': 'Congratulations! ' + 'x' * 300})
        eyes.observe('ember', {'id': 1, 'text': 'Congratulations! ' + 'x' * 400})
        express.assert_called_once_with('happy')
        eyes.observe('ember', {'id': 2, 'text': 'x' * 200 + ' Congratulations!'})
        express.assert_called_once_with('happy')

    def test_positive_and_success_cues_override_question_fallback(self):
        for text, expected in [
                ("That's a great song. What do you like about it?", 'happy'),
                ("We finally found it! What gave it away?", 'happy'),
                ("That's really exciting! When do you start?", 'excited'),
                ("I'm thrilled for you. How are you celebrating?", 'excited'),
                ("I'm not sure. Could you repeat that?", 'confused')]:
            with self.subTest(text=text):
                self.assertEqual(auto.cue(text), expected)

    def test_streamed_curiosity_can_refine_without_repeating_or_overriding_requests(self):
        express = Mock(return_value=True)
        eyes = auto.AutoEyes(express)
        eyes.observe('ember', {'id': 1, 'text': 'Did we get it?'})
        eyes.observe('ember', {'id': 1, 'text': 'Did we get it? We'})
        express.assert_called_once_with('curious')
        eyes.observe('ember', {'id': 1, 'text': 'Did we get it? We found it!'})
        self.assertEqual([call.args[0] for call in express.call_args_list], ['curious', 'happy'])
        eyes.observe('user', {'id': 2, 'text': 'show curious eyes'})
        eyes.observe('ember', {'id': 3, 'text': "That's really exciting!"})
        self.assertEqual(express.call_args.args, ('curious',))
        self.assertTrue(eyes.done)

    def test_happy_then_question_then_enthusiasm_changes_with_streamed_sentences(self):
        express = Mock(return_value=True)
        eyes = auto.AutoEyes(express)
        text = "That's a great song."
        eyes.observe('ember', {'id': 1, 'text': text})
        text += ' What do you like about it?'
        eyes.observe('ember', {'id': 1, 'text': text})
        eyes.observe('ember', {'id': 1, 'text': text})
        text += " I'm really excited to hear it."
        eyes.observe('ember', {'id': 1, 'text': text})
        self.assertEqual([call.args[0] for call in express.call_args_list],
                         ['happy', 'curious', 'excited'])

    def test_medical_override_remains_fixed_through_later_questions(self):
        express = Mock(return_value=True)
        eyes = auto.AutoEyes(express)
        eyes.observe('user', {'id': 1, 'text': 'My arm hurts'})
        eyes.observe('ember', {'id': 2, 'text': "I'm sorry."})
        eyes.observe('ember', {'id': 2, 'text': "I'm sorry. Is someone with you?"})
        express.assert_called_once_with('surprised')


if __name__ == "__main__":
    unittest.main()
