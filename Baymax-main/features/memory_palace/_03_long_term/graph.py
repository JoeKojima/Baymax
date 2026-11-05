"""Evidence-backed local memory graph and durable, single-worker extraction queue."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
import time

from features.memory_palace._02_encoding.hybrid import KINDS
from features.memory_palace._01_short_term.interactions import encode_interaction
from .._02_encoding.local import LocalEncoder
from .._06_forgetting.retention import forget_graph
from .._04_consolidation.claims import save_claim


class PalaceGraph:
    def __init__(self, path, encoder=None, clock=time.time):
        self.path, self.encoder, self.clock = str(path), encoder or LocalEncoder(), clock
        self.index = None
        self.sweeper = None
        self.behavior = None
        self.recent = None
        self.state = 'waiting'
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
              PRAGMA secure_delete=ON;
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL);
              INSERT OR IGNORE INTO meta VALUES ('forget_before',0);
              INSERT OR IGNORE INTO meta VALUES ('claims_revision',0);
              CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, t REAL, text TEXT,
                state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, retry REAL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, kind TEXT, value TEXT,
                status TEXT, first_seen REAL, last_seen REAL);
              CREATE TABLE IF NOT EXISTS evidence (claim_id TEXT, source_id TEXT, quote TEXT,
                PRIMARY KEY(claim_id,source_id));
              CREATE TABLE IF NOT EXISTS evidence_quotes (claim_id TEXT, source_id TEXT, quote TEXT,
                PRIMARY KEY(claim_id,source_id,quote));
              INSERT OR IGNORE INTO evidence_quotes SELECT claim_id,source_id,quote FROM evidence;
              CREATE TABLE IF NOT EXISTS cloud_budget (day TEXT PRIMARY KEY, attempts INTEGER);
              CREATE TABLE IF NOT EXISTS sweep_results (id INTEGER PRIMARY KEY, fingerprint TEXT, data TEXT);
              CREATE TABLE IF NOT EXISTS interaction_events (id TEXT PRIMARY KEY, owner_id TEXT, t REAL, data TEXT);
            ''')
            db.execute('CREATE TABLE IF NOT EXISTS graph_indexed (id TEXT PRIMARY KEY)')
            for table in ('jobs', 'claims'):
                if 'owner_id' not in {r[1] for r in db.execute('PRAGMA table_info('+table+')')}:
                    db.execute('ALTER TABLE '+table+' ADD COLUMN owner_id TEXT')
            if 'enriched' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN enriched INTEGER DEFAULT 0')
            from features.memory_palace._01_short_term.behavior import install
            install(db)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, turns):
        with self.lock, self.db() as db:
            cutoff = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
            for t in turns[-200:]:
                if t.get('speaker') != 'user' or float(t.get('t', 0)) <= cutoff:
                    continue
                text = str(t.get('text', ''))[:4000]
                owner = t.get('speaker_id')
                legacy = hashlib.sha256(json.dumps([t['t'], text]).encode()).hexdigest()
                previous = db.execute('SELECT id FROM jobs WHERE id=? AND owner_id IS ?', (legacy,owner)).fetchone()
                source = previous['id'] if previous else hashlib.sha256(json.dumps([t['t'], text, owner]).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO jobs(id,t,text,owner_id) VALUES (?,?,?,?)', (source, t['t'], text, t.get('speaker_id')))
                interaction=encode_interaction(text,float(t['t']),t.get('interaction'))
                db.execute('INSERT OR IGNORE INTO interaction_events SELECT id,owner_id,t,? FROM jobs WHERE id=?',
                           (json.dumps(interaction),source))

    def step(self):
        if hasattr(self.encoder, 'process_graph'):
            return self.encoder.process_graph(self)
        return self._step_local()

    def save_claim(self, db, job, kind, value, quote, negative=False):
        cid = save_claim(db, job, kind, value, quote, negative)
        if self.recent is not None:
            with self.recent.lock:
                self.recent.sweep = {}
        return cid

    def _step_local(self):
        with self.lock, self.db() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='pending' AND retry<=? ORDER BY t LIMIT 1", (self.clock(),)).fetchone()
            cutoff = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
        if job is None:
            self.state = 'waiting'
            return False
        self.state = 'encoding'
        try:
            proposals = self.encoder(job['text'])
            if not isinstance(proposals, list) or len(proposals) > 8:
                raise ValueError('Invalid claims')
            validated = []
            for p in proposals:
                if not isinstance(p, dict):
                    raise ValueError('Invalid claim')
                kind, value, quote = p.get('kind'), p.get('value'), p.get('quote')
                if kind not in KINDS or not isinstance(value, str) or not 2 <= len(value) <= 160:
                    raise ValueError('Invalid claim value')
                if not isinstance(quote, str) or not 4 <= len(quote) <= 500 or quote not in job['text']:
                    raise ValueError('Unsupported evidence')
                # Model-generated claims stay proposals, never verified personal facts.
                validated.append((kind, value.strip(), quote))
            with self.lock, self.db() as db:
                current = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
                if current != cutoff or not db.execute('SELECT 1 FROM jobs WHERE id=?', (job['id'],)).fetchone():
                    return False  # A forget request won while inference was running.
                for kind, value, quote in validated:
                    self.save_claim(db, job, kind, value, quote)
                db.execute("UPDATE jobs SET state='done' WHERE id=?", (job['id'],))
            self.state = 'waiting'
            return True
        except Exception:
            with self.lock, self.db() as db:
                attempts = job['attempts'] + 1
                db.execute('UPDATE jobs SET attempts=?,retry=?,state=? WHERE id=?',
                           (attempts, self.clock() + min(300 * attempts, 3600),
                            'failed' if attempts >= 3 else 'pending', job['id']))
            self.state = 'encoder unavailable or invalid output'
            return False

    def forget(self):
        forget_graph(self)

    def snapshot(self):
        with self.lock, self.db() as db:
            claims = [dict(row) for row in db.execute('SELECT * FROM claims ORDER BY last_seen DESC LIMIT 100')]
            for c in claims:
                c['evidence_count'] = db.execute('SELECT count(*) FROM evidence WHERE claim_id=?', (c['id'],)).fetchone()[0]
                c['evidence'] = [dict(r) for r in db.execute(
                    'SELECT e.quote,e.source_id,j.t AS observed_at,j.owner_id,i.data AS interaction '
                    'FROM evidence e LEFT JOIN jobs j ON j.id=e.source_id '
                    'LEFT JOIN interaction_events i ON i.id=e.source_id AND i.owner_id IS j.owner_id '
                    'WHERE e.claim_id=? ORDER BY j.t DESC,e.source_id LIMIT 5', (c['id'],))]
                for evidence in c['evidence']:
                    evidence['interaction'] = json.loads(evidence['interaction']) if evidence['interaction'] else None
                    evidence['quotes'] = [r['quote'] for r in db.execute(
                        'SELECT quote FROM evidence_quotes WHERE claim_id=? AND source_id=? ORDER BY quote LIMIT 5',
                        (c['id'], evidence['source_id']))]
                events=[{**json.loads(r['data']),'source_id':r['id']} for r in db.execute('SELECT i.id,i.data FROM interaction_events i JOIN evidence e ON i.id=e.source_id WHERE e.claim_id=? AND i.owner_id IS ? ORDER BY i.t DESC LIMIT 5',(c['id'],c['owner_id']))]
                c['interaction']=events[0] if events else None
            interaction_events=[{**dict(r),'interaction':json.loads(r['data'])} for r in db.execute(
                'SELECT i.id,i.owner_id,i.t,i.data,j.text FROM interaction_events i JOIN jobs j ON j.id=i.id ORDER BY i.t DESC LIMIT 100')]
            for event in interaction_events:
                event.pop('data',None)
                event['interaction']['source_id']=event['id']
            counts = dict(db.execute('SELECT state, count(*) FROM jobs GROUP BY state').fetchall())
            total = db.execute('SELECT count(*) FROM claims').fetchone()[0]
            enrichment_pending = db.execute("SELECT count(*) FROM jobs WHERE enriched=0").fetchone()[0]
            sweep = db.execute('SELECT data FROM sweep_results WHERE id=1').fetchone()
            from features.memory_palace._01_short_term.behavior import snapshot as behavior_snapshot
            behavior = behavior_snapshot(db, self.behavior)
        owners = {c['owner_id'] for c in claims} or {None}
        root = lambda owner: owner or 'unattributed'
        return {'interaction_events':interaction_events, 'behavior':behavior,
                'short_term':{key:value for key,value in self.recent.snapshot(self.clock()).items() if key != 'sweep'}
                             if self.recent is not None else {'state':'disabled'},
                'sweep': {**(json.loads(sweep[0]) if sweep else {}),
                         'state':self.sweeper.state if self.sweeper is not None else 'disabled'},
                'nodes': [{'id':root(o),'label':o or 'Unverified speaker','kind':'person'} for o in owners]
                + [{'id':root(o)+':'+k,'label':k,'kind':'category'} for o in owners for k in KINDS]
                + [{'id':c['id'],'label':c['value'],'kind':'claim'} for c in claims],
                'edges': [{'source':root(o),'target':root(o)+':'+k,'relation':'proposed'} for o in owners for k in KINDS]
                + [{'source':root(c['owner_id'])+':'+c['kind'],'target':c['id'],'relation':c['kind']} for c in claims],
                'enrichment_pending': enrichment_pending if hasattr(self.encoder, 'process_graph') else 0, 'enrichment_status': getattr(self.encoder, 'status', 'local only'),
                'state': self.state, 'encoder': getattr(self.encoder, 'name', None) or ('local Ollama' if getattr(self.encoder, 'model', None) else 'local explicit-statement encoder'),
                'queue': counts, 'claims': claims, 'total': total, 'limit': 100,
                'note': 'Unattributed proposals with source quotes. Visible faces do not verify speakers. Conflicts require review.'}


def start_worker(graph, pending, forgotten, stop, tick=None):
    def work():
        while not stop.wait(3):
            try:
                if tick is not None:
                    tick()
                if forgotten():
                    graph.forget()
                    graph.state = 'paused for forgetting'
                    continue
                if graph.behavior is not None:
                    graph.behavior.drain(graph)
                graph.ingest(pending())
                graph.step()
                if graph.index is not None:
                    graph.index.update(graph)
                if graph.recent is not None:
                    try:
                        graph.recent.update(graph)
                    except Exception:
                        graph.recent.state = 'local embedding deferred'
            except Exception:
                graph.state = 'local storage unavailable'
    thread = threading.Thread(target=work, name='memory-encoder', daemon=True)
    thread.start()
    return thread
