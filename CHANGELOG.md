- Add hybrid long-term graph consolidation: local immediate extraction plus
  bounded asynchronous Gemini Flash-Lite batches with schema/source validation,
  persistent daily attempt budgets and enrichment retry. Preserve negative
  evidence and forgetting races; keep graph proposals unattributed. Add nine
  hybrid tests and a three-request synthetic comparison (13/20 utterances
  represented versus 9/20 with rules; not a general accuracy metric).

- Add a reproducible synthetic five-interest memory diagnostic using the real
  local encoder and ONNX sentence embeddings. Report actual extraction misses,
  label-blind cosine clusters, retrieval probes and a lossy PCA visualization;
  isolate all invented memories from the live data folder.

- Add an experimental active local memory worker, durable SQLite extraction
  queue and evidence graph; group explicit interests/preferences/learning/goals
  in the Memory Palace tree. Preserve unverified attribution, inspect quotes,
  deduplicate exact claims and clear the graph safely on forgetting. Optional
  local Ollama extraction; rule encoder works without additional downloads.
  Graph proposals do not yet drive Gemini retrieval or verified-person profiles.

- Use explicit free-tier Flash-Lite for v10 background memory/appearance work;
  remove model fallback and share pacing/quota cooldown across workers. Zero
  charges require a Google Free Tier project; conversation/audio model unchanged.

