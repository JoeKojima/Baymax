import unittest
from features.memory_palace._04_consolidation.clustering import memory_clusters, memory_hierarchies, hierarchy_clusters
from features.memory_palace._04_consolidation.sweeper import consolidate


def source(sid, vector, scope='profile:a'):
    return {'id':sid,'vector':vector,'scope':scope,'kind':'preference',
            'text':sid,'note':sid,'time':100,'negative':False}


class ClusteringTests(unittest.TestCase):
    def test_clusters_follow_vectors_not_words_and_do_not_force_singletons(self):
        rows = [source('unrelated words A',[1.,0.]), source('different words B',[.9,.1]),
                source('unrelated words A copy',[0.,1.])]
        clusters = memory_clusters(rows)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(set(clusters[0]['members']), {r['id'] for r in rows[:2]})
        self.assertIn(clusters[0]['label'], [r['note'] for r in rows[:2]])

    def test_scopes_invalid_vectors_and_conversation_summaries_do_not_cluster(self):
        rows = [source('a',[1.,0.]),source('other',[1.,0.],scope='profile:b'),
                source('bad',[float('nan'),0.]),source('zero',[0.,0.]),source('missing',None),
                {**source('conversation',[1.,0.]),'kind':'conversation'}]
        self.assertEqual(memory_clusters(rows), [])

    def test_cluster_ids_and_members_do_not_depend_on_input_order(self):
        rows = [source('a',[1.,0.]),source('b',[.9,.1]),source('c',[0.,1.])]
        self.assertEqual(memory_clusters(rows), memory_clusters(list(reversed(rows))))

    def test_sweeper_exports_clusters_without_rewriting_source_metadata(self):
        rows = [source('a',[1.,0.]),source('b',[.9,.1])]
        import copy
        original = copy.deepcopy(rows)
        result = consolidate(rows, now=100)
        self.assertEqual(len(result['clusters']), 1)
        self.assertEqual(result['clusters'][0]['method'], 'average-link hierarchy; adaptive display cut')
        self.assertEqual(rows, original)

    def test_full_hierarchy_keeps_all_levels_and_display_cuts_are_adjustable(self):
        rows = [source('a',[1.,0.]),source('b',[.98,.2]),
                source('c',[0.,1.]),source('d',[.2,.98])]
        tree = memory_hierarchies(rows)[0]
        self.assertEqual(len(tree['merges']), 3)
        self.assertEqual(len(hierarchy_clusters(tree)), 2)
        self.assertEqual(len(hierarchy_clusters(tree, 1)), 1)
        self.assertEqual(set(hierarchy_clusters(tree, 1)[0]['members']), {'a','b','c','d'})
        self.assertEqual(hierarchy_clusters(tree, 100), [])
        self.assertTrue(all('threshold' not in node for node in tree['merges']))

    def test_weak_positive_associations_keep_weights_without_the_old_cutoff(self):
        result = consolidate([source('a',[1.,0.]),source('b',[.2,.98])], now=100)
        self.assertEqual(len(result['links']), 1)
        self.assertGreater(result['links'][0]['score'], 0)
        self.assertLess(result['links'][0]['score'], .7)
        self.assertEqual(result['links'][0]['relation'], 'similarity_hint')


if __name__ == '__main__':
    unittest.main()
