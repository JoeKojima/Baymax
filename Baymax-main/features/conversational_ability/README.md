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

## Validation and limits

### Local waiting acknowledgement (MVP)

`run_mvp.py` enables `BAYMAX_WAIT_CUE_ENABLED=1` by default. After more than
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
