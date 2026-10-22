# Local development changes

## Optional memory caches and local latency measurements

- Add patient-scoped asynchronous retrieval with an exact-query cache and a
  prepared-memory cache. Keep MiniLM, Chroma, Gemini Live, the rolling 30-word
  query, top-three results, distance cutoff and 150 ms remaining-search wait.
- Prepare possible follow-up memories from recognised transcript cues, using
  lower-priority local work and at most one prediction attempt per 30 seconds.
  Prepared-cache serving requires a matching passing calibration artifact.
- Export local OpenTelemetry retrieval, delivery and audio timing records,
  with bounded queues, rotating files and no transcript or memory contents.
- Add desktop memory controls, fictional-memory checks, evaluation tools and
  documentation. Personal memory requires an explicit patient ID; the legacy
  shared database is not migrated. Experiments remain disabled by default.
- Validation: 133 offline tests passed. Mac voice interaction and local cache
  checks completed. A controlled warmed-query comparison measured 1.939 ms
  uncached versus 0.088 ms for exact hits; live latency benefit remains unproven.
- Outstanding: representative prepared-cache and prediction tests, reliable
  patient-wait associations, loaded LattePanda resource/stability measurements,
  and the existing summary model's availability error during shutdown saving.

## Desktop memory integration

- Add an explicit version-10 memory testing selection to the existing desktop
  launcher, preserving the original version-8 mode and production service files.
- Save an opaque test-person ID and independent memory/timing switches in Setup.
- Add local fictional-memory seeding and a two-query cache mechanism check.
- Show the selected core and numeric memory statistics in Monitor.
- Prepare the existing MiniLM ONNX model and pinned memory dependencies during
  Setup; include the memory pytest suite in the existing offline review gate.
- Desktop core/toolkit/memory checks pass locally with simulated devices. Mac
  voice interaction has been exercised; camera, other-platform and loaded
  LattePanda performance checks remain pending.
