import ast
import json
from pathlib import Path
import tempfile
import unittest

from features.memory_palace.behavior import BehaviorRecorder, clean_payload
from features.memory_palace.graph import PalaceGraph
from features.memory_palace.interactions import InteractionTracker
from types import SimpleNamespace
from unittest.mock import Mock


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = [1000.]
        self.graph = PalaceGraph(Path(self.folder.name)/'memory.sqlite3',clock=lambda:self.now[0])
        self.log = BehaviorRecorder(clock=lambda:self.now[0])
        self.graph.behavior = self.log
        self.a = {'person_id':'a','generation':1}
        self.b = {'person_id':'b','generation':2}

    def view(self):
        return self.graph.snapshot()['behavior']

    def test_producer_only_queues_and_rejects_unknown_or_unowned_events(self):
        self.assertIsNone(self.log.emit('happy',self.a))
        self.assertIsNone(self.log.emit('user_turn',{}))
        self.log.emit('user_turn',self.a,{'word_count':3})
        self.assertEqual(self.view()['events'],[])
        self.log.drain(self.graph)
        event=self.view()['events'][0]
        self.assertEqual(event['owner_id'],'a')
        self.assertEqual(event['metrics'],{'word_count':3})

    def test_full_queue_drops_instead_of_waiting(self):
        log=BehaviorRecorder(capacity=1)
        self.assertTrue(log.emit('user_turn',self.a))
        self.assertIsNone(log.emit('user_turn',self.a))
        self.assertEqual(log.health()['dropped'],1)

    def test_offer_denominator_and_unknown_outcomes(self):
        one=self.log.emit('offer_presented',self.a,topic_id='piano')
        self.log.emit('offer_presented',self.a,topic_id='piano')
        self.log.emit('action_started',self.a,ref_id=one)
        self.log.emit('action_started',self.a,ref_id=one) # repeated starts count one offer
        self.log.emit('action_stopped',self.a,ref_id=one,payload={'played_ms':4000})
        self.log.drain(self.graph)
        offer=self.view()['offers'][0]
        self.assertEqual(offer['observed_offers'],2)
        self.assertEqual(offer['offers_followed_by_start'],1)
        self.assertNotIn('rejected',offer)

    def test_cross_profile_and_missing_references_are_unlinked(self):
        offer=self.log.emit('offer_presented',self.a)
        self.log.emit('action_started',self.b,ref_id=offer)
        self.log.emit('action_replayed',self.a,ref_id='missing')
        self.log.drain(self.graph)
        events={e['kind']:e for e in self.view()['events']}
        self.assertIsNone(events['action_started']['ref_id'])
        self.assertIsNone(events['action_replayed']['ref_id'])
        self.assertEqual(self.view()['offers'][0]['offers_followed_by_start'],0)

    def test_forget_rejects_queued_history(self):
        self.log.emit('user_turn',self.a)
        self.now[0]+=1
        self.graph.forget()
        self.log.drain(self.graph)
        self.assertEqual(self.view()['events'],[])
        self.now[0]+=1
        self.log.emit('user_turn',self.a)
        self.log.drain(self.graph)
        self.assertEqual(len(self.view()['events']),1)
        self.graph.forget()
        self.assertEqual(self.view()['events'],[])

    def test_persistence_dedup_and_retention(self):
        self.log.emit('user_turn',self.a,event_id='one')
        self.log.emit('user_turn',self.a,event_id='one')
        self.log.drain(self.graph)
        reopened=PalaceGraph(self.graph.path)
        self.assertEqual(len(reopened.snapshot()['behavior']['events']),1)
        self.now[0]+=31*86400
        self.log.emit('user_turn',self.a)
        self.log.drain(self.graph)
        self.assertEqual(len(self.view()['events']),1)

    def test_raw_history_cap_and_bounded_snapshot(self):
        with self.graph.db() as db:
            db.executemany('INSERT INTO behavior_events VALUES (?,?,?,?,?,?,?,?)',
                [(str(i),'a','session',100+i/100,'user_turn',None,None,'{}') for i in range(20010)])
        self.log.emit('user_turn',self.a)
        self.log.drain(self.graph)
        with self.graph.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM behavior_events').fetchone()[0],20000)
        self.assertEqual(len(self.view()['events']),100)

    def test_payload_has_no_text_keys_audio_scores_or_invalid_numbers(self):
        value=clean_payload({'text':'secret','audio':b'no','happy':1,'word_count':True,
                            'played_ms':float('nan'),'elapsed_ms':-1,'tool':'weather','speech_speed':5})
        self.assertEqual(value,{'tool':'weather'})

    def test_storage_failure_cannot_block_or_raise_on_producer(self):
        class Broken:
            @property
            def lock(self):raise RuntimeError('storage unavailable')
        self.log.emit('user_turn',self.a)
        self.log.drain(Broken())
        self.assertEqual(self.log.health()['failed_batches'],1)
        self.assertEqual(self.log.health()['dropped'],1)

    def test_repeat_mentions_count_distinct_sources_and_days(self):
        self.graph.ingest([{'t':100,'speaker':'user','speaker_id':'a','text':'I like piano.'}])
        self.graph.step()
        self.graph.ingest([{'t':86500,'speaker':'user','speaker_id':'a','text':'I like piano.'}])
        self.graph.step()
        mention=self.view()['mentions'][0]
        self.assertEqual(mention['source_mentions'],2)
        self.assertEqual(mention['observed_days'],2)

    def test_runtime_helper_fails_open(self):
        source=Path(__file__).parents[1]/'realtime_gemini_10.py'
        node=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                  if isinstance(n,ast.FunctionDef) and n.name=='_behavior_event')
        ns={'_palace_graph':None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
        self.assertIsNone(ns['_behavior_event']('user_turn'))
        ns['_palace_graph']=self.graph
        self.assertTrue(ns['_behavior_event']('user_turn',self.a))

    def test_real_transcript_hook_links_turns_without_playback_claims(self):
        source=Path(__file__).parents[1]/'realtime_gemini_10.py'
        node=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                  if isinstance(n,ast.ClassDef) and n.name=='_TurnTranscript')
        ns={'_current_memory_context':lambda:self.a,'_interaction_tracker':InteractionTracker(),
            '_behavior_event':self.log.emit,'_notify_webapp':Mock(),'_present_names':lambda:[],
            '_conversation':SimpleNamespace(add=Mock()),
            'memory_debug':SimpleNamespace(finish=Mock(),transcribe=Mock())}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
        robot=ns['_TurnTranscript']('gemini');robot.add('Want piano?');robot.flush()
        user=ns['_TurnTranscript']('user');user.add('Yes please.');user.flush()
        self.log.drain(self.graph)
        events={e['kind']:e for e in self.view()['events']}
        self.assertEqual(events['user_turn']['ref_id'],events['assistant_transcript']['id'])
        self.assertEqual(self.view()['offers'],[]) # transcript not an observed offer
        self.assertNotIn('speech_duration_ms',events['user_turn']['metrics'])
        self.assertEqual(ns['_conversation'].add.call_args.kwargs['interaction']['turn_id'],events['user_turn']['id'])


if __name__=='__main__':unittest.main()
