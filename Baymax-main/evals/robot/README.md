# Robot test rig (Layer 2): a test robot that tests itself against a PR

Layer 0 (`.github/workflows/offline-checks.yml`) runs `check_release.py` on every PR.
Layer 1 (`evals/live_eval.py`) plays synthetic turns straight into Ember's Gemini
session. Neither touches the robot's microphone, echo cancellation, speaker, camera,
eyes, CPU or wake words. Layer 2 does, without a person in the room:

- A dedicated **test robot** runs the PR's **real runtime** (`run_mvp.py` → `run_pc.py` →
  `realtime_gemini_N.py`) with its real devices.
- A **second USB speaker** about 2 m away plays the scripted "person" (synthetic speech)
  and noise (synthetic TV babble, music, pink noise). Ember's echo cancellation only
  removes Ember's own output, so the robot hears this like a person in the room.
- A **tablet** in front of the camera shows synthetic cards from a page the rig serves.
- The rig watches only what the runtime already writes: `.runtime-status.json`
  (`runtime_monitor.py`: states, events, transcripts, toolkit activity), the stdout log
  (`[USER]`, `[GEMINI TXT]`, `[ENGAGE]`, `[WAKE]`, `[EYES]`, `[MIC LEVEL]` ...), and CPU and
  memory of the process tree.
- It checks each scenario, writes a Markdown and JSON report, and the workflow posts
  the report on the PR.

```bash
python evals/robot/rig.py --root ../pr/Baymax-main --dry-run   # plan and gating only; opens nothing
python evals/robot/rig.py --root ../pr/Baymax-main --yes       # test robot only
python evals/robot/rig.py --calibrate --spl 60.5               # level calibration (below)
python evals/robot/rig.py --root ../pr/Baymax-main --cleanup   # stop a leftover runtime, delete recordings
```

## Design: a self-hosted runner on the test robot, composed with PR #22

**Chosen: option (a).** `.github/workflows/robot-test.yml` runs on a GitHub Actions runner
registered on the test robot (`runs-on: [self-hosted, ember-test-robot]`). It is triggered
when someone adds the `robot-test` label to a same-repository PR.

Why (a) rather than (b), driving PR #22's boot updater:

- **Per-PR and on demand.** #22's `ember-update` follows one configured PR and updates at
  boot. Switching PRs means rewriting device settings and rebooting. A labelled run
  checks out exactly the labelled commit (`head.sha`, so a later push cannot slip in),
  and the result lands back on that PR.
- **Built-in plumbing.** Actions provides queueing (one runner, so one job at a time), timeouts,
  cancellation, secrets scoped to an environment with required reviewers, and a place
  to post results. With (b) we would have to build all of that.
- **The installed release is untouched.** The PR runs from the runner's workspace.
  `~/ember-deployment/current` is never re-pointed, and no systemd unit is changed.

How it composes with #22 (Natey's files are only read, never modified):

- #22's image provisions the test robot: the OS, packages, AnyDesk, device detection, and
  an installed release with a ready `.venv`.
- `prepare_checkout.sh` reuses that `.venv` when the PR's `requirements*.txt` and
  `devops/clickables/bootstrap.py` match the installed release. This is the same
  shortcut `ember-update` takes for code-only updates. Otherwise it installs the way
  `ember-update` does, including the v10 native packages when the PR runs
  `realtime_gemini_10.py`.
