# MVP (unreleased)

- Remove scenario examples and sample replies from conversation guidance;
  retain the general rules for engagement and sharing the floor.

- Follow newly shared uncertainty or difficulty rather than treating an answer
  as a farewell. Replace generic reassurance and closing wishes with a specific
  question or concrete observation, while still respecting pauses and endings.
  Live conversational evaluation remains pending.

- Balance Ember's conversation around sharing the floor: usually one short
  sentence, one relevant question or observation when there is room to elaborate,
  and brief personality without generic praise or changing the subject to itself.
  Clarify unclear details instead of inventing a topic; avoid routine wellness
  reminders triggered only by late hours. Offline configuration checks do not
  establish live conversational quality; spoken evaluation remains pending.

- Give Ember an explicit point of view instead of relying on a weak “have
  opinions” instruction. It stays receptive but does not automatically agree or
  mirror; it can make a clear claim, give a brief reason, disagree gently and
  change its mind when persuaded. Its grounding values are dignity and
  independence in care, patient attention, dependable understandable technology
  and the continuing relevance of classical literature. These are conversational
  starting points rather than slogans. Medical limits, uncertainty and the ban
  on invented expertise or lived experience remain unchanged. Live personality
  consistency remains pending.

- Replace timer-driven chatter with event-driven silence by default: the old
  silence/face-presence random initiation requires
  `BAYMAX_RANDOM_INITIATION=1`. Gemini's existing proactive audio, conversation
  context and visual cues still judge when speech is directed at Ember; facing
  or looking toward Ember while speaking is supporting evidence, not proof.
  Default nap eyes now appear after 60 quiet seconds instead of 10. A sustained
  scene-change trigger is not added yet; live mixed-room and gaze behavior remain
  pending.

- Give Ember grounded personal interests: it is studying nursing curriculum in
  hopes of becoming a personal caregiver, while remaining explicitly a learner
  rather than a medical professional; it likes sleeping to recharge, classical
  literature and hunting for software bugs online. Clear pain or medical-
  emergency language now forces surprised/exclamation eyes for the next reply,
  above explicit requests and sad reply cues. This is a local visual reaction;
  medical wording, diagnosis limits and alert behavior are unchanged. Live
  personality naturalness and matrix timing remain pending.

- Prevent Ember from interrupting its own replies: MVP no longer opts into
  speech-triggered interruption by default, so the existing reply-time mic mute
  and 250 ms echo tail protect playback. `BAYMAX_INTERRUPTION_ENABLED=1` remains
  an explicit barge-in option after live echo-cancellation validation. Restore
  the startup statement even when ambient noise is transcribed during its initial
  camera wait, and retry it after a failed session send.

- Keep eye expressions local by default. A live blocking `set_eye_expression`
  call occurred after reply generation and made Gemini repeat the same sentence;
  two startup attempts also ended in connection recovery immediately after their
  prompts. Restore the phrase-based one-expression-per-reply path so ordinary
  replies add no model tool round trip. Treat yes/no as well
  as wh-questions near the reply opening as curious, while concern such as “Oh no”
  retains sad eyes. Tell Gemini that its natural opening wording guides the local
  animation, giving it conversational control without hidden metadata or a tool.

- Disable the strict local name/20-second reply gate by default: it suppressed
  legitimate "Tell me about yourself" requests after a conversational pause.
  Existing Gemini proactive audio/instructions still judge whether speech is
  directed at Ember; direct questions do not need its name. No extra classifier.
  `BAYMAX_ADDRESS_GATE=1` restores the old gate. Live background-conversation
  and noise behavior remains pending; model judgment is not guaranteed.

- Turn-taking: MVP waits for 900 ms of silence instead of 500 ms (raw fallback
  previously used 100 ms); speech-triggered interruption remains opt-in.
  Saved settings still win; `BAYMAX_TURN_END_SILENCE_MS` accepts 300-2000 ms.
  v10 cancels queued reply audio before handling output on an interruption
  message, while retaining co-delivered user transcription. Share the speaking
  flag with the monitor/initiation logic instead of a receive-loop local. Added regressions
  for pause validation, overrides and cancellation ordering. Physical echo,
  thinking pauses and spliced-response reproduction remain pending.

- Slightly slower speech via the persona, with brief natural pauses instead of
  drawn-out words. Broaden automatic eye cues for ordinary conversation; default
  open eyes retain only the original blink. Fix missed cues in
  large transcription chunks and curly apostrophes. Explicit requests and
  alert/status priority remain; no extra model call or capture. Physical voice
  speed, matrix timing and perceived naturalness remain pending.

- Omit timezone names and UTC offsets from the spoken startup greeting; retain
  device-local clock context so the time and daypart remain accurate.

- Keep ordinary conversation on the person's topic: remove task/capability
  suggestions from the startup greeting and instruct the persona to offer
  reminders, lists or other services only when they address an expressed need.
  Requested tools and existing reminder announcements are unchanged. Live
  conversation validation pending.

- Startup greeting: supply the actual device-local time, timezone and UTC offset
  instead of asking Ember to act as if waking up. Request a friendly two- or
  three-sentence hello, a grounded camera observation when available, and one
  question or offer to help. No humming or invented places; an unclear view
  prompts a question instead. Wait four seconds for the existing video sender.
  Offline: 267 core + 156 toolkit tests pass on Windows/Python 3.12.15,
  including local clock/daypart and session regressions. Live voice/camera and
  device timezone validation pending. No extra capture or provider calls.

- Eyes sleep 60 s after the conversation goes quiet (was originally 180 s), counted from
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