Memory Palace builds on Conversational Ability (#12); the audio implementation
is owned by that separate feature PR.

# Memory Palace (#13)

- Add opt-in desktop WebRTC echo cancellation, noise suppression and gain control.
  Keep mic audio flowing during replies, tune activity detection for brief words,
  preserve raw recordings and expose input meters in Monitor.

- Add an isolated experimental Windows v10 launcher with key/model preflight,
  existing PC device adapters, persistent memory and graceful stop requests.
- Add Windows cross-process people-store locking and handle-safe JSON reads;
  preserve POSIX flock behavior. Close Chroma clients in storage tests.
- Live Windows smoke reached Gemini, memory/face readiness and the consent
  greeting; enrollment, restart recall and forgetting still need user validation.

- Support an external Gemini key file at startup via GOOGLE_API_KEY_FILE.
  Keep credential values out of source/settings output; report file errors safely.

- Add a separate read-only Memory Palace window: per-person/shared/unattributed
  views, search, context markers and clearly labelled live/offline snapshots.
  No model loading or new capture; bounded local inspection is opt-in.
- Keep profile, face records and pending conversation paths in BAYMAX_DATA_DIR
  alongside the existing memory database. API-key rotation never resets memory.

- Combine #8 people-awareness, attributed conversation memories and forgetting
  with #11's feature organization and opt-in public tools.
- Remove configured-profile-as-speaker and older-adult/home assumptions.
- Connect v10 to the shared debugging UI: transcripts, tool activity, reply state,
  memory counts and profile/visible-person separation. Debug bridge is opt-in.
- Preserve the configured Gemini Live model, audio processing and session resumption.
- Connected graph architecture is documented; graph storage and automatic
  curated feed ranking remain future work.
- Offline regression and identity/memory checks recorded in the PR; physical
  robot, Linux face/storage suite and live v10 monitor checks remain pending.

# Update notes

## Unreleased â€” experimental public-information toolkit

- Remove the unneeded identity diagnostic. Remove the v8 Temple University demo
  setting; add neutral current-person/setting instructions in v8/v9/v10; and stop
  v9/v10 treating old memories as unquestionable current-user facts. Models, audio
  processing and stored data are unchanged. The #8 identity/profile fixes are a
  separate small branch based on that PR.

- Add the proposed memory-palace design: canonical person-ID ownership, separate
  visible-person/current-speaker context, evidence-backed graph records, corrections,
  scoped recall, graph-driven curation, inspection and coordinated forgetting.
  Record the James identity investigation and acceptance scenarios. This is a design,
  not a deployed memory system or a confirmed diagnosis of the reported incident.

- Organize key systems under `Baymax-main/features/`, with sibling `memory_palace/`,
  `person_recognition/` and `toolkits/` packages. Move the existing toolkit code and
  update runtime imports, tests, documentation, container packaging and root paths.
  Memory/recognition folders document planned #8 integration; no second memory or
  identity implementation is introduced.

- Include PR #7's desktop setup UI, live monitor, device tools and launchers as a
  merge dependency; keep its original branch unchanged. Add setup checkboxes for
  public tool enablement/groups and name-only public tool activity in the monitor.
- Add an opt-in v8 desktop / v10 robot toolkit experiment: personalized headlines
  across nine categories, city lookup, weather,
  air quality, dated currency reference rates, holidays, recent earthquakes,
  Wikipedia background and book discovery.
- Add explicitly named local feed profiles with followed/excluded terms, strict
  matching, partial updates and forgetting. No inferred speaker identity or cloud
  profile synchronization. Public headline requests contain no interest/profile data.
- Preserve existing tool declarations and add v10 tool-response routing; keep the
  base conversation/model/audio pipeline and deployment settings unchanged.
- Add bounded HTTPS reads, short caches, partial-source failure handling, container
  source inclusion, offline tests and explicit synthetic provider smoke checks.
- Windows evidence: 162 combined offline checks (92 core, 70 toolkit)
  and 17 public-provider smoke checks pass. Live Gemini/hardware, interruption and
  reconnect behavior, Docker image build, commercial terms and lead approval remain
  pending. See Baymax-main/features/toolkits/EXPERIMENTAL.md.
## Unreleased - ARM installer test repair

- Fixed installer test fixtures to create and modify the requirements file selected for the host architecture. Four checks previously errored on Orange Pi because their temporary folders contained only the desktop profile.
- Added simulated Linux ARM coverage for all nine installer scenarios. All 108 offline checks pass locally (81 core, 27 toolkit); the repaired tests still need rerunning on the physical Orange Pi. Runtime dependencies and installation safety checks remain unchanged.

## Earlier - experimental Orange Pi CPU fall toolkit

- Added automatic native Linux ARM64 requirements selection, using MediaPipe 0.10.18 and a separate compatible NumPy/OpenCV/spaCy profile; desktop pins remain unchanged.
- Added a Debian 13 prerequisite clickable with an explicit system-package confirmation. ARM voice-analysis dependency Praat-Parselmouth requires a source build.
- Added a Setup toggle for experimental CPU fall detection, disabled by default, sharing existing camera frames with a bounded independent inference worker.
- Reused the unchanged upstream detector from commit 6d2386101d676df476eb97554963d60f1b5950b2; added model checksum verification, monitoring/performance state and uncertain Gemini check-ins without external alerts.
- 99 offline tests pass; ARM dependency metadata resolves and the native CPU pose backend processes a synthetic image on Windows. Actual Orange Pi installation, source build, fall accuracy and performance are pending.

## Earlier - first-run clickable installation

- Added Discord-ready ZIP/setup, Gemini key creation and usage-limit instructions in docs/DISCORD_QUICKSTART.md, with official Google links and explicit platform/data-flow limits.

- Clarified first-use and daily click order: Setup once, Monitor, Start Ember, then Stop Ember; Monitor can open before or after Start.

- OS clickables now bootstrap private Python 3.12, full requirements and the spaCy model without terminal commands on supported configurations.
- Installation readiness requires offline tests; requirement changes retrigger installation, failures remain retryable, and concurrent environment setup is blocked.
- Setup creates Desktop/clickables shortcuts pointing to the extracted repository. Start opens Setup if the key is missing.
- Nine installer regression tests bring the offline total to 80 (70 core/development, 10 toolkit).
- A fresh project environment was installed and tested on this Windows PC. Mac/Linux fresh installs and physical hardware are unverified; Intel/Rosetta Macs remain unsupported.

## Earlier - OS launchers and Gemini startup verification

- Grouped launchers into devops/clickables/Windows, macOS and Linux, sharing launch.py.
- Start Ember now verifies key acceptance and configured model visibility with Google before importing the conversation runtime or opening hardware.
- Failures explain invalid/denied access, unavailable models, quota, service or network problems without echoing keys/provider exceptions. Metadata success does not guarantee Live-session quota or generation.
- Added nine offline regression tests for the preflight and startup gate; no real keys or providers are used by these tests. All 71 offline checks pass locally on Windows (61 core/development, 10 toolkit); all 12 launcher relative paths were checked.

## Earlier - macOS device compatibility

- Added native Mac camera names and persistent camera IDs matching OpenCV's AVFoundation ordering.
- Save/test resolve the same camera after reordering; missing or ambiguous cameras require reselection.
- Discovery reads Mac camera metadata without starting capture; permission errors explain System Settings.
- Added 14 Mac contract simulations and three HTTP integration simulations; generic tests no longer depend on the host OS.
- Added a ready-to-enable Windows/macOS GitHub offline workflow example and a real-Mac verification checklist. Hosted testing is pending because this CLI login lacks workflow authorization.
- Local Windows result: 62 offline tests pass (52 core/development and 10 toolkit). Hosted macOS results must be checked in the PR; physical Mac and live Gemini validation remain pending.

## Earlier - Ember desktop development tools

### Added
- Local Gemini key/device setup UI with microphone, speaker and camera tests.
- Conversation monitor with incremental transcripts, grouped toolkit events and reply timing.
- Desktop hardware adapters and Windows/macOS/Linux launchers.
- Location/weather toolkit with approximate location, city clarification and caching.
- Setup documentation and offline tests for these additions plus upstream runtime parity checks.
- `check_release.py`: separate core/toolkit offline results and a pending live-review checklist.
- Testing protocol and PR template requiring core, toolkit and integration evidence.

### Verification and limitations
- 45 offline checks pass on Windows: 35 core/development checks and 10 toolkit checks. The total includes three tests for the review CLI itself.
- A Windows Gemini conversation and location/weather events were observed locally.
- Fresh-machine installation, macOS/Linux hardware, deployed robot version/parity and the full live checklist remain pending.
- No production deployment or merge is implied by these checks.
