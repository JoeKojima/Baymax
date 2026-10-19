"""Offline inspector checks: attribution, isolation, bounded snapshots and local access."""
import ast
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.memory_palace import debug, inspector
import person_memory

ROOT = Path(__file__).resolve().parents[1]


class InspectorTests(unittest.TestCase):
    def collection(self):
        return SimpleNamespace(get=Mock(return_value={
            'ids': ['a', 'b', 'c'],
            'documents': ['Alex and Sam went hiking.', 'Likes jazz (owner not recorded).', 'Sam has an interview.'],
            'metadatas': [{'people': '["Alex", "Sam"]', 'time': 100, 'kind': 'fact', 'source': 'conversation_summary'},
                          {'timestamp': 'old'}, {'people': '["Sam"]', 'time': 200}]}), count=lambda: 800)

    def test_shared_entries_are_not_duplicated_and_biometrics_are_excluded(self):
        collection = self.collection()
        snap = inspector.capture(collection, people=[{'id': 'p_a', 'name': 'Alex', 'embeddings': ['private'], 'crop': 'private'}],
                                 profile={'user_name': 'Alex', 'private_field': 'private'},
                                 in_prompt={'Alex and Sam went hiking.'}, given={'Sam has an interview.': 22})
        self.assertEqual(len(snap['memories']), 3)
        hiking = next(m for m in snap['memories'] if m['id'] == 'a')
        self.assertEqual(hiking['people'], ['Alex', 'Sam'])
        self.assertTrue(hiking['in_prompt'])
        self.assertTrue(next(m for m in snap['memories'] if m['id'] == 'c')['recalled'])
        self.assertEqual(next(m for m in snap['memories'] if m['id'] == 'b')['people'], [])
        self.assertNotIn('private', json.dumps(snap))
        self.assertEqual(snap['stored_total'], 800)
        collection.get.assert_called_once_with(limit=500, include=['documents', 'metadatas'])

    def test_pending_forget_hides_links_and_text_mentions(self):
        snap = inspector.capture(self.collection(), people=[{'id': 'p_s', 'name': 'Sam'}], forgotten=['Sam'])
        self.assertEqual([m['id'] for m in snap['memories']], ['b'])
        self.assertNotIn('Sam', json.dumps(snap))

    def test_no_database_is_unavailable_not_fake_memories(self):
        snap = inspector.capture(None)
        self.assertFalse(snap['available'])
        self.assertEqual(snap['memories'], [])

    def test_atomic_snapshot_survives_reader_reopen_and_marks_stale(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(inspector, 'PATH', Path(folder) / '.runtime-memory.json'):
            inspector.publish(inspector.capture(self.collection()))
            with patch.object(inspector.monitor, 'read_status', return_value={'pid': os.getpid(), 'state': 'listening'}):
                first, reopened = inspector.read_snapshot(), inspector.read_snapshot()
                self.assertTrue(first['live'])
                self.assertEqual(first['memories'], reopened['memories'])
                with patch.object(inspector.time, 'time', return_value=first['updated_at'] + 20):
                    self.assertFalse(inspector.read_snapshot()['live'])
            with patch.object(inspector.monitor, 'read_status', return_value={'pid': 999999, 'state': 'listening'}):
                self.assertFalse(inspector.read_snapshot()['live'])
            self.assertFalse(inspector.PATH.with_suffix('.tmp').exists())

    def test_corrupt_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(inspector, 'PATH', Path(folder) / 'memory.json'):
            inspector.PATH.write_text('not json')
            self.assertFalse(inspector.read_snapshot()['available'])

    def test_inspection_is_opt_in_and_throttled(self):
        sample = Mock(return_value={'available': True})
        with patch.object(debug, '_last_inspection', float('-inf')), patch.object(inspector, 'publish') as publish:
            with patch.dict(os.environ, {'BAYMAX_DEBUG_MONITOR': '0'}):
                debug.inspect_memory(sample)
                sample.assert_not_called()
            with patch.dict(os.environ, {'BAYMAX_DEBUG_MONITOR': '1'}):
                debug.inspect_memory(sample)
                debug.inspect_memory(sample)
            sample.assert_called_once()
            publish.assert_called_once()

    def test_runtime_snapshot_reports_real_context_without_loading_models(self):
        import threading
        from test_public_core_integration import load_function
        collect = self.collection()
        snapshot = load_function('_debug_memory_snapshot', {
            '_memory_save_lock': threading.RLock(), '_memory_embedder': SimpleNamespace(_collection=collect),
            '_face_id': None, '_profile': {'user_name': 'Alex'}, '_present_names': lambda: ['Sam'],
            '_memories_in_prompt': {'Alex and Sam went hiking.'}, '_memories_given': {'Sam has an interview.': 12},
            '_conversation': SimpleNamespace(pending=lambda: ['unfinished']), '_being_forgotten': lambda: set()})
        result = snapshot()
        self.assertEqual(result['pending_turns'], 1)
        self.assertEqual(result['visible'], ['Sam'])
        self.assertTrue(next(m for m in result['memories'] if m['id'] == 'c')['recalled'])

    def test_each_data_directory_controls_all_memory_paths(self):
        # Execute actual path declarations without importing Linux face/device dependencies.
        paths = [('ember_self.py', 'PROFILE_PATH'), ('face_id.py', 'PEOPLE_PATH'), ('person_memory.py', 'PENDING_PATH')]
        for file, variable in paths:
            tree = ast.parse((ROOT / file).read_text(encoding='utf-8'))
            assignments = [n for n in tree.body if isinstance(n, ast.Assign) and
                           any(isinstance(t, ast.Name) and t.id in ('SCRIPT_DIR', variable) for t in n.targets)]
            with self.subTest(file=file), tempfile.TemporaryDirectory() as folder:
                for name in ('ember-a', 'ember-b'):
                    expected = os.path.join(folder, name)
                    ns = {'os': os, '__file__': str(ROOT / file)}
                    with patch.dict(os.environ, {'BAYMAX_DATA_DIR': expected, 'GOOGLE_API_KEY': 'synthetic-key'}):
                        exec(compile(ast.Module(body=assignments, type_ignores=[]), file, 'exec'), ns)
                    self.assertEqual(os.path.dirname(ns[variable]), expected)
                    self.assertNotIn('synthetic-key', ns[variable])

    def test_pending_conversation_survives_restart_and_is_separate_per_space(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b = str(Path(folder) / 'a.jsonl'), str(Path(folder) / 'b.jsonl')
            person_memory.ConversationLog(a).add('user', 'I like space news.', ['Alex'])
            self.assertEqual(person_memory.ConversationLog(a).pending()[0]['text'], 'I like space news.')
            self.assertEqual(person_memory.ConversationLog(b).pending(), [])
