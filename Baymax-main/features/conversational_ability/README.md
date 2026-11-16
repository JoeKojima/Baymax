# Conversational ability

Responsive turn-taking and local microphone processing, independent of memory or face recognition.
Integrated with desktop v8 and robot v10.
The existing camera/audio streams and 48 kHz rates stay in the runtime. This
module opens no devices and makes no external requests.

## Why

The old v10 sender discarded every microphone packet while playback was buffered,
so spoken interruptions and quick yes/no answers during a reply never reached
Gemini. The desktop enhancement sends those packets with a WebRTC AEC3 echo
canceller using the actual speaker callback PCM as the reference. Background
noise suppression, high-pass filtering and AGC2 volume control run before Gemini
input. Raw recordings for existing voice analysis remain unchanged.

The opt-in session configuration uses low speech-start sensitivity, zero required
speech-start padding for brief words, and 500 ms end-of-turn silence. There is no
local speech/noise gate dropping packets. This changes speech detection, not
speaker identity, transcription models or the conversation prompt.

The lower speech-start default reduces eagerness to interrupt the robot when
sound is detected; it is not a guarantee against echo or noise. Intentional
interruptions may require clearer speech. To restore the earlier setting, set
`BAYMAX_SPEECH_START_SENSITIVITY=high` in private `.env` and restart. The default
is `low`; invalid values warn and fall back to low. End-of-turn silence remains
500 ms, and echo cancellation/noise reduction are unchanged.

## Inputs, outputs and dependencies

`processing.py` takes mono int16 mic samples and the already-rendered speaker PCM.
It processes whole 10 ms frames with retained leftovers and 10 ms output buffering;
it never zero-pads/discards a tail on every 1024-sample callback. Reference storage
is bounded to 250 ms and missing reference is silence. Capture/output stream
callback ADC/DAC timestamps provide the AEC delay hint, falling back to stream
latencies when callback timestamps are unavailable. The capture thread alone owns native DSP;
the output callback only appends to the reference buffer.

Dependency: `pywebrtc-audio==0.2.0` (Apache-2.0), included in the isolated
`requirements-pc.txt` environment. Native processing is imported only when
enabled. It adds no provider, cloud data flow, saved speaker embeddings or capture
stream. Gemini receives the processed mic audio; the existing raw recording path
is preserved. Monitor telemetry contains levels/clipping/probability only.

## Controls and fallback

A 4th-order 200 Hz high-pass (`BAYMAX_FAN_FILTER_HZ`, `0` disables) runs first,
before AEC/noise suppression/AGC. On ember-002 the fan put 95% of its noise power
below 211 Hz (29 Hz rotation, 88/193 Hz tones); filtering a recording removed
12.4 dB of noise while 300-3400 Hz speech changes by under 1 dB. It needs
`scipy` (installed with `librosa`); if unavailable or failing, cleanup continues
without it.

MVP sets `BAYMAX_INTERRUPTION_ENABLED=0` by default. Gemini receives microphone
audio during replies when DSP is healthy, with `activity_handling=NO_INTERRUPTION`
so detected speech does not cut off the current model response. The audio sender,
AEC and residual subtraction remain active. Set `BAYMAX_INTERRUPTION_ENABLED=1`
in private `.env` and restart to restore speech-triggered interruptions. Other
launchers retain their existing default. The no-interruption option also applies
with enhancement off, but that existing raw path still skips buffered-playback
mic packets; failed DSP retains its protective half-duplex fallback.

With interruptions off, `BAYMAX_MUTE_MIC_DURING_REPLY` defaults to `1`: AEC keeps
adapting on the captured audio, but Gemini receives silence while the speaker,
its device buffer and the 250 ms echo tail are active, so leaked robot speech
cannot become a later user turn. Speech during a reply is not heard; set it to
`0` to restore full-duplex input. Without the mute, leaked echo
can still be recognized as input. Matching a transcript afterward cannot remove
audio already processed by Gemini. We retain transcript warnings rather than
delete phrases a person may intentionally repeat. Echo subtraction is an acoustic
estimate and cannot guarantee separation of overlapping voices.

