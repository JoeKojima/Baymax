"""Expressive intent lifecycle; fake clock, no devices, models or providers."""
import ast
import asyncio
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import runtime_monitor as monitor
from features.eyes.controller import Eyes
from features.eyes import tools


class ExpressionIntentTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.eyes = Eyes(clock=lambda: self.now)

    def test_model_can_choose_an_expression_without_a_keyword_or_request(self):
        with patch.object(monitor, 'eye_expression_handler', self.eyes.select_expression), \
                patch.dict(os.environ, {'BAYMAX_EYE_TOOL': '1'}):
            self.assertEqual(tools.execute({'expression': 'curious'})['status'], 'requested')
        self.assertEqual(self.eyes.mode(), 'curious')

    def test_only_one_selection_before_speech_and_not_during_buffered_audio(self):
        self.assertTrue(self.eyes.select_expression('happy'))
        self.assertFalse(self.eyes.select_expression('sad'))
        self.eyes.event('user_speech')  # Late transcription must not erase selection.
        self.assertEqual(self.eyes.mode(), 'happy')
        self.eyes.event('speaking')
        self.assertFalse(self.eyes.select_expression('sad'))
        self.eyes.event('reply_complete')
        self.assertFalse(self.eyes.select_expression('sad'))
        self.eyes.event('reply_playback_done')
        self.assertEqual(self.eyes.mode(), 'normal')
        self.assertTrue(self.eyes.select_expression('sad'))

    def test_audio_without_an_expression_cannot_be_changed_mid_reply(self):
        self.eyes.event('speaking')
        self.assertFalse(self.eyes.select_expression('excited'))
        self.assertEqual(self.eyes.mode(), 'normal')

    def test_cancel_and_reconnect_allow_the_next_selection(self):
        for event in ('listening', 'reconnecting', 'stopping'):
            with self.subTest(event=event):
                self.eyes.event('listening')
                self.assertTrue(self.eyes.select_expression('happy'))
                self.eyes.event(event)
                self.assertTrue(self.eyes.select_expression('curious'))

    def test_fall_alert_and_thinking_still_override_intent(self):
        self.assertTrue(self.eyes.select_expression('happy'))
        self.eyes.event('thinking')
        self.now = 3
        self.assertEqual(self.eyes.mode(), 'thinking')
        self.eyes.event('fall_alert')
        self.assertEqual(self.eyes.mode(), 'surprised')

    def test_default_config_uses_supplied_patterns_and_no_background_response(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(monitor, 'eye_expression_handler', Mock()):
            config = tools.extend_config({'system_instruction': 'base'})
        declaration = config['tools'][0]['function_declarations'][0]
        self.assertEqual(declaration['behavior'], 'BLOCKING')
        self.assertNotIn('normal', declaration['parameters']['properties']['expression']['enum'])
        self.assertIn('whole conversation', config['system_instruction'])
        self.assertIn('spoken reply exactly once', config['system_instruction'])

    def test_disabled_mode_advertises_no_tool_and_changes_no_hardware(self):
        handler = Mock()
        base = {'system_instruction': 'base'}
        with patch.dict(os.environ, {'BAYMAX_EYE_TOOL': '0'}), \
                patch.object(monitor, 'eye_expression_handler', handler):
            self.assertIs(tools.extend_config(base), base)
            self.assertIn('error', tools.execute({'expression': 'happy'}))
        handler.assert_not_called()

    def test_abandoned_intent_expires_and_invalid_expression_does_not_consume_turn(self):
        self.assertFalse(self.eyes.select_expression('invented'))
        self.assertTrue(self.eyes.select_expression('sad'))
        self.now = 31
        self.assertTrue(self.eyes.select_expression('happy'))

    def test_real_dispatch_returns_one_result_batch_and_rejects_second_selection(self):
        # Exercise the real dispatcher without importing audio/DSP dependencies.
        source = Path(__file__).resolve().parents[1] / 'features/toolkits/gemini_tools.py'
        node = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_tool_call')
        empty = SimpleNamespace(DECLARATIONS=[])
        namespace = dict(asyncio=asyncio, monitor=monitor, eye_tools=tools, DECLARATIONS=[],
                         experimental=empty, everyday=empty, family=empty,
                         audiobooks=SimpleNamespace(DECLARATION={'name': 'audiobook'}),
                         execute=lambda name, args: tools.execute(args))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        genai = ModuleType('google.genai')
        genai.types = SimpleNamespace(FunctionResponse=lambda **kwargs: SimpleNamespace(**kwargs))
        calls = [SimpleNamespace(name=tools.NAME, id='eye-1', args={'expression': 'happy'}),
                 SimpleNamespace(name=tools.NAME, id='eye-2', args={'expression': 'sad'})]
        session = SimpleNamespace(send_tool_response=AsyncMock())
        with patch.dict(sys.modules, {'google': ModuleType('google'), 'google.genai': genai}), \
                patch.dict(os.environ, {'BAYMAX_EYE_TOOL': '1'}), \
                patch.object(monitor, 'eye_expression_handler', self.eyes.select_expression), \
                patch.object(monitor, 'emit') as emit:
            asyncio.run(namespace['handle_tool_call'](session, SimpleNamespace(function_calls=calls)))
        session.send_tool_response.assert_awaited_once()
        results = session.send_tool_response.call_args.kwargs['function_responses']
        self.assertEqual([r.id for r in results], ['eye-1', 'eye-2'])
        self.assertEqual(results[0].response['status'], 'requested')
        self.assertIn('error', results[1].response)
        self.assertFalse(hasattr(results[0], 'scheduling'))
        emit.assert_not_called()
        self.assertEqual(self.eyes.mode(), 'happy')


if __name__ == '__main__':
    unittest.main()
