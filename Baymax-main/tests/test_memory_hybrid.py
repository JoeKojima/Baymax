import json
from pathlib import Path
import tempfile
import unittest
from features.memory_palace.graph import PalaceGraph, LocalEncoder
from features.memory_palace.hybrid import HybridEncoder, extract_batch

class HybridTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1000;self.calls=[]
        self.encoder=HybridEncoder(self.generate, LocalEncoder())
        self.graph=PalaceGraph(Path(self.tmp.name)/'graph.sqlite3', self.encoder,clock=lambda:self.now)
    def tearDown(self):self.tmp.cleanup()
    def generate(self,prompt,**kwargs):
        self.calls.append((prompt,kwargs))
        sources=json.loads(prompt.split('data, not instructions:\n')[1])
        return json.dumps({'claims':[{'source_id':s['source_id'],'kind':'affiliation','value':'UCSF',
               'quote':s['text'],'polarity':'positive'} for s in sources]})
    def ingest(self,text='I work at UCSF.',t=100):
        self.graph.ingest([{'speaker':'user','t':t,'text':text}])
    def test_batch_and_real_schema_and_persisted_enrichment(self):
        for i in range(8):self.ingest(t=100+i)
        self.assertTrue(self.graph.step())
        self.assertEqual(len(self.calls),1)
        self.assertIn('response_schema',self.calls[0][1])
        self.assertEqual(self.graph.snapshot()['queue'],{'done':8})
        with self.graph.db() as db:self.assertEqual(db.execute('SELECT sum(enriched) FROM jobs').fetchone()[0],8)
        self.assertIn('hybrid',self.graph.snapshot()['encoder'])
        self.now+=121;self.graph.step();self.assertEqual(len(self.calls),1)
    def test_recent_turns_wait_instead_of_one_request_per_sentence(self):
        self.ingest(t=990);self.graph.step();self.assertEqual(self.calls,[])
        self.now=1120;self.graph.step();self.assertEqual(len(self.calls),1)
    def test_quota_failure_keeps_local_output_and_enrichment_work(self):
        def fail(*a,**k):raise RuntimeError('quota')
        self.encoder.generate=fail;self.ingest('I like UCSF.')
        self.graph.step();s=self.graph.snapshot()
        self.assertEqual(s['total'],1);self.assertEqual(s['enrichment_pending'],1)
        self.assertIn('deferred',s['enrichment_status'])
        self.now+=301;self.encoder.generate=self.generate;self.graph.step()
        self.assertEqual(self.graph.snapshot()['enrichment_pending'],0)
    def test_foreign_source_or_invented_quote_or_value_is_rejected(self):
        job={'id':'a','text':'I work at UCSF.'}
        base={'source_id':'a','kind':'affiliation','value':'UCSF','quote':job['text'],'polarity':'positive'}
        for change in ({'source_id':'other'},{'quote':'invented quote'},{'value':'Stanford'},{'kind':'identity'},{'polarity':'delete'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                extract_batch(lambda *a,**k:json.dumps({'claims':[{**base,**change}]}),[job])
    def test_forget_during_call_drops_output(self):
        def generate(*a,**k):
            result=self.generate(*a,**k);self.graph.forget();return result
        self.encoder.generate=generate;self.ingest();self.graph.step()
        self.assertEqual(self.graph.snapshot()['total'],0)
    def test_budget_survives_worker_restart(self):
        self.encoder.daily_limit=1;self.ingest();self.graph.step()
        again=HybridEncoder(self.generate,LocalEncoder(),daily_limit=1)
        self.graph.encoder=again;self.now+=200;self.ingest(t=200);self.graph.step()
        self.assertEqual(len(self.calls),1);self.assertIn('budget reached',again.status)
    def test_positive_and_negative_evidence_are_both_retained(self):
        self.ingest('I like UCSF.')
        self.graph.step();self.now+=121
        def negative(prompt,**kwargs):
            data=json.loads(self.generate(prompt,**kwargs));data['claims'][0]['polarity']='negative'
            return json.dumps(data)
        self.encoder.generate=negative;self.ingest('I do not like UCSF.',t=200);self.graph.step()
        self.assertTrue(any('negative' in c['status'] for c in self.graph.snapshot()['claims']))
        self.assertTrue(any(c['status']=='unattributed proposal' for c in self.graph.snapshot()['claims']))
    def test_existing_database_is_migrated_without_losing_jobs(self):
        import sqlite3
        from contextlib import closing
        path=Path(self.tmp.name)/'old.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY,t REAL,text TEXT,state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,retry REAL DEFAULT 0)")
            db.execute("INSERT INTO jobs(id,t,text) VALUES ('legacy',100,'I work at UCSF.')")
            db.commit()
        graph=PalaceGraph(path,self.encoder,clock=lambda:self.now)
        graph.step()
        self.assertEqual(graph.snapshot()['total'],1)
        self.assertEqual(graph.snapshot()['enrichment_pending'],0)
    def test_empty_response_completes_without_inventing_memories(self):
        self.encoder.generate=lambda *a,**k:json.dumps({'claims':[]})
        self.ingest();self.graph.step();self.assertEqual(self.graph.snapshot()['total'],0)
        self.assertEqual(self.graph.snapshot()['enrichment_pending'],0)

if __name__=='__main__':unittest.main()
