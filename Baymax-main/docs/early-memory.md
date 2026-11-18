# Early memory preparation and latency measurement

Implemented against GitHub `main` at `5b3b850325726711b92c2d6ad677ef6ca5f32101`.
The implementation targets the deployed `realtime_gemini_10.py` entry point.

## Behaviour and defaults

MiniLM, Chroma, and Gemini Live remain in place. The first usable transcript
fragment still triggers a query over the rolling last 30 words. Results remain
limited to three, with cosine distance at most 1.0. The receiver still waits up
to 150 ms for an unfinished search **after receiving reply content**, then sends
useful memories before queuing that received audio. Faster retrieval does not
guarantee memory reaches Gemini before generation. Sending memories cannot
change audio Gemini has already generated.

Topic preparation defaults on (`memory.prefetch_enabled = true`), with a
five-second minimum search interval (`memory.prefetch_interval_seconds = 5`).
It runs only with personal-memory retrieval active and the embedder ready.
Instrumentation, exact caching, prepared serving and comparison still default
false. Filling the cache does not authorize serving it; calibration is required.
No HTTP endpoints changed. Set `prefetch_enabled = false` for an uncached baseline.

The experimental `prediction_enabled`, `prediction_interval_seconds` and
`prediction_max_topics` settings are retired and ignored. Desktop Setup replaces
`memory_prediction` / `BAYMAX_MEMORY_PREDICTION` with
`memory_prefetch` / `BAYMAX_MEMORY_PREFETCH` (default on). An old saved prediction
switch does not disable local preparation; use the new switch explicitly.

Personal memory now requires `memory.patient_id`, an opaque identifier in
`/etc/ember/device.toml` (or `EMBER_DEVICE_CONFIG`). An empty ID disables personal
memory reads **and writes**, even when `retrieval_enabled = true`. Invalid IDs
fail configuration. A configured patient uses:

```
$BAYMAX_DATA_DIR/patients/<patient_id>/chroma_store/
$BAYMAX_DATA_DIR/patients/<patient_id>/transcripts/
```

Connection-time prompt memories, current-turn retrieval, initiation hints, and
handled-shutdown summary writes share that context. Change patients by stopping
the process and starting a fresh process/Live session. The legacy shared
`chroma_store/` is neither read nor migrated. Existing audio recording and voice
analysis are separate, unchanged subsystems; this is the agreed minimum memory
separation, not a migration of every robot data source.

There is no transcript journal, commit acknowledgement, new microphone gate,
or crash-recovery guarantee. Existing handled-Ctrl+C summary saving remains.

## Modules and control flow

| File | Responsibility |
| --- | --- |
| `ember_memory.py` | Import-safe service, patient paths, worker, caches, topic scheduling and stale-result guards |
| `ember_topics.py` | Shared topic vocabulary, aliases, raw-fragment matching and summary validation |
| `ember_timing.py` | Import-safe OTel setup, local exporter, callback marker handoff and conservative audio estimates |
| `semantic_embedder.py` | Existing `search()` plus `search_by_embedding()`; optional Chroma telemetry disabled |
| `tools/memory_evaluate.py` | Calibration and repeatable retrieval replay |
| `tools/timeline_report.py` | Summarize a robot process's rotating timeline |

`MemoryService.lookup(query, session_id, turn_id)` checks the exact-query cache,
then embeds once, compares actual-question cosine scores against cached document
vectors, and otherwise queries Chroma with that same vector. Results include
IDs, documents, distances, revision, source, and correlation IDs. Cache failures
fall back to Chroma. Chroma/model failures return an empty error result so voice
conversation can continue.

Exact results include empty searches. Exact hits skip embedding and Chroma.
Prepared hits skip Chroma but still embed the actual question. Prepared serving
requires three qualifying documents and a passing calibration; comparison mode
always returns normal Chroma results while retaining candidate scores for offline
assessment. It does not compare the question with a predicted phrase.

Cache limits are 128 exact queries, 256 documents and 8 MiB of stored payload
(including vector bytes and conservative record/key overhead). Entries expire
300 seconds after fetching from Chroma, regardless of reads. Smaller limits may
be used in tests; production configuration cannot exceed these ceilings. Scope
includes patient, model fingerprint, revision and retrieval settings. Reconnect
clears caches; successful summary writes invalidate the revision. Late results
from an older session/revision are rejected. A model fingerprint hashes the local
ONNX/tokenizer/configuration files and the pooling implementation version.

One worker serializes local embedding/Chroma operations. Pending foreground jobs
win over pending background jobs. In-progress inference is not preempted; its
queue cost is measured. Initiation/profile reads use the same worker. Queued preparation never takes priority over a foreground lookup; an already
running embedding/search cannot be preempted.

