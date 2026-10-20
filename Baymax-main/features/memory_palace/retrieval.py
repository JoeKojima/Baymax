"""Local semantic index; SQLite graph remains the source of truth for ownership."""
import threading
import json


class GraphIndex:
    def __init__(self, embedder):
        self.embedder = embedder
        self.lock = threading.RLock()
        self.collection = embedder._chroma_client.get_or_create_collection(
            name='memory_palace_graph',metadata={'hnsw:space':'cosine'},embedding_function=None)

    def update(self, graph):
        with graph.lock, self.lock, graph.db() as db:
            if self.collection.count() == 0:
                db.execute('DELETE FROM graph_indexed')
            rows=[dict(r) for r in db.execute('''SELECT c.* FROM claims c LEFT JOIN graph_indexed i ON c.id=i.id
                 WHERE c.owner_id IS NOT NULL AND i.id IS NULL LIMIT 16''')]
            if not rows:
                return
            vectors=self.embedder.embed([r['kind']+': '+r['value'] for r in rows])
            self.collection.upsert(ids=[r['id'] for r in rows],documents=[r['value'] for r in rows],
                embeddings=vectors.tolist(),metadatas=[{'owner_id':r['owner_id']} for r in rows])
            db.executemany('INSERT OR IGNORE INTO graph_indexed VALUES (?)',[(r['id'],) for r in rows])

    def search(self, graph, query, owner_id, limit=3):
        if not owner_id or not query.strip():
            return []
        with self.lock:
            vector=self.embedder.embed(query)[0].tolist()
            found=self.collection.query(query_embeddings=[vector],where={'owner_id':owner_id},
                                         n_results=8,include=['distances'])
        candidates=[]
        with graph.lock, graph.db() as db:
            for cid,distance in zip((found.get('ids') or [[]])[0],(found.get('distances') or [[]])[0]):
                if distance>0.65:
                    continue
                # Recheck source ownership and deletion even when an index is stale.
                row=db.execute('SELECT * FROM claims WHERE id=? AND owner_id=?',(cid,owner_id)).fetchone()
                if row is None:
                    continue
                conflict=db.execute('SELECT 1 FROM claims WHERE owner_id=? AND kind=? AND lower(value)=lower(?) AND status<>?',
                                     (owner_id,row['kind'],row['value'],row['status'])).fetchone()
                if conflict:
                    continue
                evidence=db.execute('SELECT e.quote,e.source_id FROM evidence e LEFT JOIN jobs j ON j.id=e.source_id WHERE e.claim_id=? ORDER BY j.t DESC LIMIT 1',(cid,)).fetchone()
                if evidence:
                    event=db.execute('SELECT data FROM interaction_events WHERE id=? AND owner_id=?',(evidence['source_id'],owner_id)).fetchone()
                    candidates.append({'id':cid,'kind':row['kind'],'value':row['value'],'status':row['status'],
                                       'quote':evidence['quote'],'last_seen':row['last_seen'],
                                       'interaction':json.loads(event['data']) if event else None})
        return candidates[:limit]

    def clear(self):
        with self.lock:
            while True:
                ids=self.collection.get(limit=100,include=[])['ids']
                if not ids:
                    break
                self.collection.delete(ids=ids)
