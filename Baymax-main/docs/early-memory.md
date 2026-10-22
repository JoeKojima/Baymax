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

All new switches default to `false`: instrumentation, exact-query caching,
prepared-cache serving, comparison mode, and prediction. They are independent.
No feature is automatically enabled by a benchmark. No HTTP endpoints changed.

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
| `ember_memory.py` | Import-safe service, patient paths, worker, caches, prediction scheduling and stale-result guards |
| `ember_topics.py` | Reviewable seeded terms, bounded learned terms, aliases, raw-fragment assembly and context snapshot |
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
queue cost is measured. Initiation/profile reads use the same worker. Foreground
retrieval never waits for prediction or for queued preparation to finish.

## Early preparation

Patient text is observed as raw transcription deltas, including whitespace-only
deltas. Complete words/phrases trigger immediately; a terminal word needs 200 ms
without changes. Seeded family/hobby/visit/appointment terms, optional aliases and
up to 4,096 content terms learned from already-loaded/retrieved memories are
matched locally. Filler words and incomplete prefixes do not trigger prediction.

The newest eligible snapshot contains the current partial utterance plus at most
five preceding speaker messages, capped at 2,000 characters, with oldest context
dropped first. Surrounding words and negation are retained. Prediction starts as
soon as a cue and the rate limit permit, without waiting for an ending sentence,
patient turn or robot reply. One newest pending snapshot replaces older pending
work. Later fragments do not cancel an in-flight request.

A separate async Gemini request uses `SUMMARY_MODEL`, predicts two phrases,
times out after three seconds, and permits one attempt per 30 seconds without an
immediate retry. Each phrase retrieves at most eight existing memories. Searches
run sequentially and yield worker priority to foreground demand between jobs.
Predictions are never stored as facts or sent directly into Gemini Live. Topic
count (2/3/5), aliases and interval are configurable; the interval cannot be
shorter than 30 seconds. These limits are resource budgets, not proven optima.

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
| `prediction.*`, `preparation.*` | Cue, throttle delay, request timing, local search, head start, ready-before-lookup and unused work |
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

Tests use fake clocks, Gemini, storage and audio. The real receiver, output
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
text; disjoint development/heldout conversations contain timestamped raw
fragments, expected memory IDs and recorded prediction phrases. The fixture is
an initial software exercise, not evidence that the workload represents seniors.

Calibration returns ordinary Chroma results, captures prepared candidates, and
sweeps best-match cosine thresholds 0.30 through 0.95 in 0.05 increments. It picks
the lowest passing development threshold, then checks that one threshold on
heldout conversations. It does not retune on heldout. Each split requires at
least 20 cases and three served candidates to avoid an empty, vacuous pass.
No wrong-person result is allowed; recall loss versus uncached retrieval must
be at most two percentage points. The artifact binds the real backend, model
fingerprint and retrieval settings, with both split gates passing. Missing,
invalid, mismatched or failing artifacts keep prepared serving disabled. Fake
embeddings cannot produce an artifact. Set `memory.calibration_path` only after
reviewing calibration on the representative workload.

## Replay and robot rollout

Software smoke replay (no models, Gemini calls or devices):

```
python tools/memory_evaluate.py benchmark --backend fake \
  --turns 200 --repetitions 3 --topics 2 3 5 --output benchmark-output/smoke.json
```

Actual local retrieval replay, retaining transcript-fragment arrival gaps:

```
python tools/memory_evaluate.py benchmark --backend minilm-chroma --realtime \
  --dataset approved-replay.json --model-dir all-MiniLM-L6-v2-onnx \
  --calibration benchmark-output/calibration.json --turns 200 --repetitions 3 \
  --topics 2 3 5 --output benchmark-output/retrieval.json
```

Four modes are compared: instrumentation off, instrumentation only, reactive
caching, and caching with prediction (2/3/5 topics). Prediction in offline replay
uses the dataset's recorded phrases, so its timing does **not** establish Gemini
request latency or prediction quality. Accelerated replay advances logical gaps
while retaining real lookup timings; only `--realtime` preserves wall-clock gaps.
Late/timeout replay counts use a scripted reply budget, not observed Live audio.
The report deliberately leaves deployment gates unpassed.

On the LattePanda, measure the **live** configurations with camera, pose,
recording and dashboard enabled: 200 turns each, three repetitions, followed by
a two-hour run using the candidate configuration. Keep the same scripted audio,
network and corpus when comparing runs. Use local traces for patient wait,
interruptions and underruns; monitor current RSS/CPU over time using the robot's
process monitor. Replay's process peak RSS is not steady-state added RSS.

Enable in stages: instrumentation → exact cache → prepared comparison → early
prediction → prepared serving, retaining only components whose evidence passes:

| Gate | Required result |
| --- | --- |
| Instrumentation overhead | At most +2 ms in local retrieval p95 |
| Caching | At least 10% improvement in full retrieval p95 |
| Correctness | No extra wrong-person results; recall loss at most 2 percentage points |
| Patient wait/audio | p95 increase at most 20 ms; no additional audio underruns |
| Footprint/stability | Added steady RSS below 64 MiB, no continuing growth over two hours |
| Prediction | Better full retrieval or late/timeout rates than reactive caching; hit count alone is insufficient |

If background contention worsens timing, transcripts arrive too late, or Chroma
cost is negligible, leave the ineffective feature off. The changes provide the
mechanism and measurements; they do not claim a measured robot improvement.