## Early preparation

Patient text is observed as raw transcription deltas, including whitespace-only
boundaries. Complete words/phrases trigger immediately; a terminal word needs
200 ms without changes. Built-in family/hobby/visit/appointment categories and
configured aliases share one canonical vocabulary with the summarizer. Names
and arbitrary words learned from memory text no longer trigger preparation.
Normal semantic retrieval still handles questions outside this vocabulary.

A cue starts local preparation immediately if the rate budget permits. Further
search starts are at least five seconds apart, including across reconnects.
Pending cues within an utterance merge; a new utterance replaces older pending
work. Interruption, invalidation, reconnect and shutdown discard stale work.
No idle timer repeatedly searches, and duplicate topics within an utterance are
ignored. Foreground jobs retain priority in the existing worker.

Each preparation embeds the canonical topic names once with MiniLM and runs one
Chroma query for at most eight records, including their embeddings. The filter
is `{"tags": {"$contains": "family"}}` for one topic, or `$or` of those clauses
for multiple topics. Results fill the existing prepared-document cache. An empty
filtered search is not retried without filters. Foreground retrieval remains
unfiltered, so existing untagged memories stay usable. Preparation makes no
Gemini request and never stores inferred facts.

## Tagged shutdown summaries

The existing summary-model request returns a JSON array of
`{"summary": "Sarah visits on Sundays.", "tags": ["family", "visits"]}` objects.
The allowed tags come from existing categories and configured alias targets.
Summary text alone is embedded; normalized, deduplicated, allowed tags are stored
as a Chroma metadata array. Invalid/unknown tags are discarded; a valid fact
without tags is saved with the field omitted. `[]` means nothing to save.
Malformed responses fail without writing memories. No second model call is added.
The raw transcript is saved before the provider request, including when the
provider fails or returns no facts. Existing records are not retagged or migrated.
The previously observed summary-model 404 remains a separate configuration issue.