`run_pc.py` enables `BAYMAX_AUDIO_ENHANCEMENT=1` by default; Setup exposes the feature toggle and noise level. Set it to `0`
in private `.env` for the previous raw/half-duplex behavior. Robot/direct v10
startup keeps enhancement off unless explicitly enabled. Noise suppression level
`BAYMAX_NOISE_SUPPRESSION_LEVEL` is 0–3 (default 1, moderate). Restart to apply.
AGC is limited to 18 dB adaptive gain while the speaker is quiet. Positive AGC
boost is removed from the cleaned PCM while non-silent speaker output, its
reported device-buffer delay and a 250 ms echo tail are active. This prevents
the gain stage from re-amplifying residual echo after AEC. Healthy DSP still
passes near-end speech; it does not mute all microphone input during replies.
Quiet interruptions may receive less gain while the robot speaks and need
physical-device testing. These settings can reduce noise, but do not
repair microphone clipping. If native processing fails, raw capture continues
with a visible half-duplex failure state. In this failure mode only, microphone
input to Gemini is zeroed while rendered audio and its tail are audible, even
when the Python playback queue has drained. Raw session recording is unchanged.

Monitor's Microphone card shows raw/clean RMS dBFS, raw clipping percentage, speech
probability, gain, and whether listening during replies is active. Probability
is an estimate, not proof of a particular speaker or transcription accuracy.
Monitor also shows **Speaker echo protection: active** and the AEC delay hint.
Reported `gain_db` is the effective applied gain, while `agc_gain_db` retains
the native processor's gain for diagnostics. These are processing-status
indicators, not measurements that certify successful physical cancellation.

### Residual echo and double talk

`echo.py` adds a conservative check after WebRTC processing and gain protection.
It retains at most 600 ms of the actual speaker PCM and 40 ms of cleaned input,
in memory only. An FFT-based search looks across recent speaker audio for a
delayed match, then refines the match at the full sample rate. This search is
repeated as sound paths change; no fixed speaker/microphone distance is assumed.

Only correlation >=0.85 on two compatible delay estimates enables subtraction.
The fitted linear speaker component is reduced by 90%; unexplained audio is
retained. Strong double talk or a poor match passes unchanged. Quiet independent
voice mixed with dominant echo retains its independent component rather than
being discarded by a whole-frame speech/noise gate. The model is not proof of
speaker identity and can be wrong: nonlinear/distorted echo, strong reverberation,
clipping, unusually long delays and correlated human audio remain limitations.
Room movement, quiet interruptions and CPU cost need physical target testing.

Monitor reports residual state, correlation, estimated relative delay and
unexplained fraction. These are signal-model estimates, not an emotion, user
identity or recognition-accuracy score. Missing/stale references are passed
through; a residual-detector error disables this extra layer while retaining
healthy WebRTC. Set `BAYMAX_RESIDUAL_ECHO_ENABLED=0` in private `.env` and restart
to disable this layer for comparison without disabling primary AEC.

The debugger also checks a user-transcript prefix against Ember transcripts
updated within eight seconds. At least three matched words, 20 matched characters
and 80% prefix overlap are required for a warning. This is diagnostic only:
Gemini may already have detected an interruption before text arrives. No user
text is discarded, rewritten or blocked, including when a person quotes Ember.
No extra model requests, capture streams, files or identity inference are added.

## Speaking only when addressed (`addressing.py`)

With `conversation.only_when_addressed` (default on), proactive audio and the
`[WHEN TO SPEAK]` instruction ask Gemini to stay quiet, but on ember-002 it still
answered `<noise>` ("You look quite tired") and a phone call it overheard. So the
robot decides too, from Gemini's own input transcription. A reply plays only if:

- the person said "Ember" (or a usual mishearing: Amber, Emba, Embar, Emmer,
  Emperor; also split by transcription as "A mber" or "E mperor"; Hindi in
  Devanagari: एम्बर, एंबर, एमबर, ऐम्बर, अम्बर, अंबर, also split as "एम् बर").
  अंबर is also the Hindi word for sky, so it can occasionally let an overheard
  reply through, as "amber" can in English, or
- they started speaking within `BAYMAX_FOLLOW_UP_SECONDS` (default 20) of Ember's
  last played reply ending, counting audio still buffered; noise tags such as
  `<noise>` are not speech, or
