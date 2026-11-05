"""Offline labeled-conversation diagnostic; never reads the private memory space."""
import argparse
import json
import os
import re
from pathlib import Path
import tempfile
import time

from .._01_short_term.recent import RecentMemory
from .._03_long_term.graph import PalaceGraph
from .._04_consolidation.claims import claim_value
from .._04_consolidation.sweeper import MemorySweeper, read_sources
from .._05_recall.retrieval import GraphIndex


def claim_key(claim):
    return (claim['kind'], claim_value(claim['value']), bool(claim.get('negative')))


def score_claims(expected, actual):
    expected = {claim_key(c) for c in expected}
    actual = {claim_key(c) for c in actual}
    return {'true_positive':len(expected & actual), 'false_positive':len(actual - expected),
            'false_negative':len(expected - actual), 'passed':expected == actual}


def session_turns(turns):
    sessions = {}
    for row in turns:
        session = row.get('session', 1)
        if sessions and session not in sessions and session <= next(reversed(sessions)):
            raise ValueError('Sessions must be in chronological order')
        if session in sessions and session != next(reversed(sessions)):
            raise ValueError('Session turns must be contiguous')
        sessions.setdefault(session, []).append(row)
    return list(sessions.items())


def score_queries(graph, queries):
    snapshot = graph.snapshot()
    scored = []
    for query in queries:
        found = graph.index.search(graph, query['text'], query['owner_id'])
        actual = {(c['kind'],claim_value(c['value'])) for c in found}
        expected = {(c['kind'],claim_value(c['value'])) for c in query['expected']}
        forbidden = {claim_value(value) for value in query['forbidden_values']}
        bad = [c['value'] for c in found if claim_value(c['value']) in forbidden]
        owner_leak = any(next(c['owner_id'] for c in snapshot['claims'] if c['id']==r['id'])
                         != query['owner_id'] for r in found)
        scored.append({**query, 'actual':[{'kind':c['kind'],'value':c['value']} for c in found],
                       'forbidden_returned':bad, 'owner_leak':owner_leak,
                       'passed':expected.issubset(actual) and not bad and not owner_leak
                                and (not query.get('expect_empty') or not found)})
    return scored


