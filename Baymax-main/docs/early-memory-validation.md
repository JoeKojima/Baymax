# Implementation validation

Base: GitHub `main`, `5b3b850325726711b92c2d6ad677ef6ca5f32101`.
Working branch: `memory-retrieval`.

## Completed locally

- **133 tests passed** using Python 3.12: 72 core, 18 toolkit and 43 memory
  checks. Device/provider adapters are simulated; exporter tests use the
  actual pinned OpenTelemetry 1.41.1 SDK.
- Syntax compilation and `git diff --check` passed.
- **3,600 fictional replay turns** completed: 200 turns × three repetitions ×
  six configurations (off, instrumentation, reactive cache, prediction with
  two/three/five topics). The stand-in embedding/storage backend was used.
- The replay exercised 408 exact-query hits and returned zero wrong-person
  results in the fake stores. Prepared serving correctly stayed disabled in
  the absence of real MiniLM/Chroma calibration.
- The final local report and trace files are retained under
  `benchmark-output/verified/`; generated benchmark outputs are ignored by Git.
- Mac microphone/speaker conversation and the desktop fictional-memory check
  were exercised. The controlled check returned `chroma` followed by
  `exact_cache` for the same query.
- A separate real MiniLM/Chroma comparison repeated the same question 30
  times per condition after five warm-up searches, alternating uncached
  searches with warmed exact-cache hits. OpenTelemetry recorded every lookup.
  Median full retrieval was **1.939 ms uncached** versus **0.088 ms per exact
  hit** (1.85 ms saved); all returned memory IDs and distances matched. The
  cache's first fill was measured separately. A disposable database copy was
  used without microphone capture or Gemini requests.
- The latest live conversation recorded seven lookups and seven Chroma
  searches: zero exact hits despite caching being enabled. Its median full
  retrieval was 17.072 ms. The earlier five-lookup uncached conversation had
  a 17.909 ms median; different utterances and workloads prevent attributing
  this difference to caching. All seven latest patient-wait associations were
  excluded, so no end-to-end response improvement is established.

The tests exercise the real receiver and output callback with fake devices,
including the 150 ms deadline and memory-send/playback ordering, whitespace-only
transcript fragments, interruptions, both pinned SDK VAD notification formats,
and audio flushed before playback. Service tests cover foreground priority,
prediction that hangs, throttling across reconnects, superseded pending work,
stale background results, actual-question scoring, calibrated serving guards,
exact hits without embedding, expiration, capacity and patient separation.

## Outstanding before enablement

These are software correctness results, not robot performance evidence.

- Real MiniLM/Chroma calibration on representative development/heldout replays.
- Real retrieval replay and live Gemini prediction latency/quality.
- Manual shutdown summarisation returned a model-availability 404. Summary
  persistence failed in that test; prediction uses the same summary-model
  setting and remains unvalidated against the live provider.
- Loaded LattePanda runs with camera, pose, recording and dashboard active.
- Patient speech-end/output timing, underruns and interruption comparisons.
- Added steady-state RSS/CPU measurements and a two-hour stability run.

All new instrumentation/optimization flags remain **off by default**.
No speedup or deployment gate is claimed as passed. Configure an opaque patient
ID before using personal memory; the legacy shared database is not migrated.

See [implementation and rollout instructions](early-memory.md) for commands,
configuration, measurements, calibration rules and numerical acceptance gates.