- it answers one of Ember's own messages: any `send_client_content` with
  `turn_complete=True` (fall alert, reminders, startup/initiation, audiobook
  notes), within 30 s, or the reply after a tool call from an addressed turn.

Gemini sometimes ends a turn without replying and answers in the next one, so
speech from a turn with no reply (nothing played or held) still counts for the
next reply for 10 s after the person's last words (`CARRY_S`). A played reply
clears it.

The decision is made at the first reply audio. Transcription can trail the reply,
so an undecided reply's audio and transcript are held and released in order if
the name arrives before the turn ends; otherwise they are dropped and
`[ADDRESS] Not spoken to; dropped reply: ...` is logged. Dropped replies never
reach the speaker, Monitor, dashboard transcript or session summary.

Limits: Gemini is not told a reply was dropped, so it may refer to it later. Tool
calls still run for overheard speech (only their spoken answer is dropped). A
conversation must start with the name. Transcription errors on the name drop a
reply that should have played; extend `NAME` in `addressing.py` if one recurs.

Inputs: Gemini's input transcription, reply audio/transcript and the messages
Ember sends on the existing session. Outputs: played or dropped replies, a log
line. No device, model, file or network request is added. Disable with
`only_when_addressed = false` in `device.toml` (also turns off proactive audio)
or `BAYMAX_ADDRESS_GATE=0` in `.env` (keeps them). Tests:
`tests/test_address_gate.py` (fake clock; the 2026-10-21 robot transcript through v10's
real receive loop).

## Validation and limits

### Long sessions: compression and resumption (`session.py`)

Google limits a Live session without compression to 15 minutes of audio (2 with
video; Ember sends a frame every 3 s), resets each connection after about 10
minutes, and re-reads the whole growing history every turn. `session.py` adds
`context_window_compression` (sliding window: trims to half once the context
passes `BAYMAX_CONTEXT_TRIGGER_TOKENS`, default 32000) and `session_resumption`.
The receive loop hands every resume handle and GoAway warning to
`on_server_message`; the next connection's config resumes that conversation. A
handle that fails once (no new handle arrives after using it) or is older than
110 minutes is dropped, so a bad handle cannot block reconnecting.
`BAYMAX_CONTEXT_COMPRESSION=0` / `BAYMAX_SESSION_RESUMPTION=0` disable each.
No new data leaves the robot: both are settings of the existing Gemini session.
Tests: `tests/test_session.py` (fake handles; full config validated by the SDK).

### Local waiting acknowledgement (MVP)

Off by default (testers found the fixed "hmm" unsettling); set
`BAYMAX_WAIT_CUE_ENABLED=1` to turn it back on. When enabled, after more than
two seconds since the latest user-transcription fragment, one short local
“hmm” is played while waiting for the real response. It does not repeat during
that wait. This uses the same wait estimate as the eyes, not an acoustic
end-of-speech detector. A fragment arriving later postpones/cancels the cue.

The clip goes through the existing 48 kHz speaker callback and is included in
the AEC render reference. No second output stream or model call is introduced,
and nothing is programmatically inserted into transcripts or memory. Real
reply PCM takes precedence on the next output callback; detected near-end
speech from the cleaned microphone PCM, interruption/end, reconnect and stop
cancel the cue. Audio already buffered by the OS/device cannot be recalled.

Working echo cancellation is required; if it is unavailable or fails, the cue
is silent and ordinary conversation continues. Set `BAYMAX_WAIT_CUE_ENABLED=0`
in private `.env` to disable. Direct `run_pc.py` keeps it off unless enabled.
The bundled synthetic local voice differs from Gemini; see
[asset provenance](assets/README.md). Speaker volume, residual echo, speech
interruption and perceived timing require a live robot check.

`tests/test_waiting_cue.py` has 12 fake-clock/PCM checks, including the actual
v8 output and microphone callbacks, echo-reference delivery, priority/reset,
one-shot behavior, disabled mode and bad-asset fallback. The guarded hooks are
normalized by upstream parity tests; prompt, models, sample rates, raw recording
and disabled audio behavior remain covered.

### Ember's personality (`features/persona.py`)

Conversations had become dull: the persona asked for a calm, soft, even voice, "not
cheerful by default", and Gemini answered with generic validation and a stock question
("That sounds lovely. It's always nice to have family visit."). The persona now has:

