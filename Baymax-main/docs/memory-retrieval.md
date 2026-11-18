# Memory retrieval

Exact-query caching skips repeat embedding/database work. Prepared memory
vectors are scored against the actual question, with normal Chroma fallback.
Local topics trigger tag-filtered preparation at most once every five seconds.
Shutdown summaries save fact text with topic tags; transcripts are saved first.

In `[memory]` (`deploy/device.example.toml`), set an opaque `patient_id`.
Empty disables personal-memory reads/writes. Restart to switch patients.
Stores live under `$BAYMAX_DATA_DIR/patients/<id>/{chroma_store,transcripts}`;
legacy shared data is not migrated, and normal retrieval includes untagged facts.

- `cache_enabled`: exact-cache serving, default false.
- `prefetch_enabled`: local topic preparation, default true.
- `prefetch_interval_seconds`: default/minimum 5.
- `prepared_cache_enabled`: prepared-cache serving, default false.
- `calibration_path`: reviewed calibration artifact required for prepared serving.
- `topic_aliases`: optional word/phrase to canonical-topic mapping.

Limits are fixed: five-minute TTL, 128 exact queries, 256 prepared documents and
8 MiB total cached payload. Queued foreground work takes priority. Session changes,
new summaries and interruptions discard obsolete work. Calibration binds the model
fingerprint, `topic-tags-v2` cache policy, real backend, development/heldout
passes and a cosine threshold of 0.30–0.95; three candidates must qualify.

The existing bounded profiler reports retrieval p50/p95 including queue time,
per-source cache counts, and preparation count/duration, including work that
finishes after a timeout. Metrics contain fixed labels and numbers; local memory
timing does not measure patient response time.

Run `python -m pytest memory_tests -q` from `Baymax-main` with the existing runtime
dependencies and pytest. GitHub runs the same focused suite using simulated
providers/devices and real disposable Chroma stores. Live summary saving (existing
model 404) and robot performance remain unverified.
