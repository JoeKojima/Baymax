import json
from pathlib import Path
import tempfile
import unittest

import person_memory
from features.memory_palace.graph import PalaceGraph
from features.memory_palace.interactions import InteractionTracker, clean_metrics, encode_interaction, current_affect


class InteractionTests(unittest.TestCase):
    def test_self_report_has_topic_evidence_and_expiry(self):
        value=encode_interaction('I feel happy about piano. I am stressed about work.',100)
        self.assertEqual([e['label'] for e in value['emotions']],['happy','stressed'])
        self.assertEqual([e['target'] for e in value['emotions']],['piano','work'])
        self.assertEqual(value['emotions'][0]['expires_at'],1900)
        self.assertEqual(value['emotions'][0]['quote'],'I feel happy about piano')
        self.assertEqual(value['emotions'][0]['source'],'self_report')

    def test_behavior_and_reported_or_hypothetical_speech_do_not_infer_emotion(self):
        for text in ('I like piano.','I am dancing.','My friend said I am happy.','If I am happy, play music.',
                     '"I am happy" is an example.','Do I feel happy?'):
            self.assertFalse(encode_interaction(text,100)['emotions'],text)

    def test_not_happy_is_not_assumed_to_be_sad(self):
        emotion=encode_interaction("I'm not happy about work.",100)['emotions'][0]
        self.assertEqual(emotion['label'],'not happy')
        self.assertIsNone(emotion['valence'])

    def test_natural_reactions_are_specific_not_global_ratings(self):
        value=encode_interaction('Not now. Keep it short. Play that again.',100,{'reply_reference':'Want music?'})
        self.assertEqual({f['kind'] for f in value['feedback']},{'timing_boundary','brevity_request','repeat_request'})
        self.assertTrue(all(f['target']=='previous_ember_turn_candidate' for f in value['feedback']))
        for text in ('','yes','no','thanks','He said that was helpful.'):
            self.assertFalse(encode_interaction(text,100)['feedback'])
        self.assertEqual(encode_interaction('That was helpful.',100)['feedback'][0]['target'],'unresolved')

    def test_whitelist_rejects_fabricated_satisfaction_speed_and_invalid_metrics(self):
        metrics=clean_metrics({'word_count':8,'response_gap_proxy_ms':float('nan'),'transcript_span_ms':True,
                               'happiness':1,'speech_speed':999,'satisfaction':1})
        self.assertEqual(metrics,{'word_count':8})

    def test_tracker_measures_receipts_with_same_scope(self):
        now=[10.];tracker=InteractionTracker(lambda:now[0]);scope={'person_id':'a','name':'Alex','generation':1}
        tracker.fragment('ember',scope);now[0]=12;tracker.finish('ember','Want music?',scope)
        now[0]=14;tracker.fragment('user',scope);now[0]=15;tracker.fragment('user',scope)
        metrics=tracker.finish('user','Play that again please.',scope)
        self.assertEqual(metrics['response_gap_proxy_ms'],2000)
        self.assertEqual(metrics['transcript_span_ms'],1000)
        self.assertEqual(metrics['word_count'],4)
        self.assertEqual(metrics['timing_source'],'transcript_arrival_proxy')
        self.assertNotIn('speech_speed',metrics)

    def test_scope_changes_and_overlap_do_not_attribute_old_reply_gaps(self):
        now=[1.];tracker=InteractionTracker(lambda:now[0]);a={'person_id':'a','generation':1};b={'person_id':'b','generation':2}
        tracker.fragment('ember',a);tracker.finish('ember','Old reply',a)
        tracker.fragment('ember',a);tracker.fragment('user',a)
        metrics=tracker.finish('user','Wait',a)
        self.assertTrue(metrics['overlap_observed']);self.assertNotIn('response_gap_proxy_ms',metrics)
        tracker.fragment('user',a)
        self.assertEqual(tracker.finish('user','Hello',b),{})
        tracker.fragment('user',b)
        self.assertNotIn('reply_reference',tracker.finish('user','Hello',b))

    def test_single_fragment_has_no_fabricated_duration(self):
        tracker=InteractionTracker(lambda:10)
        tracker.fragment('user',{})
        value=tracker.finish('user','Hi',{})
        self.assertNotIn('transcript_span_ms',value)
        self.assertNotIn('response_gap_proxy_ms',value)

    def test_discarded_ember_turn_does_not_seed_a_reply_reference(self):
        tracker=InteractionTracker(lambda:10);scope={'person_id':'a','generation':1}
        tracker.fragment('ember',scope);tracker.finish('ember','',scope)
        tracker.fragment('user',scope)
        self.assertNotIn('reply_reference',tracker.finish('user','Hello',scope))

    def test_pending_turn_metadata_survives_restart_and_forget_removes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'pending.jsonl');log=person_memory.ConversationLog(path,clock=lambda:100)
            log.add('user','Keep it short.',speaker_id='a',interaction={'word_count':3,'reply_reference':'About Alex'})
            reopened=person_memory.ConversationLog(path)
            self.assertEqual(reopened.pending()[0]['interaction']['word_count'],3)
            reopened.forget('Alex')
            self.assertNotIn('interaction',reopened.pending()[0])
            self.assertNotIn('reply_reference',Path(path).read_text())

    def test_graph_preserves_episode_and_claim_provenance_and_clears_on_forget(self):
        with tempfile.TemporaryDirectory() as folder:
            g=PalaceGraph(Path(folder)/'memory.sqlite3')
            turn={'t':100,'speaker':'user','speaker_id':'a','text':'I like piano. I feel happy about piano.',
                  'interaction':{'word_count':9,'response_gap_proxy_ms':1200}}
            g.ingest([turn]);g.step();snapshot=g.snapshot()
            self.assertEqual(len(snapshot['interaction_events']),1)
            self.assertEqual(snapshot['claims'][0]['interaction']['emotions'][0]['target'],'piano')
            g.ingest([{**turn,'speaker_id':'b'}])
            self.assertEqual(g.snapshot()['interaction_events'][0]['owner_id'],'a')
            g.forget();self.assertEqual(g.snapshot()['interaction_events'],[])

    def test_only_recent_target_matched_reports_enter_compact_context(self):
        evidence=encode_interaction('I feel happy about piano. I am stressed about work.',100)
        self.assertEqual(current_affect(evidence,'classical piano',now=200)[0]['feeling'],'happy')
        self.assertEqual(current_affect(evidence,'surfing',now=200),[])
        self.assertEqual(current_affect(evidence,'piano',now=2000),[])
