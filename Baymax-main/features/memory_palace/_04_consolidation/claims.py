"""Conservative claim consolidation; source records remain authoritative."""
import hashlib
import json


def claim_value(value):
    return ' '.join(value.split()).casefold()


def save_claim(db, job, kind, value, quote, negative=False):
    source = db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    owner = job['owner_id'] if 'owner_id' in job.keys() else None
    if source is None or source['owner_id'] != owner or quote not in source['text']:
        raise ValueError('Missing or mismatched claim source')
    status = ('self-reported negative' if negative else 'self-reported') if owner else (
        'unattributed negative proposal' if negative else 'unattributed proposal')
    normalized = claim_value(value)
    matches = [row for row in db.execute(
        'SELECT * FROM claims WHERE owner_id IS ? AND kind=? AND status=? ORDER BY first_seen,id',
        (owner, kind, status)) if claim_value(row['value']) == normalized]
    key = json.dumps([owner, kind, normalized, bool(negative)])
    cid = matches[0]['id'] if matches else hashlib.sha256(key.encode()).hexdigest()[:24]
    db.execute('INSERT OR IGNORE INTO claims(id,kind,value,status,first_seen,last_seen,owner_id) '
               'VALUES (?,?,?,?,?,?,?)', (cid, kind, value, status, source['t'], source['t'], owner))
    for match in matches:
        db.execute('INSERT OR IGNORE INTO evidence_quotes SELECT claim_id,source_id,quote FROM evidence WHERE claim_id=?',
                   (match['id'],))
    # Move links only. Never replace original quotes, timestamps or interaction records.
    for duplicate in matches[1:]:
        db.execute('INSERT OR IGNORE INTO evidence SELECT ?,source_id,quote FROM evidence WHERE claim_id=?',
                   (cid, duplicate['id']))
        db.execute('INSERT OR IGNORE INTO evidence_quotes SELECT ?,source_id,quote FROM evidence_quotes WHERE claim_id=?',
                   (cid, duplicate['id']))
        db.execute('DELETE FROM evidence_quotes WHERE claim_id=?', (duplicate['id'],))
        db.execute('DELETE FROM evidence WHERE claim_id=?', (duplicate['id'],))
        db.execute('DELETE FROM claims WHERE id=?', (duplicate['id'],))
        # graph_indexed retains obsolete IDs until the local index removes their vectors.
    db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?)', (cid, source['id'], quote))
    db.execute('INSERT OR IGNORE INTO evidence_quotes VALUES (?,?,?)', (cid, source['id'], quote))
    db.execute('UPDATE claims SET first_seen=(SELECT min(j.t) FROM evidence e JOIN jobs j '
               'ON j.id=e.source_id WHERE e.claim_id=?), last_seen=(SELECT max(j.t) FROM evidence e '
               'JOIN jobs j ON j.id=e.source_id WHERE e.claim_id=?) WHERE id=?', (cid, cid, cid))
    db.execute("UPDATE meta SET value=value+1 WHERE key='claims_revision'")
    db.execute('DELETE FROM sweep_results')
    return cid
