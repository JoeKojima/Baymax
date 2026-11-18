# Memory retrieval

Local topics trigger tag-filtered Chroma preparation at most once every five
seconds. Exact caching skips repeat embedding/search; prepared candidates are
scored against the actual question, with normal retrieval fallback.
Shutdown summaries store fact text and topic tags; transcripts are saved first.

Set `[memory].patient_id` in `deploy/device.example.toml`; empty disables personal
memory. Restart to switch patients. Data lives under
`$BAYMAX_DATA_DIR/patients/<id>/{chroma_store,transcripts}`. Existing untagged
memories remain searchable; legacy shared data is not migrated.

- `cache_enabled` and `prepared_cache_enabled`: serving switches, default false.
- `prefetch_enabled`: topic preparation, default true.
- `prefetch_interval_seconds`: default/minimum 5.
- `topic_aliases`: optional word/phrase to canonical-topic mapping.
- `calibration_path`: reviewed artifact required for prepared serving; binds model
  fingerprint, `topic-tags-v2` policy, backend, development/heldout passes and a
  0.30–0.95 cosine threshold. Three candidates must qualify.

Caches expire after five minutes and cap at 128 queries, 256 documents and 8 MiB.
Foreground work has priority; sessions, summaries and interruptions discard stale
work. The existing bounded profiler reports retrieval/queue/preparation timing
and cache-source counts. These metrics do not establish conversational speedup.

Run `python -m pytest memory_tests -q`; GitHub runs the same suite with simulated
providers and disposable real Chroma stores. Live summary saving (existing model
404) and robot performance remain unverified.
