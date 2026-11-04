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
speech-start padding for brief words, and 500 ms end-of-turn silence outside MVP.
MVP defaults to 400 ms to leave a short pause before answering. There is no
local speech/noise gate dropping packets. This changes speech detection, not
speaker identity, transcription models or the conversation prompt.

The lower speech-start default reduces eagerness to interrupt the robot when
sound is detected; it is not a guarantee against echo or noise. Intentional
interruptions may require clearer speech. To restore the earlier setting, set
`BAYMAX_SPEECH_START_SENSITIVITY=high` in private `.env` and restart. The default
is `low`; invalid values warn and fall back to low. `BAYMAX_TURN_END_SILENCE_MS`
overrides turn-end silence (300-2000 ms; invalid input warns/falls back to 400).
The override also applies without DSP; other raw-path behavior is unchanged.

## Inputs, outputs and dependencies

V10 inserts 180 ms of silence between distinct replies only when the preceding
reply still has queued audio. Chunks within a reply stay contiguous; a reply
after an empty queue starts immediately. Interruption still clears pending audio,
including the gap. This uses the existing PCM buffer and adds no stream, provider
or dependency. Offline coverage: `test_address_gate.py` and `test_v10_runtime.py`.
Audible transitions and device buffering still need live verification.

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

MVP sets `BAYMAX_INTERRUPTION_ENABLED=1` by default so the person can reclaim
the turn. Healthy DSP keeps cleaned microphone audio flowing during replies;
Gemini's interruption notification flushes queued audio before any co-delivered
reply text/audio is processed. New user transcription on that message is retained.
The receive loop updates the shared speaking flag used by initiation and Monitor,
so those consumers see generation in progress rather than a shadowed local value.
Set `BAYMAX_INTERRUPTION_ENABLED=0` in private `.env` for non-interruptible replies.
Saved overrides take precedence. The no-interruption option also applies
with enhancement off, but that existing raw path still skips buffered-playback
mic packets; failed DSP retains its protective half-duplex fallback.

Interrupted audio already buffered in the OS/device cannot be recalled by clearing
the Python queue. Acoustic echo/false interruptions and pause timing need live
robot checks; this change cannot establish that they caused a particular splice.

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

With `conversation.only_when_addressed` (default on), the existing Gemini
session's proactive audio and `[WHEN TO SPEAK]` instruction judge conversational
intent. Direct questions do not require the name; ordinary pauses do not end an
established conversation. Current conversation and visual cues provide context,
not speaker identity; facing or looking toward Ember while speaking strengthens
the evidence but is not treated as proof by itself. The old silence/face-presence
random initiation is off by default; `BAYMAX_RANDOM_INITIATION=1` restores it for
demo testing. No extra classification call, capture or local gaze model.

The strict local transcript gate is now **off by default**. It suppressed two
"Tell me about yourself" requests after the user paused longer than 20 seconds.
Set `BAYMAX_ADDRESS_GATE=1` to opt in; `0` (default) lets generated replies play.
This trades deterministic name gating for model judgment: Gemini may still
misjudge background speech or noise. Mixed conversations need live verification.

When the optional strict gate is enabled, a reply plays only if:

- the person said "Ember" (or a usual mishearing: Amber, Emba, Embar, Emmer,
  Emperor; also split by transcription as "A mber" or "E mperor"), or
- they started speaking within `BAYMAX_FOLLOW_UP_SECONDS` (default 20) of Ember's
  last played reply ending, counting audio still buffered; noise tags such as
  `<noise>` are not speech, or
- it answers one of Ember's own messages: any `send_client_content` with
  `turn_complete=True` (fall alert, reminders, startup/initiation, audiobook
  notes), within 30 s, or the reply after a tool call from an addressed turn.

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

### Ember's voice volume (`volume.py`)

The person can change how loud Ember speaks by asking: "Ember, speak louder",
"softer", "set your volume to 8", "how loud are you?". Gemini calls the
`set_voice_volume` tool (louder, quieter, default, status, or an exact level), and
Ember's short confirmation already plays at the new level.

- **Scale:** 1 to 10. Level 7 is the original loudness and leaves the audio byte for
  byte unchanged, so nothing changes until someone asks. Each step is clearly audible:
  level 1 is about -14 dB, level 10 about +6 dB.
- **Above 7:** gain with a soft limiter from 70% of full scale, so loud passages are
  rounded off instead of clipping harshly. Quiet speech gets the full gain.
- **Never muted:** the lowest level is 1, so a fall check-in can't be silenced by
  accident. A level change ramps across one speaker block, so there's no click.
- **What it changes:** Ember's voice only (Gemini's replies and the waiting cue), on the
  speaker callback, before audiobooks are mixed in and before the echo canceller's
  reference. Echo cancellation therefore keeps matching what is played. Audiobooks keep
  their own volume (the audiobook tool's `volume`). The system speaker level is not
  touched.
- **Saved:** `voice_volume.json` in `BAYMAX_DATA_DIR` (next to the code by default), so
  the level survives restarts. `BAYMAX_VOICE_VOLUME` (1-10) in `.env` sets the level
  before anyone has changed it; a saved level wins. An unreadable file or value falls
  back to 7; if saving fails the new level still lasts until restart.
- **Core, like the eye tool:** offered with the toolkit off too.
  `BAYMAX_VOICE_VOLUME_TOOL=0` stops offering it; a level already saved still applies.
- **Data flows:** none. Nothing is sent anywhere and no device is opened.

Tests: `tests/test_voice_volume.py` (levels, saving, the gain and limiter, the order in
the real speaker callback, and the tool offered and answered with the toolkit off).

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
