"""Bounded recent evidence vectors, populated only by the background worker."""
import math
import re
import threading


class RecentMemory:
    capacity = 128
    lifetime = 30 * 60
    batch_size = 8

    def __init__(self, embedder):
        self.embedder = embedder
        self.lock = threading.RLock()
        self.rows = {}
        self.generation = 0
        self.sweep = {}
        self.state = 'waiting for recent evidence'

    def clear(self):
        with self.lock:
            self.rows.clear()
            self.sweep = {}
            self.generation += 1

    def sources(self, now):
        with self.lock:
            previous = len(self.rows)
            self.rows = {key: row for key, row in self.rows.items()
                         if row['time'] > now - self.lifetime}
            if len(self.rows) != previous:
                self.sweep = {}
                self.generation += 1
            return [dict(row, vector=list(row['vector'])) for row in self.rows.values()]

    def snapshot(self, now):
        with self.lock:
            sources = self.sources(now)
            return {'count':len(sources), 'capacity':self.capacity, 'lifetime_seconds':self.lifetime,
                    'state':self.state, 'sweep':self.sweep}

    def update(self, graph):
        now = graph.clock()
        with graph.lock, graph.db() as db:
            cutoff = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
            recent = [dict(row) for row in db.execute(
                'SELECT id,t,text,owner_id FROM jobs WHERE owner_id IS NOT NULL AND t>? '
                'ORDER BY t DESC,id DESC LIMIT ?', (max(cutoff, now-self.lifetime), self.capacity))]
            with self.lock:
                keep = {'turn:'+row['id'] for row in recent}
                if set(self.rows) - keep:
                    self.sweep = {}
                    self.generation += 1
                self.rows = {key: row for key, row in self.rows.items() if key in keep}
                generation = self.generation
                pending = [row for row in reversed(recent) if 'turn:'+row['id'] not in self.rows][:self.batch_size]
        if not pending:
            self.state = 'up to date'
            return False
        # No graph/cache lock is held during local inference.
        vectors = self.embedder.embed([row['text'] for row in pending])
        if len(vectors) != len(pending):
            raise ValueError('Incomplete recent-memory embeddings')
        sources = []
        for row, vector in zip(pending, vectors):
            vector = [float(value) for value in vector]
            if not vector or not all(math.isfinite(value) for value in vector):
                raise ValueError('Invalid recent-memory embedding')
            sources.append({'id':'turn:'+row['id'], 'text':row['text'], 'note':row['text'],
                            'scope':'profile:'+row['owner_id'], 'kind':'utterance', 'time':row['t'],
                            'negative':bool(re.search(r"\b(not|never|dislikes?|hates?|no longer|doesn't|don't)\b", row['text'], re.I)),
                            'vector':vector})
        with graph.lock, graph.db() as db, self.lock:
            current = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
            if current != cutoff or generation != self.generation:
                return False
            self.rows.update({row['id']: row for row in sources
                              if row['time'] > graph.clock()-self.lifetime})
            self.sweep = {}
            self.generation += 1
            self.state = 'up to date'
        return True