- **A LattePanda test robot instead** (the venv runtime, like unit 001, no #22 release):
  set `EMBER_RIG_VENV` in the runner's environment to the robot's working venv (for example
  `/home/meowmax/Baymax/Baymax/Baymax-main/venv`), and `prepare_checkout.sh` links it
  instead of installing. It's used as is, so a PR that changes requirements has to be
  checked against it by hand. `baymax.service` must be stopped, because the rig refuses
  to start next to another Ember runtime.
- The rig's `base_env` points at the installed release's `.env`. The robot's saved
  microphone, speaker and camera choices (from Ember Setup and `ember-hardware.py`) are
  reused, and any key in that file is dropped.
- On the test robot, `ember.service` must not hold the microphone. Keep **no
  `GOOGLE_API_KEY`** in `~/ember-deployment/current/Baymax-main/.env`: `ember-run` waits
  for a key and never starts capture. Also create `~/.config/ember/no-autostart`, which
  `ember-update` honours. The rig refuses to start while any other Ember runtime is
  running (it checks every process, not just its own checkout). It never stops a
  service itself.
- Do not put the Docker nightly updater (`deploy/provision/`) on the test robot. That
  unit runs the runtime all the time.

## Scenarios (`scenarios.json`)

These are the same format as Layer 1. `heard`, `reply`, `not_reply`, `tools`,
`not_tools`, `no_tools`, `max_words` and `max_latency_s` are checked by
`live_eval.check`. Synthesis (`live_eval.synthesize`), cards (`live_eval.card`) and
config building (`live_eval.core_session`, `launcher_defaults`, `missing_requirements`)
are reused too.

| Scenario | What only the robot can show |
|---|---|
| `conversation-latency` | Two turns through the real mic and AEC; first audio within 5 s of the person's line ending (this includes Gemini's end-of-turn wait); states thinking → speaking → listening |
| `barge-in` | Talking over a long reply interrupts it (skipped while `BAYMAX_INTERRUPTION_ENABLED=0`, the MVP default) |
| `echo-own-voice` | No echo flag, no user transcript while Ember speaks, then 15 s of silence |
| `disengage-tv-engage` | PR #21: "Ember, disengage", then 2 min of synthetic TV with near-miss words ("members engaged", "embers"). Zero false wakes, nothing reaches Gemini. "Ember engage" works at 1 m and at 3 m-equivalent (−9.5 dB) |
| `reminder-reengages` | A reminder that comes due while disengaged engages Ember again and is announced |
| `look-closely-card` | PR #24: a label on the tablet, read by the real camera |
| `radio-stop` / `audiobook-stop` | "Stop" / "Ember stop" over playback from Ember's own speaker pauses it through the offline wake words (needs internet) |
| `eye-states` | `[EYES] Ready` at start, no lost sensors, and the state sequence that drives the eyes |
| `cpu-ceiling` | Mean and peak CPU (% of all cores) and resident memory of the runtime during a turn with a card |
| `restart-engaged` | A restart after "Ember disengage" comes back engaged |
| `restart-recovers` | Stop and start again; connected within 150 s, and it answers |

Step kinds:

- `say`: synthetic speech on the person speaker. Optional `distance_m` or `level_db`,
  `voice`, `timeout_s`, `observe_s`.
- `noise`: `tv`, `music` or `pink`, for `seconds`, at a `level_db`.
- `show` (card lines), `show_image`, `clear_display`.
- `wait` (seconds).
- `restart`.

Any step can take `wait_for` with `state`, `log` or `event`, plus `timeout_s` and
`after_s`.

Robot-only expectations:

- `max_false_wakes`, `no_user_speech` (`silent` is shared with Layer 1)
- `log` / `not_log` / `startup_log` / `event` / `not_event` (regex)
- `states` (ordered), `engaged`, `no_echo`
- `max_cpu_percent` / `max_cpu_peak_percent` / `max_rss_mb`
- `ready_within_s`

**Requirements gating.** As in Layer 1's `requires`, a scenario is skipped when the
branch lacks its feature. The checks are:

- `tools`, `any_tools` and `prompt`, from the branch's own session config. `probe.py` runs
  in the branch's interpreter and builds the config from the core that the branch's
  `run_pc.py` names.
- `files` that must exist.
- `env`: the effective value from the rig's `.env` plus the branch's launcher defaults,
  with `env_defaults` for the code default.
- `display`.

