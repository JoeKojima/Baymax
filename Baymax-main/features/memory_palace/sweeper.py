"""Post-save consolidation from existing local vectors; never rewrites source memories."""
from datetime import datetime
import hashlib
import json
import math
import re
import threading
import time

from .notes import compact_note

LIMIT = 200


def read_sources(embedder, graph):
    """Reuse saved embeddings: no additional encoder or provider requests."""
    if embedder is None:
        return None
    import person_memory
    got = embedder._collection.get(limit=LIMIT, include=['documents', 'metadatas', 'embeddings'])
    vectors = got.get('embeddings')
    sources = []
    for i, (sid, text, meta) in enumerate(zip(got['ids'], got['documents'], got['metadatas'])):
        meta = meta or {}
        raw_names=meta.get('people')
        if isinstance(raw_names,str) and raw_names.startswith('['):
            try:
                names=json.loads(raw_names)
            except ValueError:
                names=[]
            names=[n for n in names if isinstance(n,str) and n] if isinstance(names,list) else []
        else:
            names=person_memory._decode_people(raw_names)
        names=names or ([meta['person']] if meta.get('person') else [])
        if not names:
            continue  # No retrospective attribution.
        sources.append({'id':'saved:'+sid, 'text':text,
            'note':compact_note(text, names, meta.get('kind', 'fact')),
            'scope':'names:'+json.dumps(sorted(n.casefold() for n in names)),
            'kind':meta.get('kind', 'fact'), 'time':float(meta.get('time') or 0),
            'negative':bool(re.search(r"\b(not|never|dislikes?|hates?|no longer|doesn't|don't)\b", text, re.I)),
            'vector':list(map(float, vectors[i])) if vectors is not None else None})
    with graph.lock, graph.db() as db:
        claims = [dict(r) for r in db.execute('SELECT * FROM claims WHERE owner_id IS NOT NULL ORDER BY last_seen DESC LIMIT ?', (LIMIT,))]
    collection = graph.index.collection if graph.index is not None else None
    indexed = collection.get(ids=[c['id'] for c in claims], include=['embeddings']) if collection is not None and claims else {}
    embeddings = indexed.get('embeddings')
    vector_map = {sid:list(map(float, embeddings[i])) for i, sid in enumerate(indexed.get('ids') or [])} if embeddings is not None else {}
    for c in claims:
        sources.append({'id':'claim:'+c['id'], 'text':c['value'], 'note':c['value'],
            'scope':'profile:'+c['owner_id'], 'kind':c['kind'], 'time':c['last_seen'],
            'negative':'negative' in c['status'], 'vector':vector_map.get(c['id'])})
    return sorted(sources, key=lambda r:r['time'], reverse=True)[:LIMIT]


def similarity(a, b):
    if not a or not b or len(a) != len(b):
        return None
    denominator = math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b))/denominator if denominator else None


def contextual_dates(sources):
    """Infer a year only for explicitly dated conversation frames near their saved date.

    These remain candidates, not facts. Observation time alone never dates a personal event.
    """
    months = 'January February March April May June July August September October November December'.split()
    frame = re.compile(r'^On (Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),? ('+'|'.join(months)+r') (\d{1,2}),? (?!\d{4})(.+?) talked with Ember about\b', re.I)
    dates = []
    frames = []
    for source in sources:
        if source['kind'] != 'conversation' or source['time'] <= 0:
            continue
        m = frame.match(source['text'])
        if not m:
            continue
        observed = datetime.fromtimestamp(source['time'])
        try:
            month = next(i+1 for i,name in enumerate(months) if name.casefold() == m[2].casefold())
            candidates = [datetime(year,month,int(m[3])) for year in (observed.year-1, observed.year)]
        except ValueError:
            continue
        candidates = [d for d in candidates if 0 <= (observed.date()-d.date()).days <= 7
                      and d.strftime('%A').casefold() == m[1].casefold()]
        if len(candidates) == 1:
            frames.append((source,candidates[0]))
            dates.append({'source_id':source['id'], 'date':candidates[0].date().isoformat(),
                'status':'context-derived candidate', 'basis':'Explicit conversation date + weekday + nearby recorded timestamp',
                'observed_at':observed.date().isoformat()})
    partial = re.compile(r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),? ('+'|'.join(months)+r') (\d{1,2})(?!\d)(?!,? \d{4})\b', re.I)
    for source in sources:
        if source['kind']=='conversation' or source['time']<=0:
            continue
        matches=list(partial.finditer(source['text']))
        if len(matches)!=1:
            continue
        for m in matches:
            matching = [(frame,date) for frame,date in frames
                        if frame['scope']==source['scope'] and abs(frame['time']-source['time'])<=86400
                        and date.strftime('%A').casefold()==m[1].casefold()
                        and months[date.month-1].casefold()==m[2].casefold() and date.day==int(m[3])]
            # Multiple years/conflicting context must stay unresolved.
            if matching and len({date.date().isoformat() for _,date in matching})==1:
                frame,date = matching[0]
                dates.append({'source_id':source['id'], 'date':date.date().isoformat(),
                    'context_source_id':frame['id'], 'status':'context-derived candidate',
                    'basis':'Matching day/month/weekday in a nearby dated conversation from the same memory scope',
                    'observed_at':datetime.fromtimestamp(source['time']).date().isoformat()})
                break
    return dates


