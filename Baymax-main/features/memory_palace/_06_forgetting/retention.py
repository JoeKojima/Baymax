"""Explicit graph forgetting; preserve the cutoff and storage lock together."""

def forget_graph(graph):
    # Until verified ownership exists, clear this entire experimental graph.
    with graph.lock, graph.db() as db:
        db.execute("UPDATE meta SET value=? WHERE key='forget_before'", (graph.clock(),))
        db.execute("UPDATE meta SET value=value+1 WHERE key='claims_revision'")
        for table in ('evidence', 'evidence_quotes', 'claims', 'jobs', 'graph_indexed', 'sweep_results', 'interaction_events', 'behavior_events'):
            db.execute('DELETE FROM ' + table)
        if graph.index is not None:
            graph.index.clear()
        if graph.recent is not None:
            graph.recent.clear()



def prune_behavior(db, now):
    """Bound optional raw telemetry while leaving claim evidence untouched."""
    db.execute('DELETE FROM behavior_events WHERE t < ?', (now-30*86400,))
    db.execute('DELETE FROM behavior_events WHERE id IN (SELECT id FROM behavior_events ORDER BY t DESC,id DESC LIMIT -1 OFFSET 20000)')
