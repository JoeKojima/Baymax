import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from features.toolkits.everyday.store import Store
from features.toolkits.everyday import tools


class EverydayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_800_000_000
        self.store = Store(Path(self.tmp.name) / 'tasks.sqlite3', clock=lambda: self.now)
        self.env = patch.dict('os.environ', {'BAYMAX_TOOLKIT_ENABLED': '1', 'EMBER_EVERYDAY_ENABLED': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_timer_restart_overdue_and_cancel(self):
        saved = self.store.reminder('create', text='Oven', seconds=20)
        restarted = Store(self.store.path, clock=lambda: self.now + 30)
        self.assertEqual(restarted.due()[0]['text'], 'Oven')
        self.assertTrue(restarted.reminder('cancel', id=saved['id'])['cancelled'])
        self.assertEqual(restarted.due(), [])
        self.assertFalse(restarted.reminder('cancel', id=saved['id'])['cancelled'])

    def test_absolute_time_and_rejected_ambiguous_or_past(self):
        from datetime import datetime, timezone
        due = datetime.fromtimestamp(self.now + 60, timezone.utc).isoformat()
        self.store.reminder('create', text='Appointment', due_at=due)
        self.assertEqual(len(self.store.reminder('list')['reminders']), 1)
        for kwargs in ({'due_at': '2027-01-01T12:00:00'}, {'seconds': True}, {'seconds': 0},
                       {'seconds': 1, 'due_at': due}, {'due_at': '2000-01-01T00:00:00Z'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.reminder('create', text='No save', **kwargs)
        self.assertEqual(len(self.store.reminder('list')['reminders']), 1)

    def test_lists_persist_deduplicate_and_remove_only_correct_list(self):
        result = self.store.items('add', name='Shopping', text='Milk')
        item = result['items'][0]['id']
        self.store.items('add', name='shopping', text='Milk')
        self.assertEqual(len(Store(self.store.path).items('show')['items']), 1)
        self.assertFalse(self.store.items('remove', name='other', id=item)['removed'])
        self.assertTrue(self.store.items('remove', id=item)['removed'])
        self.store.items('add', text='Eggs')
        self.assertEqual(self.store.items('clear')['items'], [])

    def test_validation_and_disabled_tools_do_not_create_storage(self):
        with patch.object(tools, 'store', self.store):
            for name,args in [('reminder', {'action': 'create', 'seconds': 10}),
                              ('household_list', {'action': 'remove', 'id': True}),
                              ('household_list', {'action': 'show', 'text': 'unexpected'}),
                              ('local_clock', {'surprise': 'argument'})]:
                self.assertIn('error', tools.execute(name, args))
            with patch.dict('os.environ', {'EMBER_EVERYDAY_ENABLED': '0'}):
                self.assertIn('error', tools.execute('reminder', {'action': 'create', 'text': 'X', 'seconds': 1}))
        self.assertFalse(self.store.path.exists())

    def test_full_store_rejects_without_losing_reminders(self):
        for i in range(100):
            self.store.reminder('create', text=str(i), seconds=1)
        with self.assertRaises(ValueError):
            self.store.reminder('create', text='Overflow', seconds=1)
        self.assertEqual(len(self.store.reminder('list')['reminders']), 100)

    def test_failed_storage_does_not_claim_saved(self):
        self.store.path.write_bytes(b'not sqlite')
        with patch.object(tools, 'store', self.store):
            result = tools.execute('household_list', {'action': 'add', 'text': 'Milk'})
        self.assertIn('error', result)
        self.assertNotIn('saved', result)
        self.assertEqual(self.store.path.read_bytes(), b'not sqlite')

    def test_delivery_failure_retries_then_success_does_not_repeat(self):
        self.store.reminder('create', text='Check oven', seconds=1)
        self.now += 2
        session = AsyncMock()
        with patch.object(tools, 'store', self.store), patch.object(tools, 'available_to_speak', return_value=True):
            session.send_client_content.side_effect = RuntimeError('offline')
            with self.assertRaises(RuntimeError):
                asyncio.run(tools.notify_due(session))
            self.assertEqual(len(self.store.due()), 1)
            session.send_client_content.side_effect = None
            asyncio.run(tools.notify_due(session))
            self.assertEqual(self.store.due(), [])
            asyncio.run(tools.notify_due(session))
            self.assertEqual(session.send_client_content.await_count, 2)
            self.assertIn('Check oven', str(session.send_client_content.call_args))

    def test_busy_or_disabled_defers_delivery(self):
        self.store.reminder('create', text='Wait', seconds=1)
        self.now += 2
        session = AsyncMock()
        with patch.object(tools, 'store', self.store), patch.object(tools, 'available_to_speak', return_value=False):
            asyncio.run(tools.notify_due(session))
        with patch.object(tools, 'store', self.store), patch.dict('os.environ', {'EMBER_EVERYDAY_ENABLED': '0'}):
            asyncio.run(tools.notify_due(session))
        session.send_client_content.assert_not_called()
        self.assertEqual(len(self.store.due()), 1)

    def test_session_shutdown_and_registration(self):
        from features.toolkits import fall_detection
        self.assertIn(tools.session_task, fall_detection.session_tasks)
        async def run():
            event = asyncio.Event()
            event.set()
            await tools.session_task(AsyncMock(), event)
        asyncio.run(run())

    def test_core_configuration_and_dispatch(self):
        from features.toolkits import gemini_tools
        config = {'system_instruction': 'Base', 'tools': []}
        configured = gemini_tools.extend_config(config)
        names = {d['name'] for t in configured['tools'] for d in t.get('function_declarations', [])}
        self.assertTrue(tools.NAMES <= names)
        with patch.object(tools, 'store', self.store):
            self.assertTrue(gemini_tools.execute('household_list', {'action': 'add', 'text': 'Milk'})['saved'])
        with patch.dict('os.environ', {'EMBER_EVERYDAY_ENABLED': '0'}):
            disabled = gemini_tools.extend_config(config)
            names = {d['name'] for t in disabled['tools'] for d in t.get('function_declarations', [])}
            self.assertFalse(tools.NAMES & names)


if __name__ == '__main__':
    unittest.main()
