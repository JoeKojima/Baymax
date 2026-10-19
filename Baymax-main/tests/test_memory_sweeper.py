import copy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from features.memory_palace.graph import PalaceGraph
from features.memory_palace.sweeper import MemorySweeper, consolidate, contextual_dates, read_sources


def source(sid, note='Likes piano.', scope='person:a', kind='preference', day=18, negative=False):
    return dict(id=sid, text=note, note=note, scope=scope, kind=kind,
                time=datetime(2026,10,day,18).timestamp(), negative=negative, vector=[1.,0.])


class SweeperTests(unittest.TestCase):
    def test_exact_copies_combine_evidence_without_modifying_sources(self):
        sources=[source('a'),source('b','likes piano'),source('c',scope='person:b'),source('d',scope='')]
        original=copy.deepcopy(sources)
        result=consolidate(sources)
        duplicates=[g for g in result['groups'] if g['type']=='exact_duplicate']
        self.assertEqual(len(duplicates),1)
        self.assertEqual(duplicates[0]['members'],['a','b'])
        self.assertEqual(sources,original)
        self.assertEqual(result['links'],[])

    def test_episodes_and_legacy_facts_on_different_days_stay_distinct(self):
        for kind in ('conversation','fact'):
            result=consolidate([source('a',kind=kind),source('b',kind=kind,day=19)])
            self.assertFalse(any(g['type']=='exact_duplicate' for g in result['groups']))

    def test_neighbors_form_grounded_digest_not_a_merged_personal_fact(self):
        rows=[source('a','Plays piano.'),source('b','Enjoys classical music.'),source('c','Likes food.',scope='person:b')]
        result=consolidate(rows)
        self.assertEqual(len(result['links']),1)
        self.assertEqual(result['links'][0]['relation'],'similarity_hint')
        group=result['groups'][0]
        self.assertEqual(group['type'],'related_notes')
        self.assertEqual(group['notes'],[r['note'] for r in rows[:2]])
        self.assertNotIn('fact',group)

    def test_negative_neighbor_is_a_difference_to_review_not_consolidation(self):
        result=consolidate([source('a'),source('b','Does not like piano.',negative=True)])
        self.assertEqual(result['links'][0]['relation'],'review_difference')
        self.assertFalse(result['groups'])

    def test_similarity_edges_are_bounded_and_skip_broad_episode_summaries(self):
        rows=[source(str(i),'Topic '+str(i)) for i in range(20)]+[source('episode',kind='conversation')]
        result=consolidate(rows)
        degree={}
        for edge in result['links']:
            for sid in (edge['source'],edge['target']):degree[sid]=degree.get(sid,0)+1
        self.assertLessEqual(max(degree.values()),2)
        self.assertNotIn('episode',degree)

    def test_missing_year_uses_a_matching_conversation_frame_as_candidate(self):
        row=source('episode','On Sunday, October 18, Alex talked with Ember about piano.',kind='conversation',day=19)
        result=contextual_dates([row])
        self.assertEqual(result[0]['date'],'2026-10-18')
        self.assertEqual(result[0]['status'],'context-derived candidate')
        self.assertIn('Sunday, October 18',row['text'])
        self.assertEqual(contextual_dates([{**row,'kind':'fact'}]),[])
        self.assertEqual(contextual_dates([{**row,'text':row['text'].replace('Sunday','Monday')}]),[])
        self.assertEqual(contextual_dates([{**row,'text':row['text'].replace('October 18,','October 18, 2026,')}]),[])

    def test_old_ambiguous_or_ordinary_event_dates_are_not_filled(self):
        rows=[source('a','Alex visited on October 18.',kind='fact'),
              source('b','On Monday, January 1, Alex talked with Ember about piano.',kind='conversation')]
        self.assertEqual(contextual_dates(rows),[])

    def test_fact_year_candidate_requires_matching_nearby_same_scope_context(self):
        frame=source('frame','On Sunday, October 18, Alex talked with Ember about work.',kind='conversation')
        fact=source('fact','Was working on Sunday, October 18.',kind='fact',day=19)
        result=contextual_dates([frame,fact])
        candidate=next(d for d in result if d['source_id']=='fact')
        self.assertEqual(candidate['date'],'2026-10-18')
        self.assertEqual(candidate['context_source_id'],'frame')
        self.assertEqual(len(contextual_dates([frame,{**fact,'scope':'other'}])),1)
        self.assertEqual(len(contextual_dates([frame,{**fact,'text':'Was working on Sunday, October 18, 2020.'}])),1)

    def test_persistent_result_is_reused_and_forgetting_clears_it(self):
        with tempfile.TemporaryDirectory() as folder:
            graph=PalaceGraph(Path(folder)/'memory.sqlite3')
            sweeper=MemorySweeper(graph,lambda:[source('a'),source('b')]);graph.sweeper=sweeper
            self.assertTrue(sweeper.step())
            self.assertFalse(sweeper.step())
            self.assertEqual(graph.snapshot()['sweep']['source_count'],2)
            graph.forget()
            self.assertNotIn('source_count',graph.snapshot()['sweep'])

    def test_forget_wins_over_inflight_sweep(self):
        with tempfile.TemporaryDirectory() as folder:
            graph=PalaceGraph(Path(folder)/'memory.sqlite3')
            def collect():
                graph.forget()
                return [source('a'),source('b')]
            sweeper=MemorySweeper(graph,collect)
            self.assertFalse(sweeper.step())
            self.assertEqual(sweeper.state,'discarded after forgetting')
            self.assertNotIn('source_count',graph.snapshot()['sweep'])

    def test_not_ready_store_defers_instead_of_deleting_previous_results(self):
        with tempfile.TemporaryDirectory() as folder:
            graph=PalaceGraph(Path(folder)/'memory.sqlite3')
            sweeper=MemorySweeper(graph,lambda:[source('a')]);sweeper.step()
            sweeper.sources=lambda:None
            self.assertFalse(sweeper.step())
            self.assertEqual(graph.snapshot()['sweep']['source_count'],1)
            self.assertIsNone(read_sources(None,graph))
