import ast
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock,Mock
from features.memory_palace.memory_context import MemoryContext,queue_context,consume_context
from features.memory_palace.graph import PalaceGraph
from features.memory_palace.retrieval import GraphIndex

class MemoryContextTests(unittest.TestCase):
    def test_accepts_upstream_id_without_identity_registry(self):
        context=MemoryContext()
        self.assertEqual(context.set_person('upstream:123','Example')['person_id'],'upstream:123')
        self.assertEqual(context.snapshot()['name'],'Example')
        self.assertFalse(hasattr(context,'observe'))
        self.assertFalse(hasattr(context,'self_identify'))
    def test_selection_has_no_identification_expiry(self):
        context=MemoryContext();state=context.set_person('local-test')
        self.assertEqual(context.snapshot(),state)
        self.assertNotIn('seconds_remaining',state)
        self.assertIsNone(context.clear()['person_id'])
    def test_invalid_ids_do_not_change_existing_scope(self):
        context=MemoryContext();context.set_person('valid')
        for bad in ('bad id',123,'../file'):
            with self.assertRaises(ValueError):context.set_person(bad)
        self.assertEqual(context.snapshot()['person_id'],'valid')
    def test_live_pid_bound_command_accepts_unregistered_test_profile(self):
        context=MemoryContext()
        with tempfile.TemporaryDirectory() as folder:
            snapshot={'live':True,'pid':os.getpid(),'people':[]}
            queue_context(folder,{'pid':os.getpid()},snapshot,'local-test','Local test')
            consume_context(folder,context)
            self.assertEqual(context.snapshot()['person_id'],'local-test')
            self.assertFalse((Path(folder)/'.runtime-memory-context.json').exists())
            with self.assertRaises(ValueError):queue_context(folder,{'pid':99},snapshot,'local-test')
    def test_corrupt_or_other_process_command_preserves_scope(self):
        context=MemoryContext();context.set_person('keep')
        with tempfile.TemporaryDirectory() as folder:
            for data in ('[]',json.dumps({'pid':-1,'issued_at':__import__('time').time(),'person_id':'other'})):
                (Path(folder)/'.runtime-memory-context.json').write_text(data)
                consume_context(folder,context)
                self.assertEqual(context.snapshot()['person_id'],'keep')
    def test_identical_claims_from_two_profiles_never_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            graph=PalaceGraph(Path(folder)/'graph.sqlite3')
            for i,owner in enumerate(('a','b',None)):
                graph.ingest([{'t':i+1,'speaker':'user','text':'I like piano.','speaker_id':owner}]);graph.step()
            claims=graph.snapshot()['claims']
            self.assertEqual(len(claims),3)
            self.assertEqual({c['owner_id'] for c in claims},{'a','b',None})
    def test_scope_change_drops_deferred_memory_context(self):
        root=Path(__file__).resolve().parents[1]
        node=next(n for n in ast.parse((root/'realtime_gemini_10.py').read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='_MemoryInjector')
        state={'person_id':'a','generation':1}
        ns={'time':__import__('time'),'asyncio':asyncio,'_current_memory_context':lambda:state,'_face_id':None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'runtime','exec'),ns)
        session=SimpleNamespace(send_client_content=AsyncMock())
        injector=ns['_MemoryInjector'](session);injector.owner_generation=1
        state.update(person_id='b',generation=2)
        asyncio.run(injector._send([('id','note from scope a')]))
        session.send_client_content.assert_not_called()
    def test_scope_change_never_resets_or_interrupts_live_connection(self):
        from test_public_core_integration import load_function
        state={'generation':0};context=SimpleNamespace(snapshot=lambda:dict(state))
        async def thread_call(f):return f()
        ns={'asyncio':SimpleNamespace(sleep=AsyncMock(side_effect=[None,asyncio.CancelledError()]),to_thread=thread_call),
            '_memory_context':context,'_memory_context_control':lambda:state.update(generation=1),
            '_current_memory_context':lambda:state,'_resume_handle':'keep-handle','_session_config':{'keep':True},
            '_recent_user_words_lock':threading.Lock(),'_recent_user_words':['old topic'],'_flush_playback':Mock()}
        run=load_function('_memory_context_monitor',ns)
        with self.assertRaises(asyncio.CancelledError):asyncio.run(run())
        self.assertEqual(ns['_resume_handle'],'keep-handle');self.assertEqual(ns['_session_config'],{'keep':True})
        ns['_flush_playback'].assert_not_called()
    def test_transcript_does_not_guess_identity_and_drops_midturn_scope_change(self):
        root=Path(__file__).resolve().parents[1]
        node=next(n for n in ast.parse((root/'realtime_gemini_10.py').read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='_TurnTranscript')
        state={'person_id':'a','generation':1};add=Mock()
        ns={'_current_memory_context':lambda:dict(state),'_notify_webapp':Mock(),
            '_conversation':SimpleNamespace(add=add),'_present_names':lambda:[],
            'memory_debug':SimpleNamespace(transcribe=Mock(),finish=Mock())}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'runtime','exec'),ns)
        ns['_behavior_event']=Mock()
        transcript=ns['_TurnTranscript']('user');transcript.add('My name is James.')
        state.update(person_id='b',generation=2);transcript.flush()
        self.assertIsNone(add.call_args.kwargs['speaker_id'])
        transcript.add('I like piano.');transcript.flush()
        self.assertEqual(add.call_args.kwargs['speaker_id'],'b')

class IndexTests(unittest.TestCase):
    def test_stale_or_other_owner_results_are_checked_in_sqlite(self):
        with tempfile.TemporaryDirectory() as folder:
            g=PalaceGraph(Path(folder)/'graph.sqlite3')
            g.ingest([{'t':1,'speaker':'user','text':'I like piano.','speaker_id':'joe-1'}]);g.step()
            cid=g.snapshot()['claims'][0]['id']
            class Vector:
                def __getitem__(self,index):return self
                def tolist(self):return [1.0,0.0]
            class Collection:
                def query(self,**kwargs):return {'ids':[[cid]],'distances':[[0.1]]}
            index=object.__new__(GraphIndex);index.lock=__import__('threading').RLock()
            index.collection=Collection();index.embedder=SimpleNamespace(embed=lambda text:Vector())
            self.assertEqual(len(index.search(g,'piano','joe-1')),1)
            self.assertEqual(index.search(g,'piano','joe-2'),[])
            g.forget();self.assertEqual(index.search(g,'piano','joe-1'),[])

if __name__=='__main__':unittest.main()
