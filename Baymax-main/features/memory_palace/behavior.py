"""Bounded, best-effort behavioral telemetry. Producers never touch disk or models."""
import json
import math
import queue
import time
import uuid

KINDS = frozenset(('user_turn', 'assistant_transcript', 'offer_presented',
    'action_started', 'action_stopped', 'action_skipped', 'action_replayed',
    'item_saved', 'item_dismissed', 'tool_started', 'tool_succeeded', 'tool_failed'))
NUMBERS = {'word_count':4000, 'response_gap_proxy_ms':180000,
    'transcript_span_ms':180000, 'elapsed_ms':86400000, 'played_ms':86400000}
TEXT = {'turn_id', 'reply_turn_id', 'tool', 'measurement_source'}


def clean_payload(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, limit in NUMBERS.items():
        value = raw.get(key)
        if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= limit:
            out[key] = round(value, 1)
    for key in TEXT:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:100]
    if isinstance(raw.get('overlap_observed'), bool):
        out['overlap_observed'] = raw['overlap_observed']
    return out


class BehaviorRecorder:
    """Only bounded allocation + put_nowait on the conversation path.

    IDs returned by emit identify *queued*, not necessarily persisted events.
    Adapters report real outcomes, never inferred acceptance or enjoyment.
    """
    def __init__(self, capacity=512, clock=time.time):
        self.queue = queue.Queue(maxsize=capacity)
        self.clock = clock
        self.session_id = uuid.uuid4().hex
        self.dropped = 0
        self.failed_batches = 0

    def emit(self, kind, context, payload=None, ref_id=None, topic_id=None, event_id=None):
        owner = context.get('person_id') if isinstance(context, dict) else None
        if kind not in KINDS or not isinstance(owner, str) or not owner or len(owner)>100:
            return None
        if any(value is not None and (not isinstance(value, str) or not value or len(value)>100)
               for value in (ref_id, topic_id, event_id)):
            return None
        event = (event_id or uuid.uuid4().hex, owner, self.session_id, self.clock(), kind,
                 ref_id, topic_id, json.dumps(clean_payload(payload), separators=(',', ':')))
        try:
            self.queue.put_nowait(event)
            return event[0]
        except queue.Full:
            self.dropped += 1
            return None

    def drain(self, graph, limit=128):
        """Called only by the background worker. Failed optional telemetry is dropped."""
        rows = []
        for _ in range(limit):
            try:
                rows.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if not rows:
            return
        try:
            with graph.lock, graph.db() as db:
                cutoff = db.execute("SELECT value FROM meta WHERE key='forget_before'").fetchone()[0]
                for row in rows:
                    if row[3] <= cutoff:
                        continue
                    # References cannot cross profiles or point into forgotten history.
                    if row[5] and not db.execute('SELECT 1 FROM behavior_events WHERE id=? AND owner_id=?',
                                                (row[5], row[1])).fetchone():
                        row = (*row[:5], None, *row[6:])
                    db.execute('INSERT OR IGNORE INTO behavior_events VALUES (?,?,?,?,?,?,?,?)', row)
                db.execute('DELETE FROM behavior_events WHERE t < ?', (self.clock()-30*86400,))
                db.execute('DELETE FROM behavior_events WHERE id IN (SELECT id FROM behavior_events ORDER BY t DESC,id DESC LIMIT -1 OFFSET 20000)')
        except Exception:
            self.failed_batches += 1
            self.dropped += len(rows)

    def health(self):
        return {'queued':self.queue.qsize(), 'capacity':self.queue.maxsize,
                'dropped':self.dropped, 'failed_batches':self.failed_batches,
                'delivery':'best effort; no reply waits; recent events may be lost on exit'}


def install(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS behavior_events (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_id TEXT NOT NULL,
        t REAL NOT NULL, kind TEXT NOT NULL, ref_id TEXT, topic_id TEXT, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS behavior_owner_time ON behavior_events(owner_id,t);
        CREATE INDEX IF NOT EXISTS behavior_reference ON behavior_events(ref_id);''')


def snapshot(db, recorder=None):
    # Bounded aggregate window, no raw transcript text and no satisfaction score.
    counts = [dict(row) for row in db.execute('''SELECT owner_id,kind,count(*) AS count
        FROM behavior_events GROUP BY owner_id,kind ORDER BY owner_id,kind LIMIT 200''')]
    events = [dict(row) for row in db.execute('SELECT * FROM behavior_events ORDER BY t DESC,rowid DESC LIMIT 100')]
    for event in events:
        event['metrics'] = json.loads(event.pop('data'))
    offers = [dict(row) for row in db.execute('''SELECT o.owner_id,o.topic_id,
        count(*) AS observed_offers,
        sum(EXISTS(SELECT 1 FROM behavior_events a WHERE a.ref_id=o.id
            AND a.owner_id=o.owner_id AND a.t>=o.t AND a.kind='action_started')) AS offers_followed_by_start
        FROM behavior_events o WHERE o.kind='offer_presented' GROUP BY o.owner_id,o.topic_id LIMIT 100''')]
    sessions = [dict(row) for row in db.execute('''SELECT owner_id,session_id,
        min(t) AS first_event,max(t) AS last_event,
        sum(kind='user_turn') AS user_turns,sum(kind='assistant_transcript') AS assistant_turns,
        sum(kind='user_turn' AND ref_id IS NOT NULL) AS linked_responses
        FROM behavior_events GROUP BY owner_id,session_id ORDER BY last_event DESC LIMIT 100''')]
    mentions = [dict(row) for row in db.execute('''SELECT c.owner_id,c.id AS claim_id,
        count(*) AS source_mentions,count(DISTINCT date(j.t,'unixepoch')) AS observed_days,
        min(j.t) AS first_seen,max(j.t) AS last_seen FROM claims c
        JOIN evidence e ON e.claim_id=c.id JOIN jobs j ON j.id=e.source_id
        WHERE c.owner_id IS NOT NULL GROUP BY c.id ORDER BY last_seen DESC LIMIT 100''')]
    return {'counts':counts, 'events':events, 'offers':offers,
            'sessions':sessions, 'mentions':mentions,
            'health':recorder.health() if recorder else {'delivery':'disabled'},
            'retention_days':30, 'event_limit':20000,
            'note':'Observed events only. Missing outcomes are unknown, not rejection. '
                   'Assistant transcripts do not prove playback or an offer. Timing is a transcript proxy. '
                   'Runtime sessions are not conversation boundaries. All views are bounded. '
                   'Uninstrumented playback, speech duration and topic initiation stay unmeasured.'}