- `[YOUR PERSONALITY]`: warm, curious, quick-witted and a little playful, with a cat's
  mischief and dignity; witty rather than apologetic about being a robot; one quirk used rarely (judging things like a cat would).
- `[YOUR VOICE]`: lively and expressive (varied pitch, pace and emphasis, a smile or a
  laugh when something is funny), quieter and tender for tiredness, sadness, pain and
  late hours; never a presenter's or a salesperson's voice, no gushing.
- `[YOUR FLAIR]`: start with the actual reaction, not a stock opener ("Oh no", "That
  sounds lovely", "That's the spirit"); add one spark (an observation, a surprising fact,
  one concrete idea, a "what if", a tiny aside about its own life); specific over
  general; play along with jokes; no therapist-speak; no jokes about pain, grief, fear,
  loneliness or health worries; still one to three spoken sentences.
- `[HOW TO TALK]` keeps every turn-taking, question and safety rule; its lines now point
  to the lively base and to `[YOUR FLAIR]`.

Measured 2026-11-15 with this branch's real session config (`evals/live_eval.py`'s
builder) on the x86 line's model, gemini-2.5-flash-native-audio: 8 scripted three-turn
conversations (morning after a bad night, groceries, boredom, "tell me something
interesting", a granddaughter's visit, salty soup, Sinatra and a late husband, a sore
knee), the old and new persona each, then a blind A/B judge (`gemini-3.6-flash`; 3.8 Flash was
overloaded that day; order shuffled, 1-10):

| | Flair | Warmth | Appropriateness | Naturalness | Preferred |
|---|---|---|---|---|---|
| gemini-2.5-flash-native-audio, before / after | 4.1 / 7.5 | 5.9 / 8.0 | 8.0 / 8.3 | 5.6 / 7.4 | after in 7 of 8 |

The first run scored appropriateness lower (8.3 to 7.6): Ember asked "What kind of soup?"
after being told, and its late-husband reply was judged incomplete. `[HOW TO TALK]` then
gained two lines (never ask about something already told; a loss gets a gentle, simple,
short reply), and those two conversations were run again (the table). Gemini 2.5 still
opens with "Ah, a classic!" or "That's the spirit!" now and then despite `[YOUR FLAIR]`.
The same persona text on Pallas-Robotics/Baymax was preferred 8 of 8 on gemini-3.8-live
(flair 3.5 to 7.9). The reply audio's pitch and loudness variation did not change
measurably, so the livelier voice shows in what Ember says more than in how the audio
model delivers it; 2.5 still opens with stock interjections more often than 3.8.

