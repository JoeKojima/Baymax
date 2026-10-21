# MVP (unreleased)

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
