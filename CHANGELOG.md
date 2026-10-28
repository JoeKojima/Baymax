# MVP (unreleased)

- Orange Pi 5 work that changes every platform (Windows, macOS, Linux including the
  LattePanda MVP and Docker robot paths), v10 camera thread and updater:
  - The fall toolkit (Setup's "experimental fall detection") reuses the core camera
    thread's pose result (`shared_pose`) instead of loading a second pose model, so
    enabling it no longer doubles pose CPU. With `camera.pose_tracking` on (the default)
    it loads and downloads no model of its own and gets no frames, only the core's
    landmarks; Monitor shows `Running on CPU (core pose)` (`NPU (core pose)` on a set-up
    Orange Pi) and the core's inference time. With pose tracking off, and in v8, it runs
    its own checksummed CPU model as before. Setup's checkbox now reads "experimental
    fall detection (CPU, or NPU on a set-up Orange Pi)".
  - The dashboard's annotated picture (7-20 ms a frame) is drawn only while someone
    watches the live view; a new viewer gets the latest frame drawn at once, or the
    camera's last one if it stopped. A frame already streamed is re-sent, not re-encoded.
  - The MVP updater (`devops/mvp/update.py`) installs the same package list as Setup on
    each platform (it always used requirements-pc.txt): requirements-orange-pi.txt on
    Linux ARM64, requirements-macos-intel.txt on Intel Macs. A pip or check_release run
    that times out (or a missing venv python) now rolls back like a failed one, and an
    update stopped after the switch (systemd's 20-minute start limit, power loss) is
    rolled back on the next start; both used to leave the robot on the new, unchecked
    commit. The update is recorded before the checkout, and a rollback records the bad
    commit before switching back: a rollback stopped before switching back or during its
    own package reinstall (a 15-minute pip timeout leaves little of the start limit) is
    finished on the next start, and that commit is never applied again. Still not
    handled, as before: a power cut during the git checkout itself (the update's or the
    rollback's) can leave the tree part old, part new, or a stale `.git/index.lock`;
    updates can then stop, with Ember starting on that unchecked mix (or on the failed
    commit) until it is fixed by hand (`git checkout --force HEAD` after removing the lock, then
    `git clean`; see devops/mvp/README.md). Follow-up: a forced checkout and removing the
    lock in the updater, once tested on a robot. An Orange Pi updating from an older
    version is switched by the old updater, which watched only requirements-pc.txt:
    install requirements-orange-pi.txt once by hand (or rerun Setup).
  - Memory's embedder (`semantic_embedder.py`) runs MiniLM with ONNX Runtime and the
    `tokenizers` library instead of optimum, transformers and torch. Same tokens, vectors
    within 3e-7 of before (cosine >= 0.9999998) and the same top-3 recall on 100 test
    queries, so existing `chroma_store`s keep working. ONNX Runtime gets two threads that
    sleep between calls (it used one per core, spinning, and set their core affinity
    itself). Where memory's packages aren't installed (the desktop profiles) memory
    stays off, as before. Where they are and `all-MiniLM-L6-v2-onnx/` is missing, Ember
    downloads it in the background on start (see the Orange Pi entry) instead of
    turning memory off. `deploy/make_lock.py` now also reads convert_model.py, so a
    regenerated Docker lock keeps optimum and torch for the image build's conversion,
    and counts `features/` as Ember's own code (it stopped with "no installed
    distribution provides module 'features'").

- Orange Pi 5 (RK3588) only (Linux on 64-bit ARM), on any kernel:
  - Ember runs on the four fast Cortex-A76 cores (cpu4-7), found from sysfs at start
    (`pc_hardware.use_performance_cpus`, called first thing by run_mvp.py and run_pc.py,
    before the eyes, network-watch and numpy threads start; it also moves any thread
    that already exists).
    MediaPipe runs pose on one thread on Linux: measured on the board, 43 ms per frame
    on an A76 and 215-247 ms on an A55. Linux on 64-bit ARM with fast and slow cores
    only: no effect on Windows, macOS, x86 Linux (including PCs with Intel/AMD hybrid
    P/E-core CPUs, and the LattePanda) or ARM boards whose cores are alike, and never
    onto a single core. `BAYMAX_CPU_PINNING=off` turns it off.
  - The camera keeps only its newest frame (V4L2 buffer of 1; Linux ARM64 with the pc
    profile, which the Orange Pi runs), so pose and fall detection see the present
    instead of a frame up to ~0.2 s old (an estimate from OpenCV's four-frame queue).
    Not measured yet, on the Orange Pi either: no camera run there so far. x86 Linux
    cameras (the LattePanda MVP, which also runs the pc profile, and the Docker image)
    keep OpenCV's default queue.
  - `requirements-orange-pi.txt` gains the MVP packages it was missing (pywebrtc-audio,
    pyfirmata2, miniaudio, vosk): audio cleanup is on by default, so the runtime stopped
    at import on the Pi.
  - Memory is on: `requirements-orange-pi.txt` adds chromadb 1.5.8, onnxruntime 1.24.4
    and tokenizers 0.22.2 (no torch), with ChromaDB's OpenTelemetry held at 1.27.0, the
    last release that accepts MediaPipe 0.10.18's protobuf < 5. On its first start with
    memory retrieval on, Ember downloads all-MiniLM-L6-v2's own ONNX export and tokenizer
    from Hugging Face in the background (about 90 MB, revision 1110a243) into
    `all-MiniLM-L6-v2-onnx/`. Ember starts and talks meanwhile, and memory turns on when
    it finishes. Each file is checked by size and SHA-256 and synced to disk before it
    is renamed into place, model.onnx last; partial files are cleaned up after 10
    minutes. `python semantic_embedder.py --download` fetches it beforehand (needed
    once when memory retrieval is off, for session summaries). Measured on the Orange
    Pi 5 Max with 500 memories: a lookup takes 34 ms of CPU and is ready in 19 ms on two
    fast cores (about 140 ms on a slow core); in the full-system test Ember went from
    42.7% to 42.8% of one core (NPU pose) and from 91.8% to 92.0% (CPU pose), with one
    lookup per user turn and none timed out; RAM about 170-230 MB more (the torch-based
    setup measured 410-500 MB on a Mac). Not yet run in a live conversation.
  - `requirements-orange-pi.txt` also installs rknn-toolkit-lite2 2.3.2 (aarch64,
    Python <= 3.12), the NPU's Python side, which was a manual step: the same version
    as the converter's default, which the accuracy check records. It is imported only
    on an RK3588. `librknnrt.so` is still installed by hand (needs sudo).

- Pose can run on the Orange Pi 5 Max's NPU (RK3588), with MediaPipe on the CPU
  everywhere else. New `features/pose/`: MediaPipe Pose Landmarker's VIDEO-mode graph
  (detector, ROI tracking, heatmap refinement, smoothing) ported to numpy + OpenCV,
  running the two networks from the same `pose_landmarker_full.task` either on the NPU
  (RKNN FP16, via rknn-toolkit-lite2) or ONNX Runtime. The camera thread asks
  `create_pose_backend()`: the NPU only on Linux aarch64 + `rockchip,rk3588` +
  rknnlite + converted models whose SHA-256s and on-board accuracy check (run with the
  same rknn-toolkit-lite2 version, and the same librknnrt/driver version as now) are in
  `manifest.json` + a working `init_runtime`; otherwise, and on any error while checking
  (unreadable files, malformed manifest), MediaPipe with the same options and calls as
  before. The port uses the runtime's MediaPipe confidence settings. The choice and the
  reason are logged once (`[POSE] Pose backend: ...`), with a warning if the runtime's
  .task weights changed since conversion; `BAYMAX_POSE_BACKEND=cpu|npu|onnx` overrides
  (`mediapipe` = `cpu`; any other value is MediaPipe with a warning, never the NPU;
  `npu` also runs on a changed librknnrt/driver, with a warning). An NPU error mid-run,
  including NaN/inf outputs, switches to MediaPipe and logs the traceback once. The
  fall toolkit shows `Running on NPU (core pose)`, and `Running on CPU (core pose)`
  after such a fallback.
  Board-side `features/pose/convert_rknn.py` (+ `setup_converter.sh`) derives ONNX
  from the .task's TFLite (densify + tf2onnx, checked against TFLite), builds RKNN with
  rknn-toolkit2 2.3.2 or 2.5.0 and checks NPU vs ONNX: raw landmarks 0-34 (incl. the
  two that steer tracking), visibility of 0-32 and the pose flag, then the whole
  pipeline with each engine tracking on its own over still frames and a 72-frame
  tracked sequence (person leaves and returns, falls, lies still): positions, and
  which landmarks are visible (>= 0.5, what fall detection and face presence use),
  with minimum frame counts so a check that compared nothing fails. This gate checks
  the conversion, not behaviour on real footage. A changed or truncated runtime .task
  makes the export download the pinned one. `benchmark.py` prints CPU vs NPU ms and
  process CPU per frame (all threads, while each frame's pose runs), back to back or
  paced like the camera (`--fps 15`, `--empty`), pinned to the fast cores before numpy
  loads, as Ember is.
  Results (features/pose/VALIDATION.md):
  - The port on ONNX Runtime (FP32), macOS, 8 licensed clips + synthetic falls (8,075
    frames at 15 fps): agrees with MediaPipe as closely as MediaPipe agrees with itself
    under +-1 grey level of noise, with the same fall decisions (2 real, 11 synthetic).
  - The NPU (FP16) on an Orange Pi 5 Max (Armbian Debian 13, vendor kernel 6.1, RKNPU
    0.9.8, 2.3.2 toolkits) against MediaPipe 0.10.18, 17 licensed clips + synthetic
    falls: on real clips closer than the noise baseline (detection 98.1% vs 95.5%), but
    it confirmed 1 of MediaPipe's 2 real-clip falls and 10 of its 11 synthetic ones, and
    on the synthetic falls it is below the baseline (detection 96.9% vs 99.0%,
    fall-pending 98.4% vs 99.1%). The real-clip difference is judo_soft, a two-person
    throw then get-up: same onset, then the NPU lost the person for a frame, re-found
    the standing partner and dropped the pending fall. The synthetic one is
    kettlebell_squat at 25 s, a slow lie-down labelled "no fall" that MediaPipe flagged.
    The converter's check passed on that board; `check --video` on 8 hard real clips
    fails its limits, which are set for the synthetic sequence.
  - Speed on that board: 10.5-11.9 ms instead of 36-44 ms per frame back to back
    (benchmark.py, per-clip medians over the 17 clips; all speed and CPU figures here
    predate benchmark.py loading numpy after the pinning, not re-run). Paced at the
    camera's 15 fps (`benchmark.py --fps 15`) a frame takes longer, as the clocks drop
    between frames:
    median 16.2 ms (p90 23.8) with a person in view and 28.5 ms with nobody in view
    (`--empty`: the detector then runs every frame), against 43.7 / 39.6 ms for
    MediaPipe. Process CPU at 15 fps: 12.4% instead of 76.7% of a core with a person in
    view, 19.7% instead of 70.5% with nobody in view (an earlier paced script that
    counted the whole run gave 13% / 76%).
  Stop Ember before the check or the benchmark. `setup_converter.sh` now always installs
  setuptools<80.1 into the converter venv (rknn-toolkit2 2.3.2 imports pkg_resources,
  which a fresh Python 3.12 venv lacks; found on the board).

- Tested offline only. `check_release.py` passes on macOS at this commit (core 356
  tests, 1 skipped: the parity test, no onnxruntime; toolkit 158), also with
  `BAYMAX_CPU_PINNING=off` exported or in `.env`, and passed on the Orange Pi 5 Max at
  commit 61c410e (core 352 tests, 15 skipped, 0 failures; toolkit 158, 0 skipped). The
  board's 15 skipped are, by the counts, the 14 git-based updater tests (no git on the
  board) and the off-RK3588 default test, so no updater test has run on Linux aarch64
  yet (to confirm with a verbose run next time). Windows was checked only by emulating
  its paths and file handling on macOS. The NPU tools (converter, check, validate,
  benchmark back to back and paced) ran on the board with Ember stopped. Pending:
  `check_release.py` on real Windows; Ember running live on the Orange Pi (not started
  there yet, CPU or NPU: audio, camera, a conversation, the live view, the fall
  toolkit's status, fast-core pinning under ember-mvp.service, and that the runtime
  gets past import with the added `requirements-orange-pi.txt` packages; with the NPU:
  the camera thread, `[POSE] Pose backend: NPU`, Monitor `Running on NPU (core pose)`,
  a mid-run fallback, a conversation alongside); the camera loop with the 1-frame
  buffer on the Orange Pi's camera (loop FPS and frame age: with Setup's fall-detection
  checkbox on, the Monitor's FPS should stay near 15, or read the FPS overlay in the
  live view); thermals over a long run; fall accuracy at home (real rooms, older
  adults, two people in view); the live view (open, close, reopen, two viewers) and
  the fall toolkit's status and inference time on Windows, macOS, the LattePanda and
  the Orange Pi; the updater tests on Linux aarch64 (install git on the board); the
  updater's rollback on a robot, and handling a power cut during its checkout (above).

- Eyes sleep 10 s after the conversation goes quiet (was 180 s), counted from
  when Ember's voice finishes playing (`reply_playback_done` now resets the
  timer). Speech wakes them to normal blinking. `BAYMAX_EYES_SLEEP_AFTER_S`
  still overrides.

- Ember answers questions about what the family wrote: asked "do you know my
  name?" with "Call the person James" in its profile, it said it didn't know,
  because the profile said "don't recite it". The header now says to answer
  such questions directly, but still not to read the profile out unprompted.

- Replies to family messages are confirmed out loud: after `send_reply`
  succeeds, Ember says "Got it, message sent." (instruction plus a `say` field
  in the tool result); on an error it says the reply didn't go through.

- Stopping an audiobook no longer needs shouting up close. On ember-002 only 6
  of 40 wake-word utterances paused the book. Pairs now count in either order
  ("Ember, hey"), with hey/okay + stop/pause, at confidence 0.8 (was 0.9), and
  across Vosk splitting "hey | Ember" up to 3 s apart: 9 of the same 40 logged
  attempts now wake. Any wake word heard alone also lowers the book to 30% for
  4 s, so the rest of the phrase is heard over it (needs a live check; narration
  that sounds like a wake word causes the same brief dip). Lone "ember" still
  never wakes.

- Ember app link (needs `BAYMAX_DEVICE_KEY`; Company-Website `messages-reminders`
  branch): every ~15 s the robot reads its inbox and syncs back. Messages from
  the dashboard are read out at the next quiet moment and Ember offers to
  reply (a reply is read back and sent only after the person confirms).
  Reminders made on the dashboard, and cancellations, go into the everyday
  reminder store. Notes about the person are summarised with
  `gemini-3.5-flash` into `[ABOUT THE PERSON]` in Ember's instructions (and
  sent mid-conversation when they change); the summary goes back to the app.
  Transcripts are never sent. See `features/toolkits/family/README.md`.

- Reminders repeat like Apple Reminders' custom repeat (hourly to yearly,
  every N, weekdays, days of the month, "the second Tuesday", end date), by
  voice or from the app. Ember asks whether each one was done and logs done /
  not done / no answer (asking once more after 10 minutes); the history is
  kept and shown on the app, with who set each reminder. Offline tests:
  `test_toolkit_reminder_repeats.py`, `test_toolkit_family.py`; also run
  against the site's real Worker locally. Live robot check pending.

