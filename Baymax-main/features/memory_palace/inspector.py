"""Read-only, bounded memory snapshots from the running v10 process. No model imports."""
import json
import os
from pathlib import Path
import time

import runtime_monitor as monitor

PATH = monitor.MONITOR_DIR / '.runtime-memory.json'
LIMIT = 500


def capture(collection, people=(), profile=None, visible=(), in_prompt=(), given=None, pending=0, forgotten=()):
    profile, given = profile or {}, given or {}
    records = collection.get(limit=LIMIT, include=['documents', 'metadatas']) if collection is not None else {}
    blocked = {str(n).casefold() for n in forgotten}
    memories = []
    for mid, text, meta in zip(records.get('ids') or [], records.get('documents') or [], records.get('metadatas') or []):
        meta = meta or {}
        names = meta.get('people', [])
        if isinstance(names, str):
            if names.startswith('|') and names.endswith('|'):
                # person_memory stores Chroma's scalar metadata as |Name|Name|.
                names = [name for name in names.split('|') if name]
            else:
                try:
                    names = json.loads(names)
                except ValueError:
                    names = []
        names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
        legacy = 'people' not in meta
        if legacy and meta.get('person'):
            names = [str(meta['person'])]
        # Suppress pending-forget entries, including references in the text.
        if blocked.intersection(n.casefold() for n in names) or any(n in str(text).casefold() for n in blocked):
            continue
        when = meta.get('time')
        if when is None:
            try:
                when = time.mktime(time.strptime(meta.get('timestamp', ''), '%Y%m%d_%H%M%S'))
            except (ValueError, TypeError):
                when = 0
        try:
            when = float(when)
        except (TypeError, ValueError):
            when = 0
        memories.append({'id': str(mid), 'text': str(text), 'people': names, 'time': when,
                         'kind': meta.get('kind', 'fact'), 'legacy': legacy,
                         'source': meta.get('source', 'Unknown / older entry'),
                         'mentions': meta.get('mentions', 1),
                         'in_prompt': text in in_prompt,
                         'recalled': text in given and given[text] is not None})
    memories.sort(key=lambda m: m['time'], reverse=True)
    # Whitelist identity fields; never publish face measurements, crops or API keys.
    known = [{k: p.get(k) for k in ('id', 'name', 'relationship', 'last_seen')}
             for p in people if str(p.get('name', '')).casefold() not in blocked]
    names = {p['name'].casefold() for p in known if p.get('name')}
    for name in sorted({n for m in memories for n in m['people']}):
        if name.casefold() not in names:
            known.append({'id': None, 'name': name, 'relationship': None, 'last_seen': None})
            names.add(name.casefold())
    return {'available': collection is not None, 'space': os.getenv('EMBER_MEMORY_SPACE_NAME') or Path(os.getenv('BAYMAX_DATA_DIR', Path(__file__).resolve().parents[2])).name,
            'profile_name': profile.get('user_name') or None, 'visible': list(visible),
            'speaker': 'Unverified — a visible face does not identify the speaker',
            'people': known, 'memories': memories,
            'stored_total': collection.count() if collection is not None else 0,
            'limit': LIMIT, 'pending_turns': pending,
            'note': 'People links currently use names; they are not verified speaker labels. Older unscoped memories remain unattributed here.'}


def publish(snapshot):
    data = {**snapshot, 'pid': os.getpid(), 'updated_at': time.time()}
    temporary = PATH.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
        os.replace(temporary, PATH)
    except OSError:
        pass  # Debugging must not interrupt the robot.


def read_snapshot():
    try:
        # Bound reads even if a local file was corrupted.
        with PATH.open('rb') as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('Snapshot too large')
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('memories'), list):
            raise ValueError('Invalid snapshot')
        runtime = monitor.read_status()
        data['live'] = (runtime.get('pid') == data.get('pid') and runtime.get('state') not in ('offline', 'stopped', 'stopping')
                        and time.time() - float(data.get('updated_at', 0)) < 15)
        return data
    except (OSError, ValueError, TypeError):
        return {'available': False, 'live': False, 'people': [], 'memories': [],
                'note': 'No memory snapshot. Enable the v10 debug monitor and use the same monitor directory.'}