Follow-up after a live test on a Mac (2026-11-16: "more fun, but not funny, doesn't crack
jokes, makes a comment and ends it there"):
- `[YOUR HUMOR]`: in a light moment every reply has at least one line meant to make
  them smile or laugh (exaggeration, absurd comparison, wordplay, deadpan, mock
  outrage, a silly "what if", a callback, a joke at its own expense), invented from
  what they said, never explained, never about pain, grief, fear or health.
- `[KEEPING IT GOING]`: end almost every reply with a hook that hands the turn back (a
  question about them, a playful challenge, a "would you rather", a teaser, an opinion
  to argue with), varied and specific; none when they are wrapping up. The `[HOW TO
  TALK]` line "Stop after your contribution" now says to hand the turn back first.
- A version with three example exchanges scored no better (humor 4.8 vs 5.9 on 2.5), so
  there are no examples.

Same 8 conversations, judge `gemini-3.6-flash` with humor and engagement added, the
first personality version against this one:

| Model | Humor | Engagement | Flair | Appropriateness | Replies ending in a question | Preferred |
|---|---|---|---|---|---|---|
| gemini-2.5-flash-native-audio | 3.3 / 5.4 | 5.9 / 8.4 | 5.0 / 7.0 | 7.4 / 7.7 | 50% / 92% | new, 6 of 7 judged |
| gemini-3.8-live | 4.8 / 6.3 | 5.0 / 8.9 | 6.0 / 7.5 | 8.3 / 8.4 | 17% / 88% | new, 7 of 8 |

Humor stays moderate on 2.5 (its best lines are wry comparisons, not real jokes); 3.8 is
wittier. Replies grew to 36-41 words, and 2.5 once invented a memory ("I remember when
the family I study...").

### Choosing Ember's voice (`voice_choice.py`)

People can pick the voice Ember speaks with by asking: "Ember, can you use a deeper
voice?", "talk like a woman", "try a different voice", "which voice is that?", "go
back to your usual voice". Gemini calls the `change_voice` tool with one of Gemini
Live's 30 prebuilt voices, chosen from a catalogue in the instructions: Google's
one-word style for each, grouped by pitch measured on `gemini-3.8-live` (librosa pYIN,
2026-11-15): 16 deep, men's voices (Alnilam about 106 Hz to Sadachbia), 3 in between
(Pulcherrima, Gacrux, Laomedeia, 137-172 Hz) and 11 higher, women's voices (to Erinome,
about 253 Hz). Unsure, Ember suggests two or three.
- **From the Ember app:** the dashboard's settings may carry `voice` (a name, or
  `default`), applied like the microphone, camera and volume (`features/device_settings.py`,
  newer `rev` only, `changed_by` named when Ember tries the voice out: "Jane chose it for
  you in the Ember app"). Every sync reports the voice in force as `settings.voice`. The
  website needs a matching field; the names are the 30 below.

- **The switch.** Gemini fixes a session's voice when the session starts, and a
  resumed session keeps it: resumed with Charon it still spoke with Kore's 214-240 Hz,
  while a fresh Charon session was 110-175 Hz (gemini-2.5-flash-native-audio and
  gemini-3.8-live, 2026-11-15). So Ember says one short line ("Alright, switching my
  voice now"); `voice_switch_monitor` in `realtime_gemini_10.py` waits until that line
  has played (or 8 s passed) and nobody has talked for 1.5 s, drops the resume handle
  and ends the connection; `run()` reconnects at once, without the PortAudio reset. The
  fresh session's instructions carry the last 16 turns (at most 3,000 characters) as
  `[CONVERSATION SO FAR]`, and Ember tries the voice out loud once (`[VOICE CHANGED]`, a
  session task). Once that session sends its own resume handle, later reconnects resume
  it as usual. Memories and the family's notes are in every session's instructions anyway.
- **Live check** (this branch's real config and tool code, gemini-2.5-flash-native-audio,
  2026-11-15): asked for "a deeper voice, like a man's", Gemini chose Algieba; the pitch
  went from about 230 Hz to 66-120 Hz and Ember still knew the dinosaur party it had just
  been told about; "go back to your usual voice" called `change_voice` with `default`.
  On Pallas-Robotics/Baymax, 3.8 chose Alnilam (240 Hz to 103 Hz).
- **Saved:** `ember_voice.json` in `BAYMAX_DATA_DIR` (next to the code by default), so
  the choice outlasts restarts. `BAYMAX_VOICE` in `.env` is the usual voice until
  someone chooses; `default` goes back to it. Names are matched forgivingly
  ("archernar" is Achernar); an unknown name changes nothing. An unreadable file is
  ignored; if saving fails, the choice lasts until restart.
- **Core:** offered with the toolkit off too. `BAYMAX_VOICE_CHOICE=0` stops offering it;
  a voice already saved still applies.
- **Data flows:** nothing new. The voice name is part of the existing Gemini session
  config; the recap is the conversation Gemini already heard, kept in RAM only.

Tests: `tests/test_voice_choice.py` (choosing, saving, the catalogue, `persona.voice`
taking the choice, the recap and its size, the try-out, the tool with the toolkit off,
and the runtime's switch at a quiet moment), and in `tests/test_device_settings.py` the app's voice setting.

### "Ember disengage" / "Ember engage" (`engage.py`)

Say "Ember disengage" and Ember gives a short goodbye, shows sleep eyes and stops
hearing the room. Saying "Ember", or anything else, gets no response. Say "Ember
engage" and it says a short hello and listens normally again. While engaged
there is no wake word: people talk to Ember without saying its name.

- **Disengage** is matched locally in Gemini's transcript of the user
  ("Ember, disengage.", "amber dis-engage") through `runtime_monitor.transcript_listeners`.
  Ember's own transcript never counts.
- **While disengaged**, the mic gate (first in `processing.input_gates`, ahead of
  the audiobook) sends Gemini silence, as during audiobooks. The same
  echo-cancelled mic audio goes only to an offline Vosk listener
  (`features/toolkits/wakeword.py`, vocabulary "ember", "engage", "disengage").
  It wakes only on a confident final "ember engage" pair. No new mic stream is
  opened, and the Vosk model is shared with the audiobook listener and loaded
  on first use. The camera's 3 s frames still go to Gemini unchanged.
- **Engage** sends Gemini an `[ENGAGED]` note through the live session so it says
  hello. `[ENGAGE AND DISENGAGE]` in the MVP persona prompt asks for the short
  goodbye and hello. The upstream v8 prompt is unchanged.
- **Ember still speaks first for safety and for things the person set up:** a
  due reminder or a possible-fall check-in engages Ember again so the person can
  answer. Eye-contact and startup greetings stay quiet.
- **Lifetime:** the state survives Gemini reconnects but not a restart, which
  always starts engaged.
- **Fallbacks:** without audio cleanup (`BAYMAX_AUDIO_ENHANCEMENT=0`), there is
  no mic gate, so Ember says disengage is unavailable and keeps listening. If
  the Vosk library or model is missing, Ember engages again at once and says
  so, so it can never be left unable to hear. While the model is still loading,
  the mic stays withheld.
- **Disable:** `BAYMAX_ENGAGE_WORDS=0` (no prompt section, transcript matching ignored).
- **Data flows:** none new. While disengaged, less audio goes to Gemini.
- **PR #20 integration:** retains its reminder occurrences/follow-ups, audiobook
  ducking and split wake-phrase matching, 400 ms MVP default and default-off strict
  address gate. Both built-in v10 and optional toolkit fall check-ins re-engage
  Ember. A failed audio-cleanup processor also re-engages with an unavailable note;
  a new disengage request is refused until cleanup is healthy again. Camera frames
  and existing raw recordings are unchanged: this is not a privacy-off mode.
- **Tests:** `tests/test_engage.py` (transcript matching, gate and audio cleanup,
  wake rules, missing model, no cleanup, disabled, prompt, eyes, live-session
  note) and `tests/test_toolkit_engage.py` (no greetings; reminders and falls
  engage Ember).
- **Live checks pending:** Gemini's transcription of "Ember disengage" in a real
  room, Vosk accuracy for "Ember engage" at a distance and with TV or
  conversation nearby (false wakes), and the goodbye/hello wording. The
  `[WAKE] heard ...` log helps with tuning.

### Audio processing

Sixteen offline checks cover continuous 1024-to-480 framing, speaker reference,
buffer bounds, disabled/failure behavior, short-speech configuration, mic routing
during playback and untouched raw recordings. Native synthetic steady-noise RMS
fell from 502 to 255 with approximately 0.5 ms processing per 21 ms input block
on this Windows PC. The SDK accepts the activity configuration. These synthetic
results do not establish real speech accuracy or echo-cancellation quality.
Actual quiet yes/no, interruptions, speaker leakage/delay and noisy-room behavior
need user testing. Residual echo/noise can still trigger detection; check native
AEC status and microphone clipping/levels in the Monitor.

Primary references: [binding API](https://github.com/strands-labs/pywebrtc-audio)
and [Gemini activity detection](https://ai.google.dev/api/live#AutomaticActivityDetection).

Additional opt-in native regression, using generated signals only:

```bash
python tests/aec_synthetic_probe.py
```

Windows/Python 3.12.15 with pinned pywebrtc-audio 0.2.0: the reproducible
speech-band echo case improved from 11.0 dB to 26.1 dB net RMS reduction.
Independent voiced/syllabic near-end input remains nonzero during double talk
(clean RMS 750.3 with user RMS about 1200). This is not a real speech-recognition
score or a LattePanda acoustic measurement. No device, recording or provider
request is used. The earlier noise-only comparison is historical DSP evidence.

Ten additional offline regressions in `tests/test_residual_echo.py` cover delayed
echo, polarity reversal, changing delay, quiet/strong double talk, missing/stale
references, reset/bounds, error isolation, prefix matching and warning expiry.
The native synthetic probe still reports 26.1 dB echo reduction and non-muted
voiced double talk with the conservative residual layer enabled; it does not
establish an additional reduction on every signal or on the physical robot.
