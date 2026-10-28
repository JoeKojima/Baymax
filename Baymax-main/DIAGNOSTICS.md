# Latency and audio diagnostics

Open **Latency & audio diagnostics** at the top of the existing Monitor, or
`http://127.0.0.1:8766/diagnostics`. This extends the existing loopback-only
devUI server and its Origin/token protections; it opens no live capture stream.

## Live readings

V10 publishes rolling latest/median/95th-percentile timings for the microphone
send queue, SDK audio send calls, received-audio processing and the wait from
the last user-transcript fragment to the first returned reply-audio packet.
Each statistic has a sample count and age. The latest 50 samples are retained
in memory; status snapshots use the existing local runtime-status file.

These are diagnostic intervals, not network RTT or end-to-end acoustic latency.
Reply wait includes transport, Gemini processing and tool waits after input
transcription; it excludes earlier capture, transcription and speech-end detection.
Send-call duration can show backpressure but does not prove network delivery.
Older runtime versions can serve the page with unavailable timing fields.

Microphone readings use the existing raw/clean RMS, clipping, speech estimate,
reply-muting, audiobook-gating and echo-cancellation telemetry. Playback shows
queued PCM duration/bytes, output format and device-reported latency. Offline
or stale runtime data clears the live readings instead of presenting old values
as current. No audio samples are added to runtime telemetry.

## Manual connection check

The button calls the fixed Gemini API hostname on port 443 and times DNS, TCP
and certificate-verified TLS setup. It sends no key, audio, video, conversation
text or generation request. It does not measure the current Live WebSocket or
isolate Wi-Fi from the rest of the route. DNS uses the system resolver; TCP
connection attempts share a three-second budget, and TLS has a three-second
timeout. The browser waits up to twelve seconds. There is no recurring probe.

## Microphone recording and replay

The microphone/playback links open the existing Setup tests. Stop Ember before
opening a device test to avoid competing streams. The microphone test captures
three seconds only when clicked and returns a mono 48 kHz WAV preview to the
local browser. It is not written to disk or sent to Gemini. Starting another
test clears the old preview. Playback uses the browser's current output; the
separate speaker test targets Ember's configured output. No package is added.

## Verification

Offline tests: `test_diagnostics.py`, `test_devUI_http.py`, `test_address_gate.py`.
They use mocked connections, synthetic PCM and extracted runtime functions;
they do not open devices or contact Gemini. Browser preview uses explicitly
labeled sample data. Actual robot Wi-Fi, microphone replay, speaker output and
latency during a real Gemini session require live checks.
