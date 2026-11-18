# Topic-based memory preparation: validation

Date: November 17, 2026. Branch: `memory-retrieval`.

## Implementation

Local canonical topic cues now replace Gemini search-phrase prediction.
One MiniLM embedding and tag-filtered Chroma query prepares up to eight
candidates, with at least five seconds between background operation starts.
Preparation defaults on for active patient memory. Prepared serving remains
opt-in and requires a new `topic-tags-v1` calibration artifact. Summary responses
now contain facts and tags; only fact text is embedded. Raw transcripts are
saved before summarization. Existing memories are not migrated.

No dependencies or production modules were added. The three central files
(`ember_memory.py`, `ember_topics.py`, `realtime_gemini_10.py`) have a combined
net reduction of 66 lines relative to the starting branch. Configuration and
Setup wiring reuse existing controls; obsolete prediction code is removed.

## Automated checks

`python check_release.py --report .local-review/topic-tags/offline-checks.md`:

- Core: 72 passed.
- Toolkit: 18 passed.
- Memory: 57 passed.
- No failures, errors or skipped tests (147 checks total).

Tests cover real Chroma 1.5.8 array filters, multiple-topic OR filters, fewer
than eight results, tagged/untagged/mixed/empty stores and patient separation.
Scheduler checks cover split words, merged/deduplicated cues, the five-second
limit, reconnect budgets, foreground priority, stale results, interruption and
revision invalidation. The real runtime startup is exercised with provider and
device substitutes and asserts zero preparation-related Gemini generation calls.
Shutdown checks cover structured tags, invalid responses, empty summaries and
preserving transcripts when the provider fails.

The first full-suite attempt could not bind temporary localhost test servers
under the restricted execution environment. Rerunning with local-server access
passed the entire suite; no tests were skipped or weakened.

## Real MiniLM/Chroma replay

Used the existing local ONNX model and pinned Chroma 1.5.8, with isolated
fictional stores (12 memories per patient, two patients). No Gemini calls,
recordings, microphone capture or real patient databases were used.

Calibration used distinct development and heldout conversations, with detected
topics rather than injected predictions. Both splits passed the existing
software quality criteria. The selected threshold was 0.30; heldout had 48 cases,
40 cache-served cases, zero wrong-patient results and zero recall loss versus
its baseline. This is a fictional-corpus result, not representative validation.

Benchmark: 200 turns per mode, three repetitions (2,400 foreground lookups),
identical workload and calibration. Table times are medians across the three
runs; CPU seconds cover each run. Recall is the mean; wrong-patient counts are
summed. Arrival gaps use the accelerated virtual clock, with eligible background
work completed between turns. These are not live response-time measurements.

| Mode | Foreground p50 (ms) | Foreground p95 (ms) | CPU seconds / 200 turns | Recall | Wrong-patient results |
| --- | ---: | ---: | ---: | ---: | ---: |
| off | 5.421 | 7.567 | 7.194 | 92.00% | 0 |
| instrumentation | 6.274 | 12.312 | 9.554 | 92.00% | 0 |
| reactive | 5.277 | 10.757 | 7.681 | 93.33% | 0 |
| topic | 5.584 | 13.047 | 11.311 | 94.00% | 0 |

Each topic-mode run completed 167 preparations and had 34 Chroma, 34 exact-cache
and 132 prepared-cache foreground results. Reactive mode had the same source
counts. Median local preparation duration across the topic runs was 3.510 ms.

**Topic preparation did not improve latency on this small workload.** It added
CPU work and increased foreground p95 compared with reactive caching, while
slightly improving recall. The incremental preparation latency gate failed in
all three repetitions. Instrumentation overhead also exceeded the existing
+2 ms p95 gate. Neither runtime deployment nor a conversational speedup is
established. The requested preparation-on default is implemented, with an
explicit off switch for comparisons; these measurements do not enable serving.

## Reproduce

From `Baymax-main`, with the pinned memory dependencies and local model:

```sh
python tools/memory_evaluate.py calibrate --backend minilm-chroma \
  --model-dir all-MiniLM-L6-v2-onnx --output benchmark-output/topic-calibration.json
python tools/memory_evaluate.py benchmark --backend minilm-chroma \
  --model-dir all-MiniLM-L6-v2-onnx --calibration benchmark-output/topic-calibration.json \
  --turns 200 --repetitions 3 --output benchmark-output/topic-replay.json
```

Retained local evidence is under `.local-review/topic-tags/`: `calibration.json`,
`real-replay.json`, `fake-replay.json`, `traces/` and `offline-checks.md`.
These generated artifacts are ignored by Git; this document records the results.

## GitHub checks

`.github/workflows/checks.yml` runs the same offline gate on Ubuntu with Python
3.12 for pushes and pull requests and uploads the report. Its test-only package
installation uses the existing runtime lock as constraints. CI needs no provider
keys or downloaded ONNX model; real MiniLM replay remains a separate local check.

## Outstanding

- Live tagged-summary save/restart with the configured provider. The recorded
  summary-model 404 is a separate issue and no model/API switch was made here.
- Representative development/heldout data and calibration before serving from
  prepared candidates. Old prediction-era artifacts are rejected.
- Live transcript arrival, patient wait, interruption and audio-underrun tests.
- Loaded robot CPU/RSS, camera/pose/dashboard contention and stability checks.
