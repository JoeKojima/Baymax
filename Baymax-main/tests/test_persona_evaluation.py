"""Offline checks for the synthetic-persona evaluation harness."""
import json
from pathlib import Path
import unittest
from features.memory_palace.evaluate_persona import average_link_clusters

class PersonaEvaluationTests(unittest.TestCase):
    def test_similarity_groups_do_not_need_persona_labels(self):
        sims=[[1,.8,.1,.1],[.8,1,.2,.1],[.1,.2,1,.9],[.1,.1,.9,1]]
        groups=average_link_clusters(sims,.5)
        self.assertEqual({frozenset(g) for g in groups},{frozenset((0,1)),frozenset((2,3))})
    def test_average_link_does_not_chain_weak_links(self):
        groups=average_link_clusters([[1,.8,.1],[.8,1,.7],[.1,.7,1]],.6)
        self.assertEqual(len(groups),2)
    def test_empty_and_singleton(self):
        self.assertEqual(average_link_clusters([]),[])
        self.assertEqual(average_link_clusters([[1]]),[[0]])
    def test_fixture_is_explicitly_synthetic_and_covers_requested_topics(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/memory_persona.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertEqual(len(fixture['utterances']),20)
        self.assertEqual({r['topic'] for r in fixture['utterances']},{'music','research','surfing','investing','food'})
        self.assertEqual(len({r['id'] for r in fixture['utterances']}),20)
        self.assertEqual(len(fixture['queries']),5)