If the branch's config can't be read (for example, a core whose prompt is built in a
way the AST reader doesn't understand), the scenarios that depend on it are skipped
and the reason is reported. The rig never guesses.

**v10's address gate.** On mvp-v10 the runtime plays a reply only when it was meant for
Ember (`features/conversational_ability/addressing.py`). A dropped reply never reaches
`.runtime-status.json`, so the rig judges what the person heard. It reads the gate's
`[ADDRESS] Not spoken to; dropped reply: ...` log line, so a step that needed an answer
fails with "the address gate dropped Gemini's reply ..." instead of "no reply". Lines in
`scenarios.json` name Ember wherever the previous reply may be more than 20 s old
(`BAYMAX_FOLLOW_UP_SECONDS`). `conversation-latency` keeps a follow-up without the
name, and `overheard-conversation` waits out the follow-up window, then checks that a
line people say to each other gets no spoken reply. The probe reports whether the gate
is on (`address_gate`), and the disclosure prints it.

**Runs.** One runtime serves the suite. It is restarted before any scenario that follows
a failure or error, and when a scenario sets `fresh_runtime`. Conversation context
carries over between passing scenarios. Limits apply at every level: per step
(`timeout_s`), per scenario (`timeout_s`, default 600 s), for the suite
(`--max-minutes`), and in the workflow. When the suite limit hits, the scenario it
cut off is an error that keeps the steps it finished (and their failures), the rest are
"not run", and the verdict is FAIL. The runtime is always stopped at the end: first
through the monitor's stop file (a clean shutdown), then SIGTERM, then SIGKILL. A job
cancel (SIGINT, then SIGTERM 7.5 s later) stops the suite at the first signal; later
signals are ignored, so stopping the runtime, deleting recordings and writing the
report always finish.

## Physical setup

**Shopping list** (for one test robot):

- A second complete robot, identical to production: board, the same USB microphone,
  speaker and camera, eyes. Use #22's image.
- **Person speaker:** a small powered USB speaker that shows up as its own sound card,
  with a name that differs from Ember's speaker. Do not use another Jieli "UACDemo":
  the rig refuses any speaker that matches Ember's. Reasonably flat response; about
  $25–40.
- **Display:** a cheap Android tablet or a 7–8" screen with a stand, mains-powered, with
  sleep, notifications and auto-brightness off.
- **SPL meter:** Class 2, A-weighting, slow response; about $30–60. It is needed for
  calibration only.
- A mic stand or tripod for the speaker, a tape measure, and a powered USB hub. Wired
  ethernet is preferable.
- A room or closet that can be closed and kept empty during runs.

**Placement:**

- Put the person speaker **2 m** from the robot's microphone, at seated head height
  (about 1.1–1.2 m), pointed at the robot. Keep it at least 1 m from Ember's own speaker
  and not directly behind the robot.
- Put the tablet 40–60 cm in front of the camera so a card fills about two-thirds of the
  frame. Check the framing once on devUI's camera test.
- Keep the room as it is. Soft furnishing helps. Moving the speaker or changing its
  volume invalidates the calibration, and the next run detects that.

**Levels.** Level 0 dB is a person talking normally at 1 m: **60 dB(A) at the robot's
microphone** (`target_spl_dba`).

- `distance_m: 3` plays at −9.5 dB (inverse square law, about 50.5 dB(A)).
- The TV scenario plays at −3 dB (about 57 dB(A), TV at a normal listening level).
- Change `gain_db` or the speaker's hardware volume, never one of these per-scenario
  levels, to calibrate.

**Calibration** (once, and after moving anything):

1. Hold the SPL meter at the robot's microphone, pointed at the person speaker.
2. Run `python evals/robot/rig.py --root <installed or any checkout> --calibrate`. It
   starts the runtime (so it connects to Gemini and sends the pink noise, which is not
   speech) and plays 12 s of pink noise at level 0.
3. Enter the meter's reading. If it is more than 1 dB from 60, the rig prints the
   `gain_db` to set in `/etc/ember/robot-rig.json`. Repeat until it is within 1 dB.
   The rig always plays at the config's `gain_db`.
4. The rig saves `calibration_file`: the gain, the SPL, and the robot's own mic level
   (median of the runtime's `[MIC LEVEL]` lines, in dBFS).

Every suite run starts with a 6 s calibration check. If the robot's mic hears the rig
more than 4 dB away from the saved level, the run stops with a **rig problem** (speaker
moved, volume changed or mic fault), so a PR is not blamed for it. So does a config
`gain_db` that differs from the one saved: calibrate again after changing it. Without a
calibration file the run proceeds and the report says "not calibrated".

## Runner registration (one-time, by a repository admin)

> **JoeKojima/Baymax is a public repository.** GitHub advises against self-hosted runners on public repositories, because PR code runs on the machine. This workflow only runs same-repository PRs labelled by someone with write access, and inside an environment that should require the software lead's approval. Before registering the runner, also:
> - set Settings → Actions → "Require approval for all external contributors";
> - give the test robot no other credentials, family data or network access beyond Gemini and GitHub;
> - re-image it with #22's card if a run ever looks wrong.
>
> If that's not acceptable, keep the rig and trigger it by hand on the robot (`rig.py --root <checkout>`) instead of registering a runner.


These steps add a new service for the runner. They do not change Ember's own systemd
units. The software lead approves them.

1. On the test robot:
   - `sudo mkdir -p /etc/ember && sudo touch /etc/ember/test-robot` (the marker).
   - Copy `rig.example.json` to `/etc/ember/robot-rig.json` and set
     `person_speaker.name` (see `python -m sounddevice`), or set
     `person_speaker.command` (`["pw-play", "--target", "<sink>", "{wav}"]` or
     `["aplay", "-D", "plughw:CARD=<card>", "{wav}"]`) if PipeWire hides the device
     from PortAudio.
   - Install `espeak-ng`, or set `tts_command` to a better voice such as Piper:
     `["piper", "--model", "/opt/piper/en_US-lessac-medium.onnx", "--output_file", "{wav}"]`.
     The text arrives on stdin.
   - Keep no key in the installed release's `.env`, and `touch ~/.config/ember/no-autostart`.
2. In GitHub, go to Settings → Actions → Runners → **New self-hosted runner** (Linux,
   ARM64). On the robot, as the `ember` user (it owns the PipeWire session and the
   audio devices), run:
   `./config.sh --url https://github.com/JoeKojima/Baymax --token <token> --name ember-test-1 --labels ember-test-robot --unattended`
   then `sudo ./svc.sh install ember && sudo ./svc.sh start`.
3. Under Settings → Environments, create **`ember-test-robot`**:
   - Add the secret `GEMINI_TEST_KEY`. Use a key from a separate Google Cloud project
     with a budget alert and a low quota limit, not the production key.
   - Add the software lead as a **required reviewer**, so every run waits for approval
     before the key is released.
   - Allow deployments only from branches of this repository.
4. Create the **`robot-test`** label. Labelling a PR queues a run. To run again, remove
   the label and add it again.
5. Register **only one runner** with the `ember-test-robot` label per robot. The queue
   below depends on it.
6. Under Settings → Actions, keep "Require approval for fork pull request workflows".
   The job's `if:` and the guard step refuse forks anyway.

## Submitting a run and the queue

Anyone with write access can put a branch on the robot, like submitting a job to a
cluster:

- **A PR:** add the `robot-test` label. The report is posted on the PR.
- **Any branch, no PR needed:** Actions → **Robot test** → **Run workflow**. Enter the
  branch, optionally a comma-separated scenario list (`radio-stop,eye-states`), and
  optionally a PR number to post the report on. The report is always in the run summary.

What "your environment" means: the code on your branch, the scenarios you pick, and
the robot's own device settings. Not an uploaded folder or image. Only branches of
this repository can run, because the code gets a live microphone, camera and Gemini key.

**How the queue behaves:**

- The robot runs one job at a time. The others wait (shown as "Queued" in Actions),
  roughly in the order submitted, for up to 24 hours.
- **Nobody can bump anyone else.** Each PR, and each person's branch, has its own
  concurrency group. Submitting again replaces only *your own* waiting run for that
  PR or branch, never someone else's, and never a run in progress. (A single shared
  group would not work: GitHub keeps one waiting run per group and a newer submission
  cancels it.)
- With the environment's required reviewer, each job also waits for the software
  lead's approval before it starts.
- **Cancelling:** GitHub lets anyone with write access cancel any run. Cancel only your
  own; the cleanup step stops the runtime and deletes recordings either way.
- **Not provided:** a queue position, per-person limits or reservations. If the robot
  gets busy enough to need those, the next step is a small job service that the robot
  polls (a lease with a heartbeat, first in first out with per-person limits, cancel by
  submitter or admin only). That would be new infrastructure, so it needs the software
  lead's approval.

**Who can trigger a run** (enforced by the workflow and `safety.py`):

- The event must be a `labeled` event with `robot-test`, or a "Run workflow" submission.
- A submission must name a plain branch of this repository (no `refs/...`, which could
  point at a fork's PR head), and a valid scenario list and PR number.
- The PR's head repository must be this repository. Forks never run.
- The PR must be open, and its author must be OWNER, MEMBER or COLLABORATOR.
- The labeller or submitter must have write, maintain or admin permission (a read-only API lookup).
- The `/etc/ember/test-robot` marker must exist.

The rig code comes from the default branch, so a PR can't weaken its own checks. Until
this layer is merged, the PR's copy is used, with a warning. The PR's code still runs
with the test key and can see the robot's devices, which is why the key is spending
capped and runs are limited to collaborators.

## Privacy and data flows

- **Sent to Google Gemini Live (by the runtime, as in production):**
  - microphone audio of the room, which should contain only the rig's synthetic speech,
    synthetic noise and Ember's own voice;
  - camera frames, which should show only the tablet's synthetic cards;
  - the MVP prompt and tools.

  The rig itself sends nothing to Gemini. **The room must be empty of people and
  private material.** The rig prints this before every run and asks for confirmation
  unless `--yes` is given (the workflow passes it; the operator keeps the room
  dedicated).
- **Real toolkit calls happen.** The runtime is real, so weather, radio and LibriVox
  providers are contacted as in production.
- **Recordings.** The v8/v9/v10 runtimes always write raw microphone audio to
  `day_utterance/seg_*.wav`. There is no switch to turn this off. `BAYMAX_AUDIO_RETENTION_DAYS<=0`
  *disables pruning* and keeps everything, so do not set it to 0.
  - The rig sets `BAYMAX_AUDIO_RETENTION_DAYS=0.01` and `BAYMAX_VOICE_ANALYSIS=0`.
  - **v10 keeps all of a run's data in a private folder.** The rig sets
    `BAYMAX_DATA_DIR` to a new 0700 folder `.ember-rig-data-*` in the checkout. Raw mic
    segments, session audio, transcripts, the ChromaDB memories that the shutdown
    summary writes, reminders, family messages and the cloud queue all go there, and
    the folder is deleted when the run ends (and by `--cleanup`). It's on disk next to
    the code, not in `/tmp`, because Debian 13's `/tmp` is RAM and a long run's session
    audio is hundreds of MB. The test robot's real memories and reminders are never
    read or changed.
  - **Nothing goes to the cloud or the dashboard.** The rig blanks `BAYMAX_DEVICE_KEY`
    (no tadashirobotics.com uplink, no Ember app link that would read real family
    messages aloud or post synthetic reminders), and sets `BAYMAX_LOCAL_URL` to a closed
    port. Otherwise synthetic falls and transcripts would reach the local dashboard,
    whose fall alerts email every paired account. `BAYMAX_DEVICE_KEY` and the
    `BAYMAX_EMAIL_*` settings are never copied from `base_env` (`run_pc.py` refuses to
    start with email settings).
  - After every run it deletes `day_utterance/seg_*.wav`, `session_*.wav`,
    `od_session_*.wav` and `transcript_*.txt` written since the run started, and
    reports the count. Any file left behind turns the report into a rig problem.
  - The workflow's always-run cleanup step repeats this if the job is cancelled.
- **The key.**
  - `GEMINI_TEST_KEY` goes into a 0600 file in a private temporary folder. The branch's
    `.env` only names that file (`GOOGLE_API_KEY_FILE`).
  - Both are removed at the end, and an existing `.env` is restored.
  - The runtime process gets no GitHub or Actions tokens.
  - The report is posted by a separate step, so the PR's code never runs alongside
    `GITHUB_TOKEN`.
- **Reports.**
  - `robot-test.md` and `.json` contain the scripted lines, what the runtime transcribed
    of them, tools, states, latencies and CPU.
  - Log excerpts in the report leave out transcript lines.
  - The full runtime log (`runtime.log`) stays on the robot, in the job's temporary
    folder (deleted with the job) or `.local-review/robot/` (gitignored), and is never
    posted.
  - No audio or images are kept.

## Inputs, outputs, dependencies

- **Inputs:**
  - the branch checkout (`--root`);
  - `/etc/ember/robot-rig.json` (see `rig.example.json`), the calibration file and
    `scenarios.json`;
  - `GEMINI_TEST_KEY`;
  - for posting, `GITHUB_TOKEN` and `--pr`.
- **Outputs:** `<report-dir>/robot-test.md` and `.json`, and `runtime.log`. With
  `--post-comment` (rig) or `report.py --post` (workflow), one PR comment. Exit codes:
  0 pass, 1 fail or not confirmed, 2 refused.
- **Dependencies:**
  - the branch's own `.venv` (numpy, opencv, sounddevice, python-dotenv, google-genai
    for the probe);
  - `espeak-ng` or a `tts_command`;
  - `ps`, and psutil (optional: `/proc` is used otherwise).
  - `safety.py` and `report.py` use only the standard library, so the workflow runs
    them with the system `python3`.
- **Enabling and disabling:**
  - Nothing in the runtime imports the rig.
  - To enable, register the runner and create the label and environment.
  - To disable, take the runner offline, remove the label, or delete
    `/etc/ember/test-robot`; the rig then refuses.

## Tests

`tests/test_robot_rig.py` (core section, offline) uses fakes from `fakes.py`:

- a clock that advances on sleep;
- a speaker that records what it plays;
- a display;
- a CPU sampler;
- a scripted `FakeRobot` that writes `.runtime-status.json` and a log in the runtime's
  formats.

Covered:

- **Scenario file:** validation, and coverage of the hardware checks.
- **Gating:** including a real probe of this branch, where `barge-in` is skipped
  because the MVP turns interruptions off.
- **Ideal robot:** every scenario in `scenarios.json` passes against one.
- **Failure cases:**
  - a false wake during TV, and audio reaching Gemini while disengaged;
  - echo, slow first audio, and a reminder that never re-engages;
  - a runtime crash (error, then a fresh runtime for the next scenario);
  - a runtime that never connects, and the CPU or memory ceiling;
  - a sticky disengage after restart, and the suite time limit.
- **Calibration:** pass, drift detection, and saving only when on target.
- **Observer:** event deduplication, ignoring the previous session, partial log lines,
  engagement reset on restart.
- **Safety:**
  - the marker, the trigger guard (forks, labels, permissions) and the key and `.env`
    lifecycle;
  - deletion of only this run's recordings;
  - token stripping from the runtime environment.
- **Report rendering and posting:** only with the flag.
- **`rig.main` end to end** with fake hardware: refusal without the marker or
  confirmation, dry run, missing key, cleanup.
- **Real hardware classes with stand-ins:**
  - the speaker chosen by name, which refuses Ember's own;
  - the HTTP display on localhost;
  - finding other runtimes;
  - stopping a dummy runtime through the stop file, and with SIGTERM;
  - the CPU sampler.

No microphone, camera or speaker is opened, and nothing contacts Gemini or GitHub.

## What still needs a human

- **Setup and approval:**
  - buying and placing the hardware, calibrating, registering the runner;
  - approving each run (environment reviewer);
  - tuning the CPU and memory ceilings from the first baseline run (the current numbers
    are placeholders).
- **Things the rig cannot see:**
  - **What the eyes look like.** The telemetry records eye *expressions* (on some
    branches) but not the displayed mode, so `eye-states` checks startup, sensors and
    the driving states only. Look at the eyes, or add a second camera later.
  - **How Ember sounds** (volume, distortion) to a listener, and fall detection.
- **Real voices.** Synthetic speech differs from people. Layer 1 found that Gemini
  transcribed synthetic "Ember, disengage." as ".". A robotic espeak voice through a
  speaker may fail the same way, so use a natural `tts_command` voice. Check any failure
  of a transcript-matched command with a real voice before blaming the PR.
- **Judging failures.** A run is evidence for the reviewer. Gemini is
  non-deterministic, so one pass or fail needs judgement. Re-run if in doubt.
- **First live use.** None of the hardware path has run on a robot yet. The workflow,
  `prepare_checkout.sh`, sounddevice and command playback, the HTTP display on a tablet,
  `[MIC LEVEL]` calibration, the regexes for real `[WAKE]`, `[ENGAGE]`, radio and
  audiobook output, and stopping a real `run_mvp.py` all need a supervised first run.
