# Memory retrieval

Exact-query caching reuses previous lookup results. The prepared-document cache
scores existing memory vectors against the actual question before reuse; normal
Chroma retrieval is the fallback. Caches expire after five minutes and are bounded.

Local transcript topic cues prepare up to eight records using Chroma tag filters.
New cues are merged within an utterance, with at least five seconds between
background searches. Foreground searches have priority over queued preparation.
There are no Gemini prediction requests. Shutdown summaries return JSON facts
and topic tags; only fact text is embedded. The transcript is saved first.

## Configuration

Set `[memory]` in `device.toml` (see `deploy/device.example.toml`):

- `patient_id`: required opaque ID; empty disables personal-memory reads/writes.
- `cache_enabled`: exact cache, default false.
- `prefetch_enabled`: local topic preparation, default true.
- `prefetch_interval_seconds`: default/minimum 5.
- `prepared_cache_enabled`: prepared-result serving, default false.
- `calibration_path`: reviewed, passing calibration artifact required for serving.
- `topic_aliases`: optional word/phrase to canonical-topic mapping.

Stores use `$BAYMAX_DATA_DIR/patients/<id>/{chroma_store,transcripts}`. Restart to
switch patients. Existing shared data is not migrated; untagged records in the
selected patient's store remain available to normal retrieval.

Prepared serving requires three qualifying candidates and an artifact matching
MiniLM's fingerprint and retrieval identity (`preparation: topic-tags-v1`), with
`backend: minilm-chroma`, development/heldout passes and a cosine threshold in
0.30–0.95. Accuracy must be checked on representative cases before enablement.

## Checks

With the existing runtime dependencies and pytest installed, run from `Baymax-main`:

```sh
python -m pytest memory_tests -q
```

The GitHub workflow runs the same focused suite. Devices and Gemini are simulated;
tag filtering also uses real disposable Chroma stores. No live conversation
speedup is established. Live summary saving (including the existing model 404)
and loaded robot performance still require validation.