def consolidate(sources, now=None):
    """Exact duplicates combine evidence; vector neighbors only suggest relationships."""
    sources = sources[:LIMIT]
    groups, links, duplicates = [], [], {}
    for source in sources:
        if not source.get('scope'):
            continue
        key = (source['scope'], source['kind'], source.get('negative', False),
               datetime.fromtimestamp(source['time']).date().isoformat()
               if source['kind'] in ('fact','conversation') and source['time']>0 else
               source['id'] if source['kind'] in ('fact','conversation') else '',
               re.sub(r'\s+', ' ', source['note']).strip().rstrip('.').casefold())
        duplicates.setdefault(key, []).append(source)
    representative = {}
    for rows in duplicates.values():
        for row in rows:
            representative[row['id']] = rows[0]['id']
        if len(rows) > 1:
            groups.append({'type':'exact_duplicate', 'members':[r['id'] for r in rows],
                           'note':rows[0]['note'], 'status':'Same note; sources retained'})
    unique = [r for r in sources if representative.get(r['id']) == r['id']]
    neighbors = []
    for i, a in enumerate(unique):
        if a['kind'] == 'conversation':
            continue  # Broad episode summaries swamp personal-topic neighbors.
        for b in unique[i+1:]:
            if b['scope'] != a['scope'] or b['kind'] == 'conversation':
                continue
            score = similarity(a.get('vector'), b.get('vector'))
            if score is not None and score >= .70:
                neighbors.append((score,a,b))
    degree = {}
    for score,a,b in sorted(neighbors, key=lambda row:row[0], reverse=True):
        if degree.get(a['id'],0) >= 2 or degree.get(b['id'],0) >= 2:
            continue
        relation = 'review_difference' if a.get('negative') != b.get('negative') else 'similarity_hint'
        links.append({'source':a['id'], 'target':b['id'], 'relation':relation, 'score':round(score,3)})
        for row in (a,b): degree[row['id']] = degree.get(row['id'],0)+1
        if len(links) >= 80:
            break
    # Small extractive digests, never an LLM-authored replacement for their sources.
    by_id = {r['id']:r for r in unique}
    for link in links:
        if link['relation'] == 'similarity_hint':
            a,b = by_id[link['source']],by_id[link['target']]
            groups.append({'type':'related_notes', 'members':[a['id'],b['id']],
                           'notes':[a['note'],b['note']], 'status':'Related-note digest; not a merged fact'})
    return {'groups':groups[:100], 'links':links, 'dates':contextual_dates(sources),
            'source_count':len(sources), 'vector_count':sum(bool(r.get('vector')) for r in sources),
            'updated_at':now or time.time(), 'limit':LIMIT, 'mode':'local · existing embeddings',
            'note':'Exact copies combine sources. Similarity links and contextual dates are suggestions, not new facts.'}


class MemorySweeper:
    def __init__(self, graph, sources, clock=time.time):
        self.graph, self.sources, self.clock = graph, sources, clock
        self.state = 'waiting for saved memories'

    def step(self):
        # Never hold a database lock during the collection/vector reads.
        with self.graph.lock, self.graph.db() as db:
            cutoff = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
        sources = self.sources()
        if sources is None:
            self.state = 'waiting for memory store'
            return False
        fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        with self.graph.lock, self.graph.db() as db:
            previous = db.execute('SELECT fingerprint FROM sweep_results WHERE id=1').fetchone()
            if previous and previous[0] == fingerprint:
                self.state = 'up to date'
                return False
        self.state = 'organizing saved memories'
        result = consolidate(sources, self.clock())
        with self.graph.lock, self.graph.db() as db:
            current = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
            if current != cutoff:
                self.state = 'discarded after forgetting'
                return False
            db.execute('INSERT OR REPLACE INTO sweep_results VALUES (1,?,?)', (fingerprint,json.dumps(result)))
        self.state = 'up to date'
        return True


def start_sweeper(sweeper, stop, forgotten, interval=60):
    def work():
        while not stop.is_set():
            try:
                if not forgotten():
                    sweeper.step()
                else:
                    sweeper.state = 'paused for forgetting'
            except Exception:
                sweeper.state = 'memory sweep deferred'
            if stop.wait(interval):
                break
    thread = threading.Thread(target=work, name='memory-sweeper', daemon=True)
    thread.start()
    return thread