- Audiobooks play 25% louder: narration was quiet on ember-002 even at full
  book volume. New `BAYMAX_BOOK_BOOST` (default 1.25) multiplies the book
  volume, and samples are clipped so loud passages cannot wrap into noise.
  The spoken "louder/quieter" 0-100 % control is unchanged (100 % now means
  the boosted level). `BAYMAX_BOOK_BOOST=1` restores the recording's level.

- Session memory summaries on v10 use `gemini-3.5-flash`. Google now answers
  `gemini-2.5-flash` with 404 ("no longer available to new users"), so every
  shutdown summary failed and nothing was saved. 3.5 Flash answered a test
  prompt in about 1 s; 3.8 Flash (Google's suggestion) was returning 503 "high
  demand", and the summary runs once at shutdown without a retry. The live
  conversation model is unchanged. Live shutdown summary check pending.

- Only when addressed, enforced on the robot: proactive audio and the prompt
  only asked Gemini to stay quiet, and on ember-002 it still answered
  `<noise>` and an overheard phone call. v10 now plays a reply only when the
  person said "Ember", spoke within 20 s of Ember's last reply
  (`BAYMAX_FOLLOW_UP_SECONDS`), or it answers Ember's own alert/reminder/
  greeting message; otherwise it is dropped and logged as `[ADDRESS]`. Uses
  Gemini's existing transcription only. `BAYMAX_ADDRESS_GATE=0` backs it out.
  The name also counts when transcription splits it ("A mber", "E mperor"):
  the first robot run dropped "Hey, E mperor, what's the weather".
  Offline tests pass (tests/test_address_gate.py); live robot check pending.

- Long conversations: add Gemini Live context window compression (sliding
  window at 32k tokens) and session resumption. Without them Google ends
  audio+video sessions after 2 minutes and resets connections about every 10,
  and Ember reconnected as a brand-new conversation with a growing, slower
  context. The receive loop stores each resume handle (parity test strips the
  hook); a handle that fails once or is near Google's 2 h expiry is dropped.
  Full config validated against google-genai 1.62; live robot check pending
  (BAYMAX_CONTEXT_COMPRESSION=0 / BAYMAX_SESSION_RESUMPTION=0 to back out).

- The local "hmm" while Gemini thinks is off by default on the MVP (testers
  found the fixed sound unsettling); `BAYMAX_WAIT_CUE_ENABLED=1` restores it.

- Robot test follow-ups: `public_poem` takes a title (PoetryDB title search,
  narrowed by author when given; live check returns The Raven even for "Edgar
  Allen Poe"), and Ember quotes requested lines instead of paraphrasing.
  TheMealDB (~300 recipes) has no peach cobbler, so when a recipe is missing
  Ember offers a simple version from its own knowledge and says so.

- Robot test: asked for "mystery thrillers", Gemini searched TVmaze by genre
  and gave up when it failed. TVmaze search is title-only and currently
  returns 502 for some queries ("mystery thriller", "crime drama", even "the
  crown"; "the office" works). `public_shows` now says title lookup only, and
  the prompt says to recommend from Gemini's own knowledge or Google Search
  and keep helping when a source fails.

- Discovery tools (trivia, TVmaze shows, PoetryDB, radio station lookup,
  TheMealDB recipes, Wikipedia, Open Library) are on by default via
  `EMBER_DISCOVERY_ENABLED` (default 1), independent of the opt-in public
  toolkit so a Setup save with it off does not remove them. Feeds, extra
  weather/air quality, reference and events stay opt-in. 226 core + 126
  toolkit offline tests pass; live provider calls on the robot pending.

- Fix Ember going deaf after connecting: the everyday reminders session task
  awaited `shutdown_event.wait()`, but the runtime passes a threading.Event,
  so it blocked the event loop forever (py-spy on ember-002: MainThread in
  everyday/tools.py:100; mic captured but never sent, no startup hello). It
  now sleeps with asyncio; the hook documents the contract, and a test runs
  every registered session task with a threading.Event and checks the loop
  keeps ticking (it hangs without the fix).
- Eyes stay on the confused spinner after Gemini connects until mic audio is
  actually being sent (`mic_live` from the audio cleanup, at most 1/s), and go
  back to it if mic audio stalls (`mic_stalled`); the waiting cue ignores both.
  `BAYMAX_EYES_WAIT_FOR_MIC=0` restores connect = ready. 226 core + 125 toolkit
  offline tests pass; robot check pending.

- Ember's systemd unit stops within 15 s (`TimeoutStopSec`, was 180 s) so a
  slow or hung exit no longer holds reboots for minutes; systemd then kills it
  and the eyes' ExecStopPost still runs. Existing robots rerun
  `python3 devops/mvp/install_autostart.py` once to apply.

- Eye-contact greetings are off by default (`BAYMAX_PRESENCE_GREETING=1`
  enables). On ember-002 (i5-8200Y, pose fall detection also on) the mic
  stopped reaching Gemini for 144 s starting 2 s after the face model loaded,
  and the startup hello never ran. On a desktop the face model at 81 Hz did
  not delay the event loop, so robot CPU load is the likely cause; pending a
  robot check with fall detection on/off. The startup hello stays on.

- Double replies were still intermittent: our runtime records each Gemini
  transcription event once, so Gemini itself generated the reply twice, each
  time around a mid-reply `set_eye_expression` call. Gemini no longer gets the
  eye tool by default; `features/eyes/auto.py` picks one expression per reply
  locally from explicit requests ("make a sad face") or clear cues in Ember's
  reply opening ("I'm so sorry"), via a new monitor `transcript_observer`.
  `BAYMAX_EYE_TOOL=1` restores the tool. Eye contact is logged to the
  dashboard as "Eye contact started/ended" (with the greeting decision), with
  a 1 s grace so blinks do not split a look. 224 core + 104 toolkit offline
  tests pass; robot check of doubles/expressions pending.

- Add persistent local timers, one-time reminders and shared household lists.
  Confirm tool saves, clarify absolute-time offsets, and announce overdue reminders
  through the existing Gemini session. Disable with EMBER_EVERYDAY_ENABLED=0.
  Spoken delivery requires Ember online; live voice/device checks remain pending.

- Add five opt-in Discovery tools: cached trivia batches, TVmaze show/cast/schedule
  lookup, PoetryDB excerpts, radio station discovery, and
  TheMealDB recipes. Reuse bounded HTTPS/cache and Gemini tool routing; no new
  dependencies or capture. Radio playback and live hardware checks remain pending.

- Robot timeline showed the double reply again inside one turn, right after a
  mid-reply `set_eye_expression('normal')`: remove `normal` from the tool enum
  (eyes reset on their own) and tell Gemini never to call it for neutral
  replies. Misspelled `BAYMAX_VOICE` ("Archernar") now maps to the closest
  voice, unknown names to Autonoe, instead of silently Fenrir. The startup
  hello waits 3 s, skips if someone is already talking, and is retried once if
  the session drops right after it (it did on ember-002, so no hello was heard).

- Ember starts conversations: one calm hello at startup and a greeting on new
  eye contact (MediaPipe Face Landmarker on existing camera frames via new
  `fall_detection.frame_observers`/`session_tasks` hooks; quiet 120 s, cooldown
  10 min, never during books/replies). Real-model check on a frontal portrait:
  eye contact detected at 202/142/101 px face widths, none below ~80 px
  (~1-1.5 m); ~8 ms/frame desktop CPU. Persona gains a [YOUR VOICE] section
  (calm, unhurried, not cheerful by default) since Autonoe still sounded
  upbeat. 220 core + 102 toolkit offline tests pass. Robot checks pending:
  real eye contact/false greetings, CPU on the robot, voice tone.

- Audiobooks play at full volume by default (`BAYMAX_BOOK_VOLUME`, 0-1) and
  the tool gains `volume` (0-100 %) so people can say "louder"/"quieter";
  status reports the volume. Persona asks for a calm, warm resting voice
  (Autonoe sounded too energetic) that brightens or softens with their mood.

- Ember's voice defaults to Autonoe (`persona.DEFAULT_VOICE`) when
  `BAYMAX_VOICE` is unset; `BAYMAX_VOICE` still overrides it.

- Google Search grounding: the toolkit adds Gemini's built-in `google_search`
  tool plus a short prompt (use it for uncertain/current facts and book
  critique, cite sources briefly, results are data). Gemini runs the search;
  only the query goes to Google. `BAYMAX_GOOGLE_SEARCH=0` (or disabling the
  toolkit) removes it; the dashboard capability reports it. Offline config
  tests pass; live check pending on the robot (no Gemini key on the dev PC).

- Audiobook book mode: while a LibriVox book plays the mic is withheld from
  Gemini (it answered the narrator as the person) and fed to an offline Vosk
  wake-word listener ("Hey Ember", "Ember stop/pause", "stop", "pause"). Books
  also pause after every chapter; Ember gets an `[AUDIOBOOK]` note with book,
  author and chapter so it can discuss it, and announces it is playing a
  recording. Falls back to the open mic without Vosk/model. Offline narration
  eval (A Tale of Two Cities ch. 1-2, ~22 min, leak 10/20 dB down): 0 false
  pauses; 15/16 phrases detected (missed "ember pause" at 10 dB down, its
  "pause" alone still works at 20 dB). Robot run with real speaker/mic pending.

- Integrate all supplied eye-expression patterns with an explicit Gemini
  `set_eye_expression` command: happy, sad, excited, confused spinner, surprised,
  curious, love, sleep bubbles and dead/X pose, plus normal blinking. Keep the
  existing >2s thinking dots automatic. Fall-alert dispatch forces surprise
  above model expressions/thinking; detection remains opt-in and local.
- Preserve expressive eyes through buffered reply playback; reset on new turns,
  interruption/reconnect/stop, playback drain or safety expiry. Validate command
  arguments and keep eye tools independent of public toolkit enablement.
- 180 core + 73 toolkit offline checks pass (253 total), including animation
  order, late fragments, fall priority, playback completion, invalid/disabled
  hardware and async tool response IDs. Physical eye/voice timing and fall
  check-ins on ember-002 remain pending.

- Default MVP to Gemini `NO_INTERRUPTION` while preserving healthy-DSP microphone
  delivery during replies, AEC and residual subtraction. Add private
  `BAYMAX_INTERRUPTION_ENABLED=1` override to restore interruptions; other
  launchers keep the earlier default. The setting also applies with enhancement
  disabled without changing that raw path's playback gating. Echo may still be
  recognized; no transcript is deleted or treated as guaranteed speaker identity.
- Offline tests cover the no-interruption config, unchanged audio/transcription
  settings, MVP default and saved override. Physical Gemini/LattePanda checks
  remain pending.

- Enable the opt-in MVP user service at user startup; add an independent,
  restarting Monitor service and desktop/Applications launcher. Open Monitor at
  desktop login when a physical display is connected; otherwise keep it local
  and available through the launcher. GNOME automatic login is still required
  for unattended boot. Setup uses a separate port to avoid Monitor conflicts.
- Bound Monitor requests to five seconds so stalled requests can retry. Offline
  startup/launcher tests pass; physical Debian login/boot and browser recovery
  still need validation on ember-002.

- Add conservative residual echo subtraction after WebRTC: search bounded recent
  speaker PCM for changing delays, require strong/stable correlation, subtract
  only 90% of the fitted component and retain unexplained user audio. Uncertain
  fits/strong double talk pass unchanged. No fixed mic/speaker geometry is used.
  Extra-layer failures retain WebRTC; local disable override is documented.
- Add debugger-only recent transcript prefix-overlap warnings. Retain all user
  text; no transcript-based gating, identity claim or extra provider/capture call.
- 169 core + 70 toolkit tests pass (239 total); ten new regressions cover delays,
  polarity, movement, quiet/strong double talk, reference bounds/staleness,
  failure isolation and diagnostic-only overlap/expiry. Native synthetic echo/
  double-talk probe still passes. Physical room behavior and CPU cost are pending.

- Fix residual robot speech being re-amplified by AGC after AEC: remove positive
  gain while speaker audio, its device buffer and a 250 ms echo tail are active.
  Healthy DSP still passes user speech during replies; failed DSP suppresses
  microphone input through the audible tail, even after queued playback drains.
  Raw recording and disabled enhancement behavior remain unchanged.
- Feed callback ADC/DAC timing into AEC delay hints and show echo protection /
  effective gain / delay in Monitor diagnostics. Apply shared v8/v10 hooks.
- 159 core + 70 toolkit offline tests pass (229 total). Native pinned-WebRTC
  synthetic probe: 11.0 -> 26.1 dB net echo reduction, with non-muted independent
  voiced double talk. Actual LattePanda echo, quiet interruptions, volume and
  timing still require live testing; no hardware recording or cloud call made.

- Lower Conversational Ability speech-start sensitivity from high to low after
  the ember-002 user reported overly sensitive interruptions in the MVP service.
  Keep echo cancellation, noise reduction, prompts/models and turn-end timing.
  Add local `BAYMAX_SPEECH_START_SENSITIVITY=high` override; invalid values warn
  and use low. Document the existing runtime debugging Monitor command.
- 154 core + 70 toolkit offline checks pass (224 total); real-device improvement
  and intentional-interruption tradeoff still need confirmation on ember-002.

- Fix the generated user service's invalid quoted WorkingDirectory path, which
  systemd rejected as non-absolute on ember-002. Keep ExecStart word quoting;
  add a regression assertion for the distinct path directive. Import only set
  desktop environment variables, avoiding the X11 WAYLAND_DISPLAY warning.

- Add one local synthetic “hmm” for response waits over 2s, at most once per wait.
  Uses the existing speaker callback/AEC render reference. Reply audio, cleaned
  near-end speech, turn end/interruption and reconnect cancel it. No new audio
  stream, prompt/model change or provider request. Requires healthy AEC; local
  `BAYMAX_WAIT_CUE_ENABLED=0` disables it. Preserve raw recording/disabled parity.
- Load saved feature overrides before starting eyes; this fixes local eye
  enable/port overrides being read before `.env` was loaded.
- Cue/installation update: 152 core + 70 toolkit offline tests pass (222 total),
  Windows/Python 3.12.15; no failures/errors/skips. Added LattePanda download and
  setup commands. Audible quality, echo/interruptions and boot testing remain pending.

- Integrate the supplied eyes under `features/eyes/`: hollow open/blinking eyes
  while speaking/listening; six-position thinking dot only after a wait exceeds
  two seconds from the latest user-transcription fragment. First reply audio,
  turn end/interruption and reconnect restore normal blinking. Brightness 3,
  left device 1/right device 0, no flips or servo. Serial writes run separately
  from Gemini; missing eyes are non-fatal. Linux MVP enables them by default.
- Eye integration gate: 139 core + 70 toolkit tests pass (209 total), zero
  failures/errors/skips, Windows/Python 3.12.15. Physical eyes/LattePanda live
  integration remain pending. Expression integration is described above.

- Combine #7, #11 and #12 into one PR against main. Defer #8 people awareness,
  #9 latency optimization and Memory Palace. Eye integration is described above.
- Add `run_mvp.py`: require explicit saved sensors once, then reuse configuration
  through the existing v8 key/model preflight and runtime. Add an OS-backed
  concurrent-launch lock and graceful SIGTERM handling.
- Add one-time Setup completion and opt-in Debian desktop autostart in
  `devops/mvp/`, using a dedicated user service and the private Python runtime.
- 124 core + 70 toolkit offline checks pass on Windows/Python 3.12.15. Actual
  Debian installation, devices, live conversation/toolkits and cold-boot tests
  remain pending. No robot deployment, production service change or merge.

# Conversational Ability (#12)

- Extract responsive audio into `features/conversational_ability/`, independent
  of Memory Palace, Chroma and face recognition (reviewed in #13).
- Integrate WebRTC AEC3, noise suppression, AGC2, short-answer detection and
  listening during replies into v8/v10; preserve models/rates/raw recording.
- Add a Setup feature toggle/noise level and microphone telemetry in Monitor.
- Include generic external-key-file loading for desktop startup.
- Combined Windows v10 live test was reported responsive by the user;
  standalone v8 integration still needs a separate live test.

# Update notes

## Unreleased — feature organization and toolkits (PR #11)

- Stack on PR #7 for all OS compatibility, setup, device and monitor infrastructure, including the Intel Mac profile. The PR review diff excludes that inherited foundation.
- Scope location/weather, optional CPU fall detection and opt-in public information/headline/feed toolkits to this branch. Existing camera frames, bounded provider calls, explicit preferences, monitoring, controls and disabled behavior are preserved.
- Organize features under sibling `memory_palace/`, `person_recognition/` and `toolkits/` namespaces; memory/person folders reserve integration boundaries. Prompt/identity/recall changes and Memory Palace implementation belong to PR #13, not this PR.
- Runtime modifications here are limited to feature import paths, toolkit dispatch/lifecycle and monitoring. Original v8 prompt/models/audio behavior remain covered by strict upstream parity checks; v9 matches main.
- All 169 combined offline checks pass on Windows/Python 3.12: 99 core and 70 toolkit, with zero failures/errors/skips. Actual Gemini/hardware, Mac/Linux/Orange Pi installs, fall accuracy/performance, Docker and live integration remain pending. Earlier provider smoke checks do not establish current provider availability or full conversation correctness.
