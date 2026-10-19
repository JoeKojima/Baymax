import ast
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from features.memory_palace.ownership import SpeakerSession,queue_confirmation,consume_confirmation
from features.memory_palace.graph import PalaceGraph
from features.memory_palace.retrieval import GraphIndex

PEOPLE=[{'id':'joe-1','name':'Alex'},{'id':'joe-2','name':'Sam'}]

class OwnershipTests(unittest.TestCase):
    def setUp(self):self.now=100;self.speaker=SpeakerSession(clock=lambda:self.now)
    def test_face_alone_never_confirms(self):
        self.assertIsNone(self.speaker.observe([{'person_id':'joe-1'}])['person_id'])
    def test_existing_id_confirmation_and_expiry(self):
        s=self.speaker.confirm('joe-1',PEOPLE);self.assertEqual(s['person_id'],'joe-1')
        self.now+=901;self.assertIsNone(self.speaker.snapshot()['person_id'])
    def test_group_wrong_face_and_removal_clear_confirmation(self):
        self.speaker.confirm('joe-1',PEOPLE,[{'person_id':'joe-1'}])
        self.assertIsNone(self.speaker.observe([{'person_id':'joe-2'}])['person_id'])
        with self.assertRaises(ValueError):self.speaker.confirm('joe-1',PEOPLE,[{'person_id':'joe-2'}])
        self.speaker.confirm('joe-1',PEOPLE)
        self.assertIsNone(self.speaker.observe([{'person_id':'joe-1'}],1)['person_id'])
    def test_departure_grace_does_not_silently_reassign(self):
        self.speaker.confirm('joe-1',PEOPLE,[{'person_id':'joe-1'}])
        self.speaker.observe([]);self.now+=11
        self.assertIsNone(self.speaker.observe([])['person_id'])
    def test_exact_explicit_identity_only(self):
        for text in ("Alex loves music", "I am Alex's brother", 'My name is Alex and I love music'):
            self.assertIsNone(self.speaker.self_identify(text,PEOPLE)['person_id'])
        self.assertEqual(self.speaker.self_identify('My name is Alex.',PEOPLE)['person_id'],'joe-1')
    def test_commands_are_live_pid_bound_and_consumed(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot={'live':True,'pid':os.getpid(),'people':PEOPLE}
            queue_confirmation(folder,{'pid':os.getpid()},snapshot,'joe-1')
            consume_confirmation(folder,self.speaker,PEOPLE,[],0)
            self.assertEqual(self.speaker.snapshot()['person_id'],'joe-1')
            self.assertFalse((Path(folder)/'.runtime-speaker-command.json').exists())
            with self.assertRaises(ValueError):queue_confirmation(folder,{'pid':99},snapshot,'joe-1')
            with self.assertRaises(ValueError):queue_confirmation(folder,{'pid':os.getpid()},snapshot,'invented')
    def test_malformed_command_does_not_crash_runtime_control(self):
        with tempfile.TemporaryDirectory() as folder:
            self.speaker.confirm('joe-1',PEOPLE)
            (Path(folder)/'.runtime-speaker-command.json').write_text('[]')
            consume_confirmation(folder,self.speaker,PEOPLE,[],0)
            self.assertIsNone(self.speaker.snapshot()['person_id'])
    def test_other_pid_command_cannot_change_speaker(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'.runtime-speaker-command.json').write_text(json.dumps({'pid':-1,'issued_at':__import__('time').time(),'person_id':'joe-1'}))
            consume_confirmation(folder,self.speaker,PEOPLE,[],0)
            self.assertIsNone(self.speaker.snapshot()['person_id'])
    def test_identical_claims_from_different_people_never_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            g=PalaceGraph(Path(folder)/'graph.sqlite3')
            for i,owner in enumerate(('joe-1','joe-2',None)):
                g.ingest([{'t':i+1,'speaker':'user','text':'I like piano.','speaker_id':owner}]);g.step()
            claims=g.snapshot()['claims']
            self.assertEqual(len(claims),3)
            self.assertEqual({c['owner_id'] for c in claims},{'joe-1','joe-2',None})
    def test_real_injector_drops_deferred_context_after_speaker_switch(self):
        root=Path(__file__).resolve().parents[1]
        tree=ast.parse((root/'realtime_gemini_10.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='_MemoryInjector')
        state={'person_id':'joe-1','generation':1}
        ns={'time':__import__('time'),'asyncio':asyncio,'_current_speaker':lambda:state,'_face_id':None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'runtime','exec'),ns)
        session=SimpleNamespace(send_client_content=AsyncMock())
        injector=ns['_MemoryInjector'](session);injector.owner_generation=1
        state.update(person_id='joe-2',generation=2)
        asyncio.run(injector._send([('id','private note for Alex')]))
        session.send_client_content.assert_not_called()

class BoundaryTests(unittest.TestCase):
    def test_identity_change_resets_live_context_and_playback(self):
        import threading
        from test_public_core_integration import load_function
        class SessionEnding(Exception):pass
        calls=[]
        ns={'asyncio':asyncio,'_speaker_control':lambda:None,'_session_speaker_generation':1,
            '_current_speaker':lambda:{'generation':2},'_resume_handle':'old-handle',
            '_session_config':{'private':'old person'},'_flush_playback':lambda:calls.append('flushed'),
            '_recent_user_words_lock':threading.Lock(),'_recent_user_words':['old topic'],
            '_SessionEnding':SessionEnding}
        run=load_function('_speaker_boundary',ns)
        with self.assertRaises(SessionEnding):asyncio.run(run())
        self.assertIsNone(ns['_resume_handle']);self.assertIsNone(ns['_session_config'])
        self.assertEqual(ns['_recent_user_words'],[]);self.assertEqual(calls,['flushed'])

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
