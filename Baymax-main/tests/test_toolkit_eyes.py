"""Explicit eye commands through real tool dispatch; no devices or Gemini calls."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import runtime_monitor as monitor
from features.eyes import tools
from features.toolkits import gemini_tools


class EyeToolTests(unittest.TestCase):
    def test_only_active_eyes_advertise_tool_and_base_config_is_preserved(self):
        base = {'system_instruction': 'base', 'speech_config': {'voice': 'unchanged'}}
        with patch.object(monitor, 'eye_expression_handler', None):
            self.assertIs(tools.extend_config(base), base)
        with patch.object(monitor, 'eye_expression_handler', Mock()), patch.dict(
                'os.environ', {'BAYMAX_TOOLKIT_ENABLED': '0'}):
            result = gemini_tools.extend_config(base)
            self.assertEqual(tools.extend_config(result), result)
        self.assertEqual(result['speech_config'], base['speech_config'])
        self.assertEqual(base['system_instruction'], 'base')
        self.assertIn('before speaking', result['system_instruction'])
        self.assertEqual(result['tools'][0]['function_declarations'][0]['name'], tools.NAME)

    def test_validation_disabled_and_failed_hardware(self):
        handler = Mock(return_value=True)
        with patch.object(monitor, 'eye_expression_handler', handler):
            for args in (None, [], {}, {'expression': 'unknown'}, {'expression': []},
                         {'expression': 'sad', 'extra': True}):
                self.assertIn('error', tools.execute(args))
            handler.assert_not_called()
            self.assertEqual(tools.execute({'expression': 'sad'})['status'], 'requested')
            handler.assert_called_once_with('sad')
            handler.return_value = False
            self.assertIn('error', tools.execute({'expression': 'sad'}))
            handler.side_effect = RuntimeError('disconnected')
            self.assertIn('error', tools.execute({'expression': 'sad'}))
        with patch.object(monitor, 'eye_expression_handler', None):
            self.assertIn('error', tools.execute({'expression': 'sad'}))

    def test_real_dispatch_keeps_call_id_even_with_public_toolkit_disabled(self):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        calls = SimpleNamespace(function_calls=[SimpleNamespace(
            name=tools.NAME, id='eyes-42', args={'expression': 'sleep'})])
        with patch.object(monitor, 'eye_expression_handler', Mock(return_value=True)) as handler, patch.dict(
                'os.environ', {'BAYMAX_TOOLKIT_ENABLED': '0'}):
            asyncio.run(gemini_tools.handle_tool_call(session, calls))
        handler.assert_called_once_with('sleep')
        reply = session.send_tool_response.call_args.kwargs['function_responses'][0]
        self.assertEqual(reply.id, 'eyes-42')
        self.assertEqual(reply.name, tools.NAME)
        self.assertEqual(reply.response['expression'], 'sleep')
