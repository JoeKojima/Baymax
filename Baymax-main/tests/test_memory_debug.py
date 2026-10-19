import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from features.memory_palace import debug
import runtime_monitor
from test_public_core_integration import load_function


class MemoryDebugTests(unittest.TestCase):
    def test_disabled_bridge_does_not_write_or_start_monitor(self):
        with patch.dict(os.environ, {'BAYMAX_DEBUG_MONITOR': '0'}), patch.object(debug, 'monitor') as monitor:
            debug.start(lambda: {})
            debug.transcribe('gemini', 'hello')
            debug.state('speaking', 'audio')
            debug.finish()
            debug.activity('news')
            self.assertEqual(monitor.mock_calls, [])

    def test_role_mapping_and_v10_capabilities(self):
        with patch.dict(os.environ, {'BAYMAX_DEBUG_MONITOR': '1'}), patch.object(debug, 'monitor') as monitor:
            debug.transcribe('gemini', 'hello')
            debug.finish('gemini')
            monitor.transcribe.assert_called_once_with('ember', 'hello')
            monitor.finish_transcripts.assert_called_once_with('ember')
        result = runtime_monitor.snapshot({'runtime': 'v10', 'memory_palace': {'memory_active': True}})
        self.assertIn('Active', result['capabilities']['memory'])
        self.assertEqual(result['capabilities']['fall_detection']['state'], 'Disabled')

    def test_public_failure_does_not_drop_people_tool_response(self):
        toolkit = SimpleNamespace(DECLARATIONS=[{'name': 'public_capabilities'}],
                                  run_call=AsyncMock(side_effect=RuntimeError('provider failed')))
        face = SimpleNamespace(handle_tool_call=Mock(return_value={'status': 'consent_required'}))
        handler = load_function('_handle_tool_call', {'public_toolkit': toolkit, 'asyncio': asyncio,
            'memory_debug': SimpleNamespace(activity=Mock()), '_face_id': face,
            'types': SimpleNamespace(FunctionResponse=lambda **kwargs: SimpleNamespace(**kwargs))})
        session = SimpleNamespace(send_tool_response=AsyncMock())
        calls = [SimpleNamespace(name='public_capabilities', id='public-1', args={}),
                 SimpleNamespace(name='remember_person', id='person-2', args={'name': 'Alex'})]
        asyncio.run(handler(session, SimpleNamespace(function_calls=calls)))
        responses = session.send_tool_response.call_args.kwargs['function_responses']
        self.assertEqual([r.id for r in responses], ['public-1', 'person-2'])
        self.assertIn('error', responses[0].response)
        self.assertEqual(responses[1].response, {'status': 'consent_required'})
        face.handle_tool_call.assert_called_once_with('remember_person', {'name': 'Alex'})
