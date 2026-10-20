import tempfile
from pathlib import Path
import threading
import unittest
from features.memory_palace.graph import PalaceGraph, LocalEncoder, explicit_claims, KINDS

class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.graph = PalaceGraph(Path(self.tmp.name)/'graph.sqlite3', clock=lambda:self.now)
    def tearDown(self):
        self.tmp.cleanup()
    def turn(self,text,t=100):
        return {'speaker':'user','text':text,'t':t,'present':['James']}
    def test_local_extraction_and_no_face_attribution(self):
        self.graph.ingest([self.turn('I like technology news. I am learning Rust.')])
        self.assertTrue(self.graph.step())
        s=self.graph.snapshot()
        self.assertEqual(len(s['claims']),2)
        self.assertEqual(len(s['edges']),len(KINDS)+2)
        self.assertNotIn('James',str(s))
        self.assertTrue(all(c['status']=='unattributed proposal' for c in s['claims']))
    def test_restart_and_duplicate_evidence(self):
        self.graph.ingest([self.turn('I like cats.')]);self.graph.step()
        self.graph.ingest([self.turn('I like cats.')]);self.assertFalse(self.graph.step())
        self.graph.ingest([self.turn('I like cats.',101)]);self.graph.step()
        reloaded=PalaceGraph(self.graph.path)
        self.assertEqual(reloaded.snapshot()['total'],1)
        self.assertEqual(len(reloaded.snapshot()['claims'][0]['evidence']),2)
    def test_ember_words_are_not_user_facts(self):
        t=self.turn('I like cats.');t['speaker']='ember'
        self.graph.ingest([t]);self.assertFalse(self.graph.step())
    def test_no_unsupported_inference(self):
        for text in ('Do I like cats?', 'If I like cats, what then?', 'She said I like cats.',
                     'I no longer like cats.', 'I used to like cats.', 'She said "I like cats".'):
            with self.subTest(text=text):self.assertEqual(explicit_claims(text),[])
    def test_failed_encoder_retries_without_losing_job(self):
        self.graph.encoder=lambda text: [{'kind':'preference','value':'cats','quote':'invented'}]
        self.graph.ingest([self.turn('I like cats.')]);self.assertFalse(self.graph.step())
        self.assertEqual(self.graph.snapshot()['queue'],{'pending':1})
        self.assertEqual(self.graph.snapshot()['total'],0)
        self.graph.encoder=LocalEncoder();self.now+=301
        self.assertTrue(self.graph.step())
    def test_forget_removes_evidence_and_prevents_replay(self):
        turns=[self.turn('I like cats.')];self.graph.ingest(turns);self.graph.step()
        self.graph.forget();self.graph.ingest(turns)
        self.assertEqual(self.graph.snapshot()['total'],0)
        self.assertFalse(self.graph.step())
    def test_forget_wins_against_inflight_encoder(self):
        entered,release=threading.Event(),threading.Event()
        def encoder(text):
            entered.set();release.wait(2);return explicit_claims(text)
        self.graph.encoder=encoder;self.graph.ingest([self.turn('I like cats.')])
        thread=threading.Thread(target=self.graph.step);thread.start();self.assertTrue(entered.wait(2))
        self.graph.forget();release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(self.graph.snapshot()['total'],0)
    def test_bad_local_model_setting(self):
        for model in ('remote/cloud','qwen3:cloud','../file'):
            with self.assertRaises(ValueError):LocalEncoder(model)
    def test_output_is_bounded(self):
        self.graph.encoder=lambda text:[{}]*9
        self.graph.ingest([self.turn('I like cats.')]);self.assertFalse(self.graph.step())
        self.assertEqual(self.graph.snapshot()['total'],0)

if __name__=='__main__':unittest.main()