Preparation during speech is possible only after usable transcription arrives.
The [Gemini API reference](https://ai.google.dev/api/live#bidigeneratecontentservercontent)
does not guarantee input-transcription ordering. The document cache is inspired
by [VoiceAgentRAG](https://github.com/SalesforceAIResearch/VoiceAgentRAG/blob/main/voice_optimized_rag/core/slow_thinker.py);
the implementation uses NumPy and adds neither FAISS nor that package.

## Measurements and privacy

Enable `[observability] enabled = true`. The pinned OpenTelemetry 1.41.1 SDK uses
a bounded background span queue and local JSONL export, with no cloud collector.
`$BAYMAX_DATA_DIR/latency/timeline.jsonl` rotates at 10,000,000 bytes, with four
backups (five files total). An aggregate snapshot is written every 30 seconds and
at clean close. Counts cover all instrumented events; duration samples retain
the latest 2,048 observations per metric. Traces sample 10% of production turns;
`benchmark = true` traces every turn. Lost callback markers make affected timing
associations uncertain. Export overload can drop sampled spans, not block audio.

Only whitelisted enums, numeric measurements and opaque session/turn/lookup/
answer IDs enter traces. No patient IDs/names, memory IDs/text, transcript text
or audio is exported. OTel initialization and files are separate from hardware
initialization. Audio callbacks only capture device timing and enqueue bounded
small markers; they do not construct spans or write files.

| Measurement | Meaning and limitations |
| --- | --- |
| `retrieval.total` | Full worker queue + local lookup, including completed work after a receiver timeout |
| `retrieval.await` | Time the caller awaited retrieval; can end at cancellation |
| `retrieval.with_formatting` | Completed local lookup plus delivery formatting, recorded in the receiver |
| `retrieval.embedding`, `retrieval.chroma`, cache checks | Separate costs; exact hits have no embedding/Chroma measurement |
| `retrieval.worker_started.queue_ms` | Foreground queue delay, including background inference already running |
| `preparation.*` | Cue, local search time, worker queue delay, head start, ready-before-lookup and unused work |
| `retrieval.ready.profile_overlap` | Number of returned facts already supplied in the connection prompt |
| `reply.first_content`, `reply.first_audio_received` | Receipt before the 150 ms wait; generation started before these timestamps |
| `memory.send_*`, `memory.timeout`, `memory.empty` | Delivery events separate from retrieval/first reply receipt |
| `speech.server_notification` | Receipt of available top-level SDK VAD signals, processed even without server content |
| `speech.estimated_*` | Existing RMS threshold, finalized after 300 ms quiet and backdated to the last active frame |
| `playback.output_device_estimate` | PortAudio DAC estimate at the first nonzero output sample, **not measured speaker sound** |
| `patient_wait.estimated` | Estimated local speech end to output-device start; excludes missing/ambiguous/overlap/uncertain associations |
| `playback.buffer_flushed`, `playback.interrupted`, `audio.underrun` | Queued audio is not counted as played; audio handed to PortAudio may already have played |

The console's old latest-microphone-packet metric remains as
`legacy_first_audio_latency`. It is not patient wait. The existing microphone
skip during buffered robot playback remains; this release does not fix overlap.

```
python tools/timeline_report.py /data/latency --output latency-report.json
```

## Tests and calibration

From `Baymax-main`, install pytest separately from robot runtime dependencies:

```
python -m pip install pytest
python -m pytest -q
```

Tests use fake clocks, Gemini and audio, plus disposable pinned-Chroma stores for real tag filters. The real receiver, output
callback, initiation and shutdown-summary functions run with mocked devices.
They cover split fragments, early cues, late/missing activity, stale work,
priority, cache expiry/capacity/revision/isolation, deadline ordering, OTel
privacy/rotation/sampling, and flushed audio.

For real calibration, install the pinned robot dependencies and provide the
existing MiniLM directory. The evaluator creates disposable, isolated Chroma
stores from labelled fictional memories. It never uses the robot's patient DB:

```
python tools/memory_evaluate.py calibrate --backend minilm-chroma \
  --model-dir all-MiniLM-L6-v2-onnx --output benchmark-output/calibration.json
```

Use `--dataset approved-replay.json` to replace the built-in smoke fixture. Its
schema is shown in `tools/memory_fixture.py`: memories have IDs, patient IDs and
text and optional canonical `tags`; disjoint development/heldout conversations
contain timestamped raw fragments and expected memory IDs. Preparation uses only
topics detected in those fragments, never expected IDs or injected predictions. The fixture is
an initial software exercise, not evidence that the workload represents seniors.

Calibration returns ordinary Chroma results, captures prepared candidates, and
sweeps best-match cosine thresholds 0.30 through 0.95 in 0.05 increments. It picks
the lowest passing development threshold, then checks that one threshold on
heldout conversations. It does not retune on heldout. Each split requires at
least 20 cases and three served candidates to avoid an empty, vacuous pass.
No wrong-person result is allowed; recall loss versus uncached retrieval must
be at most two percentage points. The artifact binds the real backend, model fingerprint and retrieval settings
(including `preparation: topic-tags-v1`), with both split gates passing. Missing,
invalid, mismatched or failing artifacts keep prepared serving disabled. Old
prediction-era artifacts are rejected. Fake embeddings cannot produce an artifact. Set `memory.calibration_path` only after
reviewing calibration on the representative workload.

## Replay and robot rollout

Software smoke replay (no models, Gemini calls or devices):

```
python tools/memory_evaluate.py benchmark --backend fake \
  --turns 200 --repetitions 3 --output benchmark-output/smoke.json
```

Actual local retrieval replay, retaining transcript-fragment arrival gaps:

```
python tools/memory_evaluate.py benchmark --backend minilm-chroma --realtime \
  --dataset approved-replay.json --model-dir all-MiniLM-L6-v2-onnx \
  --calibration benchmark-output/calibration.json --turns 200 --repetitions 3 \
  --output benchmark-output/retrieval.json
```

Four modes are compared: instrumentation off, instrumentation only, reactive
caching, and caching with local topic preparation. Only topic mode enables
background preparation. Accelerated replay uses a virtual arrival clock and
isolates eligible work between turns; it is a software workload comparison, not
live audio performance. Use `--realtime` for real arrival gaps. Compare identical
corpora, queries and calibration settings; report foreground p50/p95, preparation
cost, cache use, recall and CPU time. Calibration isolates candidate ranking
from scheduler timing and is not a scheduling benchmark.

Enable serving in stages: instrumentation → exact cache → prepared comparison
→ calibrated prepared serving. Preparation itself defaults on and can be
disabled for comparisons. Retain only components whose evidence passes:

| Gate | Required result |
| --- | --- |
| Instrumentation overhead | At most +2 ms in local retrieval p95 |
| Caching | At least 10% improvement in full retrieval p95 |
| Correctness | No extra wrong-person results; recall loss at most 2 percentage points |
| Patient wait/audio | p95 increase at most 20 ms; no additional audio underruns |
| Footprint/stability | Added steady RSS below 64 MiB, no continuing growth over two hours |
| Topic preparation | Better full retrieval or late/timeout rates than reactive caching; hit count alone is insufficient |

If background contention worsens timing, transcripts arrive too late, or Chroma
cost is negligible, leave the ineffective feature off. The changes provide the
mechanism and measurements; they do not claim a measured robot improvement.