def evaluate(fixture, embedder):
    if fixture.get('synthetic') is not True:
        raise ValueError('Only explicitly synthetic fixtures are accepted')
    turns = fixture['turns']
    if len({row['id'] for row in turns}) != len(turns):
        raise ValueError('Fixture turn IDs must be unique')
    if not turns or len(turns) > RecentMemory.capacity:
        raise ValueError('This diagnostic requires 1..128 synthetic turns')
    # Labels never enter extraction, embedding, consolidation or retrieval.
    sessions = session_turns(turns)
    if any(q.get('session', sessions[-1][0]) not in {s for s,_ in sessions} for q in fixture['queries']):
        raise ValueError('Recall query refers to an unknown session')
    now = 0
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='ember-labeled-conversation-') as folder:
        graph = None
        session_results = []
        queries = []
        for session, rows in sessions:
            session_end = max(row['t'] for row in rows) + 1
            if session_end <= now:
                raise ValueError('Session end times must increase')
            now = session_end
            previous_ids = {c['id'] for c in graph.snapshot()['claims']} if graph is not None else set()
            expired = not graph.recent.sources(now) if graph is not None else True
            # Reopen the same SQLite store; reset only the in-process short-term buffer.
            graph = PalaceGraph(Path(folder)/'memory.sqlite3', clock=lambda:now)
            graph.index = GraphIndex(embedder)
            graph.recent = RecentMemory(embedder)
            restored_ids = {c['id'] for c in graph.snapshot()['claims']}
            for row in rows:
                graph.ingest([{'speaker':row['speaker'], 'text':row['text'], 't':row['t'],
                               'speaker_id':row['owner_id']}])
                graph.step()
            for _ in range((len(rows)+7)//8 + 1):
                graph.index.update(graph)
                if not graph.recent.update(graph):
                    break
            sweeper = MemorySweeper(graph, lambda:read_sources(embedder, graph), clock=lambda:now)
            sweeper.step()
            session_queries = score_queries(graph, [q for q in fixture['queries']
                                            if q.get('session', sessions[-1][0]) == session])
            queries.extend(session_queries)
            session_results.append({'session':session,'turns':len(rows),'claims_restored':len(restored_ids),
                                    'claims_after':graph.snapshot()['total'],
                                    'recent_count':graph.recent.snapshot(now)['count'],
                                    'previous_cache_expired':expired,'persistence_passed':previous_ids==restored_ids,
                                    'recall_passed':sum(q['passed'] for q in session_queries),
                                    'recall_total':len(session_queries)})
        snapshot = graph.snapshot()
        per_turn = []
        source_ids = {}
        with graph.db() as db:
            for row in turns:
                source = db.execute('SELECT id FROM jobs WHERE t=? AND text=? AND owner_id IS ?',
                                    (row['t'],row['text'],row['owner_id'])).fetchone()
                sid = source['id'] if source else None
                source_ids[row['id']] = sid
                actual = [{'kind':c['kind'],'value':c['value'],'negative':'negative' in c['status']}
                          for c in db.execute('SELECT c.* FROM claims c JOIN evidence e ON c.id=e.claim_id '
                                              'WHERE e.source_id=?', (sid,))]
                per_turn.append({'id':row['id'], 'text':row['text'], 'expected':row['expected_claims'],
                                 'actual':actual, 'replay':bool(row.get('replay_of')),
                                 **score_claims(row['expected_claims'], actual)})
            provenance = []
            groups = {}
            for row in turns:
                for claim in row['expected_claims']:
                    groups.setdefault((row['owner_id'], *claim_key(claim)), set()).add(source_ids[row['id']])
            for (owner,kind,value,negative), expected_sources in groups.items():
                matches = [c for c in snapshot['claims'] if c['owner_id']==owner
                           and claim_key({**c,'negative':'negative' in c['status']})==(kind,value,negative)]
                actual_sources = {e['source_id'] for c in matches for e in db.execute(
                    'SELECT source_id FROM evidence WHERE claim_id=?', (c['id'],))}
                provenance.append({'owner_id':owner,'kind':kind,'value':value,'negative':negative,
                                   'expected_sources':len(expected_sources),'actual_sources':len(actual_sources),
                                   'passed':len(matches)==1 and None not in expected_sources
                                            and actual_sources==expected_sources})
            metadata_errors = []
            for c in snapshot['claims']:
                evidence = list(db.execute('SELECT j.*,i.data FROM evidence e JOIN jobs j ON j.id=e.source_id '
                                           'LEFT JOIN interaction_events i ON i.id=j.id WHERE e.claim_id=?', (c['id'],)))
                if (not evidence or any(e['owner_id']!=c['owner_id'] or e['data'] is None for e in evidence)
                    or c['first_seen']!=min(e['t'] for e in evidence)
                    or c['last_seen']!=max(e['t'] for e in evidence)):
                    metadata_errors.append(c['id'])
        recent = graph.recent.snapshot(now)
        graph.forget()
        graph.ingest([{'speaker':r['speaker'],'text':r['text'],'t':r['t'],'speaker_id':r['owner_id']} for r in turns])
        graph.index.update(graph)
        forgotten = graph.snapshot()
        forgetting_passed = (forgotten['total']==0 and forgotten['short_term']['count']==0
                             and not graph.index.search(graph, 'piano', 'synthetic:alex'))
        with graph.db() as db:
            forgetting_passed = forgetting_passed and all(db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
                for table in ('jobs','evidence','evidence_quotes','interaction_events','sweep_results'))
    scored = [row for row in per_turn if not row['replay']]
    totals = {key:sum(row[key] for row in scored) for key in ('true_positive','false_positive','false_negative')}
    tp,fp,fn = (totals[key] for key in ('true_positive','false_positive','false_negative'))
    return {'synthetic':True, 'model':'all-MiniLM-L6-v2 / ONNX CPU', 'turn_count':len(turns),
            'validation_passed':all(row['passed'] for row in scored) and all(g['passed'] for g in provenance)
                                and not metadata_errors and all(q['passed'] for q in queries) and forgetting_passed
                                and all(s['persistence_passed'] and s['previous_cache_expired'] for s in session_results),
            'extraction':{**totals, 'precision':tp/(tp+fp) if tp+fp else None,
                          'recall':tp/(tp+fn) if tp+fn else None},
            'turns':per_turn, 'claims':snapshot['claims'], 'provenance_groups':provenance,
            'sweep':snapshot['sweep'],
            'metadata_errors':metadata_errors, 'queries':queries,
            'sessions':session_results,
            'recent_count':recent['count'], 'recent_hint_count':len(recent['sweep'].get('links',[])),
            'forgetting_passed':forgetting_passed, 'evaluation_ms':round((time.perf_counter()-started)*1000,1),
            'note':'One synthetic diagnostic, not a general accuracy or live audio benchmark. Labels are used only '
                   'for scoring, never extraction. Missing negative/occupation/paraphrase claims remain failures; '
                   'no cloud enrichment is simulated or called. Replay rows are excluded from extraction totals.'}


def report(result):
    lines = ['# Synthetic conversation validation', '', result['note'], '',
             f"- Overall labeled validation: {'PASS' if result['validation_passed'] else 'FAIL'}",
             f"- Turns: {result['turn_count']}", f"- Extraction: {result['extraction']}",
             f"- Sessions: {len(result['sessions'])}; store/cache recreation is simulated, model process is reused",
             f"- Provenance groups: {sum(g['passed'] for g in result['provenance_groups'])}/{len(result['provenance_groups'])}",
             f"- Metadata errors: {len(result['metadata_errors'])}",
             f"- Recall cases: {sum(q['passed'] for q in result['queries'])}/{len(result['queries'])}",
             f"- Forgetting/replay protection: {result['forgetting_passed']}",
             f"- Short-term entries/hints: {result['recent_count']}/{result['recent_hint_count']}",
             f"- Automatic hierarchy groups: {len(result['sweep'].get('clusters',[]))}",
             f"- Local evaluation: {result['evaluation_ms']} ms (excludes model loading)", '',
             '## Session continuity', '']
    for session in result['sessions']:
        lines.append(f"- Session {session['session']}: restored {session['claims_restored']} claims; "
                     f"{session['claims_after']} after conversation; {session['recent_count']} recent turns; "
                     f"previous cache expired={session['previous_cache_expired']}; "
                     f"recall {session['recall_passed']}/{session['recall_total']}")
    lines += ['', '## Extraction misses', '']
    for row in result['turns']:
        if not row['passed'] and not row['replay']:
            lines += [f"- {row['id']}: {row['text']}", f"  - Expected: {row['expected']}; actual: {row['actual']}"]
    lines += ['', '## Recall cases', '']
    for query in result['queries']:
        lines += [f"- {'PASS' if query['passed'] else 'FAIL'} {query['id']}: {query['text']}",
                  f"  - Returned: {query['actual']}; forbidden: {query['forbidden_returned']}"]
    return '\n'.join(lines)+'\n'


def conversation_text(fixture):
    lines = ['# Invented conversation and held-out memory labels', '', fixture['description'], '']
    dates = {s['id']:s['date'] for s in fixture.get('sessions', [])}
    for session, turns in session_turns(fixture['turns']):
        lines += [f"## Session {session} — {dates.get(session, 'synthetic observation times')}", '']
        for row in turns:
            lines += [f"**{row['id']} · {row['speaker']} · {row['owner_id'] or 'unattributed'}**", '', row['text'], '']
            expected = '; '.join(f"{c['kind']}: {c['value']} ({'negative' if c.get('negative') else 'positive'})"
                                 for c in row['expected_claims']) or 'No durable claim'
            lines += [f"- Expected: {expected}", f"- Observed timestamp: {row['t']}"]
            if row.get('label'):
                lines.append('- Label: '+row['label'])
            lines.append('')
    return '\n'.join(lines)


def debugger_html(result, page):
    """Use the actual inspector UI with an isolated, immutable synthetic snapshot."""
    if result.get('synthetic') is not True:
        raise ValueError('Synthetic previews only')
    people = [{'id':owner,'name':owner.split(':')[-1].title()+' (synthetic)',
               'relationship':'Invented validation profile'}
              for owner in sorted({c['owner_id'] for c in result['claims'] if c['owner_id']})]
    snapshot = {'available':True,'live':False,'people':people,'memories':[],'visible':[],
                'space':'SYNTHETIC five-day validation — separate from active memory',
                'profile_name':'Synthetic fixture only','memory_context':{'person_id':None,'name':None},
                'note':'Actual extracted test memories, including mistakes. Groups use existing ONNX vectors, '
                       'not topic keywords or ground-truth labels. Representative text is not a category fact.',
                'graph':{'claims':result['claims'],'total':len(result['claims']),'queue':{},
                         'encoder':'Local ONNX synthetic validation','state':'completed; see validation report',
                         'enrichment_pending':0,'enrichment_status':'Gemini not called',
                         'sweep':result['sweep'],'behavior':None}}
    page = re.sub(r'<title>.*?</title>', '<title>Synthetic test · Memory Palace</title>', page, count=1)
    page = page.replace('<body>', '<body><aside role="note" style="padding:16px;background:#3b351b;color:#fff">'
                        '<strong>SYNTHETIC TEST MEMORIES — five simulated days</strong><br>'
                        'Read-only snapshot; active runtime memory is unchanged. '
                        '<a href="conversation.md">Conversation and labels</a> · '
                        '<a href="report.md">Validation report</a></aside>', 1)
    payload = json.dumps(snapshot, ensure_ascii=True).replace('<', '\\u003c')
    update = "function update(){data="+payload+"; $('status').textContent='Synthetic test snapshot — not live'; " \
             "$('space').textContent=data.space; $('note').textContent=data.note; $('error').hidden=true; render(); " \
             "for(const id of ['confirmSpeaker','testProfile','clearSpeaker','profileId','profileName'])$(id).disabled=true;}"
    page, count = re.subn(r'^async function update\(\).*$', lambda _:update, page, flags=re.M)
    if count != 1:
        raise ValueError('Inspector update entry point changed')
    page, count = re.subn(r'async function setProfile\([^)]*\)\{[\s\S]*?\n\}',
                          "async function setProfile(){$('speakerMessage').textContent='Synthetic preview is read-only.';}", page)
    if count != 1 or 'fetch(' in page:
        raise ValueError('Preview must not access runtime endpoints')
    page = page.replace('href="/monitor"', 'href="http://127.0.0.1:8766/monitor"')
    page = page.replace('>Conversation monitor</a>', '>Active runtime monitor (separate)</a>')
    page = page.replace('No matching saved memories. Try another person or clear the filters.',
                        'This test uses the active graph above; no legacy Chroma summaries were exported.')
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[3]
    parser.add_argument('--fixture', type=Path, default=root/'tests/fixtures/memory_five_day_conversation.json')
    parser.add_argument('--output', type=Path, default=root/'.local-review/five-day-conversation')
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding='utf-8'))
    if fixture.get('synthetic') is not True:
        raise ValueError('Only explicitly synthetic fixtures are accepted')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['ANONYMIZED_TELEMETRY'] = 'False'
    print('Loading existing local ONNX model; synthetic data only, no provider calls.', flush=True)
    started = time.perf_counter()
    from semantic_embedder import SemanticEmbedder
    import chromadb
    from chromadb.config import Settings
    embedder = SemanticEmbedder(model_dir=root/'all-MiniLM-L6-v2-onnx', chroma_dir=None, verbose=False)
    embedder._chroma_client = chromadb.Client(Settings(anonymized_telemetry=False, is_persistent=False))
    embedder._collection = embedder._chroma_client.get_or_create_collection(
        name='synthetic_conversation_legacy', embedding_function=None)
    load_seconds = round(time.perf_counter()-started, 2)
    print(f'Model ready after {load_seconds}s; evaluating labeled conversation.', flush=True)
    result = evaluate(fixture, embedder)
    result['load_seconds_including_imports'] = load_seconds
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (args.output/'report.md').write_text(report(result), encoding='utf-8')
    (args.output/'conversation.md').write_text(conversation_text(fixture), encoding='utf-8')
    (args.output/'debugger.html').write_text(debugger_html(result, (root/'static/memory-palace.html').read_text(encoding='utf-8')),
                                           encoding='utf-8')
    print(report(result), flush=True)
    print(f"Saved synthetic report: {args.output/'report.md'}", flush=True)
    if not result['validation_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
