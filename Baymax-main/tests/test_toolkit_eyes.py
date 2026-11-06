"""Explicit eye commands through real tool dispatch; no devices or Gemini calls."""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import runtime_monitor as monitor
from features import persona
from features.eyes import tools
from features.toolkits import gemini_tools


class EyeToolTests(unittest.TestCase):
    """The opt-in Gemini eye tool (BAYMAX_EYE_TOOL=1); the default is features/eyes/auto.py."""
    def setUp(self):
        patcher = patch.dict('os.environ', {'BAYMAX_EYE_TOOL': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_active_eyes_advertise_tool_and_base_config_is_preserved(self):
        base = {'system_instruction': 'base', 'speech_config': {'voice': 'unchanged'}}
        with patch.object(monitor, 'eye_expression_handler', None):
            self.assertIs(tools.extend_config(base), base)
        with patch.object(monitor, 'eye_expression_handler', Mock()), patch.dict(
                'os.environ', {'BAYMAX_TOOLKIT_ENABLED': '0'}):
            result = gemini_tools.extend_config(base)
            self.assertEqual(tools.extend_config(result), result)
        self.assertEqual(result['speech_config']['voice'], 'unchanged')  # Only the persona's voice is set.
        self.assertEqual(base['speech_config'], {'voice': 'unchanged'})
        self.assertEqual(base['system_instruction'], 'base')
        self.assertIn('before speaking', result['system_instruction'])
        self.assertEqual(result['tools'][0]['function_declarations'][0]['name'], tools.NAME)

    def test_eye_tool_is_non_blocking_by_default_and_override_restores_blocking(self):
        from google.genai import types
        base = {'system_instruction': 'base'}
        with patch.object(monitor, 'eye_expression_handler', Mock()):
            with patch.dict('os.environ', {}, clear=False):
                os.environ.pop('BAYMAX_EYE_TOOL_SILENT', None)
                silent = tools.extend_config(base)
            with patch.dict('os.environ', {'BAYMAX_EYE_TOOL_SILENT': '0'}):
                blocking = tools.extend_config(base)
        declaration = types.Tool.model_validate(silent['tools'][0]).function_declarations[0]
        self.assertEqual(declaration.behavior, types.Behavior.NON_BLOCKING)
        self.assertIn('never repeat', silent['system_instruction'])
        self.assertNotIn('behavior', blocking['tools'][0]['function_declarations'][0])
        self.assertNotIn('never repeat', blocking['system_instruction'])
        self.assertNotIn('behavior', tools.DECLARATION)  # Shared declaration stays untouched.

    def dispatch(self, calls, env):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        states = []
        with patch.object(monitor, 'eye_expression_handler', Mock(return_value=True)), \
                patch.object(monitor, 'emit', side_effect=lambda state, message: states.append(state)), \
                patch.dict('os.environ', {'BAYMAX_TOOLKIT_ENABLED': '0', **env}):
            asyncio.run(gemini_tools.handle_tool_call(session, SimpleNamespace(function_calls=calls)))
        return session.send_tool_response.call_args.kwargs['function_responses'], states

    def test_silent_eye_reply_needs_no_follow_up_and_does_not_enter_thinking(self):
        from google.genai import types
        eye = SimpleNamespace(name=tools.NAME, id='eyes-1', args={'expression': 'happy'})
        replies, states = self.dispatch([eye], {'BAYMAX_EYE_TOOL_SILENT': '1'})
        self.assertEqual(replies[0].scheduling, types.FunctionResponseScheduling.SILENT)
        self.assertEqual(replies[0].response['expression'], 'happy')
        self.assertEqual(states, [])
        replies, states = self.dispatch([eye], {'BAYMAX_EYE_TOOL_SILENT': '0'})
        self.assertIsNone(replies[0].scheduling)
        self.assertEqual(states, ['calling_tool', 'thinking'])

    def test_other_tools_in_the_same_batch_still_get_a_spoken_reply(self):
        eye = SimpleNamespace(name=tools.NAME, id='eyes-1', args={'expression': 'happy'})
        weather = SimpleNamespace(name='get_weather', id='w-1', args={})
        replies, states = self.dispatch([eye, weather], {'BAYMAX_EYE_TOOL_SILENT': '1'})
        self.assertEqual([r.id for r in replies], ['eyes-1', 'w-1'])
        self.assertIsNotNone(replies[0].scheduling)
        self.assertIsNone(replies[1].scheduling)
        self.assertEqual(states, ['calling_tool', 'thinking'])

    def test_real_v8_prompt_is_replaced_by_mvp_persona(self):
        import ast
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "realtime_gemini_8.py").read_text(encoding="utf-8")
        prompt = next(ast.literal_eval(node.value) for node in ast.parse(source).body if isinstance(node, ast.Assign)
                      and any(getattr(t, 'id', None) == '_BASE_SYSTEM_INSTRUCTION' for t in node.targets))
        self.assertIn('Temple University', prompt)  # Upstream text itself stays byte-identical.
        for toolkit in ('0', '1'):
            with patch.object(monitor, 'eye_expression_handler', None), patch.dict(
                    'os.environ', {'BAYMAX_TOOLKIT_ENABLED': toolkit}):
                result = gemini_tools.extend_config({'system_instruction': prompt})
                again = gemini_tools.extend_config(result)
            text = ' '.join(result['system_instruction'].split())
            for gone in ('Temple', 'Tadashi', '[CURRENT CONTEXT]', '[MEMORY CONTEXT]'):
                self.assertNotIn(gone, text)
            for kept in ('Pallas Robotics', 'body of a cat', 'cannot move', 'Never guess or assume a name',
                         '[HOW TO TALK]', '[WELLNESS]', "'[FALL ALERT]'", 'never diagnose'):
                self.assertIn(kept, text)
            self.assertTrue(result['system_instruction'].startswith(persona.PERSONA))  # Toolkit sections follow.
            self.assertEqual(again['system_instruction'].count('[WHO YOU ARE TALKING TO]'), 1)
            self.assertEqual('[LOCATION AND WEATHER TOOLS]' in text, toolkit == '1')

    def test_voice_setting_is_validated_against_gemini_voices(self):
        from google.genai import types
        base = {'system_instruction': 'base', 'speech_config': {'voice_config': {'prebuilt_voice_config': {'voice_name': 'Fenrir'}}}}
        chosen = persona.voice(base, {'BAYMAX_VOICE': ' sulafat '})
        self.assertEqual(chosen['speech_config']['voice_config']['prebuilt_voice_config']['voice_name'], 'Sulafat')
        types.SpeechConfig.model_validate(chosen['speech_config'])
        self.assertEqual(base['speech_config']['voice_config']['prebuilt_voice_config']['voice_name'], 'Fenrir')
        name = lambda env: persona.voice(base, env)['speech_config']['voice_config']['prebuilt_voice_config']['voice_name']
        self.assertEqual(name({'BAYMAX_VOICE': 'Archernar'}), 'Achernar')  # Typo: closest voice.
        self.assertEqual(name({'BAYMAX_VOICE': 'Sami'}), 'Autonoe')  # Unknown: default, never an invalid name.
        default = persona.voice(base, {})  # Unset: Ember's default voice.
        self.assertEqual(default['speech_config']['voice_config']['prebuilt_voice_config']['voice_name'], 'Autonoe')
        self.assertEqual(len(persona.VOICES), 30)

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
