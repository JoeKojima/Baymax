# Ember toolkit

Reusable features live here; they never open microphones or cameras.

## Experimental public tools

This combined branch includes #7's desktop/debugging tools plus sixteen opt-in
public tools for personalized headlines, weather/air quality, currency reference
rates, holidays, earthquakes, background knowledge and book discovery. Enable the
experiment and choose groups in desktop Setup; tool activity appears in the live
monitor. Robot v10 supports the same public tools through its adapter. See
[inputs, providers, data flows, enabling/disabling and tests](EXPERIMENTAL.md).

## Conversational web discovery

The existing Gemini Google Search tool can be used without an explicit search
request when a public topic in an engaged conversation merits a relevant lookup.
Ember is guided to share one sourced finding, connect it to the person's interest,
and contribute a reaction or invite their view. It avoids research during venting
or ongoing storytelling, repeated searches, and invented sources or familiarity.
Queries should contain public topic terms, not personal disclosures or transcripts;
Google receives the query through Gemini's existing built-in search integration.
No new provider, dependency, background worker or recording is added.
`BAYMAX_GOOGLE_SEARCH=0` or `BAYMAX_TOOLKIT_ENABLED=0` disables this search tool.
Offline coverage is in `tests/test_toolkit.py` (registration, idempotence and
disabled behavior); relevant results, latency and natural spoken follow-ups still
require live evaluation.

## Location and weather

- `location.py`: `Location.get()` estimates city/region via HTTPS IPWho (`ipwho.is`), cached for one hour. It does not return or save the public IP. `set_city(city, country_code, region)` resolves a city explicitly supplied by the user through Open-Meteo geocoding. Multiple results require clarification; nothing changes until one place matches.
- `weather.py`: `Weather.get(units)` returns Open-Meteo current model-based conditions and today's forecast with timestamps, units and provider attribution. Cache lasts ten minutes; new coordinates or units have separate entries. Service failures return an error, not invented or expired conditions.
- `gemini_tools.py`: declares `get_location`, `set_location`, and `get_weather` for Gemini Live, dispatches HTTP work off the audio event loop, and preloads default location/weather in a background thread at startup.

No extra API key or package is required beyond the existing `requests` and Gemini SDK. No location data is written to disk by these tools. Confirmed cities last for the current process only. Gemini's existing transcript/recording behavior still applies.

IP location is approximate and can be wrong, particularly with a VPN or mobile network. It cannot establish an address, indoor room, GPS position or exact user whereabouts. A confirmed city refers to the city centre. Neither demo context nor old conversation notes establish the current location.

### Data sent to services

IPWho sees the requesting device's public IP. Open-Meteo receives approximate coordinates for weather and user-provided city/region queries for geocoding. Tool results (including approximate coordinates) go to Gemini so it can speak the answer. Tools do not send audio, images, Gemini keys or email credentials to these providers.

### Try it

Start Ember normally and ask “Where am I?” or “What's the weather?” Correct the estimate with “I'm in [city], [state/region], [country]”, then ask about weather again. Ask for Celsius or Fahrenheit as desired. Stop Ember using the existing stop procedure.

Run offline tests: `python -m unittest discover -s tests`.

Set `BAYMAX_TOOLKIT_ENABLED=0` in the private `.env` to disable public tool declarations, added instructions and preloading. Independent eye commands remain available when the MVP eye session is enabled; `BAYMAX_EYES_ENABLED=0` disables those. The Gemini model and original audio/video pipeline have not changed.

### Provider references and deployment

- [IPWho API documentation](https://ipwhois.io/documentation)
- [Open-Meteo weather API](https://open-meteo.com/en/docs)
- [Open-Meteo geocoding API](https://open-meteo.com/en/docs/geocoding-api)
- [Gemini Live function calling](https://ai.google.dev/gemini-api/docs/live-api/tools)

The Open-Meteo public endpoint is for non-commercial use under its current service terms. A commercial Ember deployment needs the appropriate provider plan and deployment configuration; this developer implementation uses the public endpoints. Do not silently switch a fleet to the development endpoint. Review provider terms and rate limits before production use.

### Feature review protocol

Every added/changed toolkit follows [TESTING_PROTOCOL.md](../../TESTING_PROTOCOL.md). Add `tests/test_toolkit*.py`, run `python check_release.py`, and document normal, invalid/ambiguous, failure/timeout, core integration and disabled behavior. Provide separate core and toolkit results in the PR.

## Audiobooks (LibriVox)

One `audiobook` tool (`play` with title/author, `pause`, `resume`, `stop`,
`next`, `status`). `librivox.py` searches the Internet Archive and downloads one
chapter at a time to a temp cache (search/ranking adapted from PR #2).
`audiobooks.py` decodes with `miniaudio` (`requirements-pc.txt`) on a feeder
thread into a ~2 s buffer; Ember's speaker callback mixes it in through the
audio cleanup's `mix_output`, so echo cancellation removes the book from the mic
and the book holds while Ember speaks. Status reports the chapter being heard.
Volume: `BAYMAX_BOOK_VOLUME` (0-1, default full; also the `volume` action's
0-100 %), times `BAYMAX_BOOK_BOOST` (default 1.25, so full volume is 25% louder
than the recording; loud peaks are clipped). Set the boost to `1` for the
recording's own level.

Book mode: echo cancellation leaves enough narration that Gemini answered the
narrator as the person. While a book plays, `gate` (in `processing.input_gates`)
withholds the mic from Gemini and feeds it to `wakeword.py`, an offline Vosk
listener (`vosk==0.3.45`, small English model ~41 MB in `models/`, gitignored;
fetch once with `python features/toolkits/wakeword.py --download`). "Hey Ember",
"Ember stop/pause", or a lone "stop"/"pause" pauses the book; the book also
pauses after every chapter. Either way Ember gets an `[AUDIOBOOK]` note naming
the book, author and chapter so it can talk about it, and `resume` continues.
Pairs count in either order ("Ember, hey"), "hey/okay stop" too, and across a
pause of up to 3 s (Vosk often splits "hey | Ember"). Any wake word heard on its
own lowers the book to 30% for 4 s (`DUCK_GAIN`/`DUCK_SECONDS` in
`audiobooks.py`) so the rest of the phrase is heard over it; narration that
sounds like a wake word causes the same short dip. Volume changes ramp over one
speaker block, so ducking and "louder" never click.
Without Vosk or the model the mic stays open (the old behaviour). Rules were
tuned on LibriVox narration and on ember-002 attempts (see the `wakeword.py`
docstring). No bookmarks yet.
Data sent: search terms and file requests to archive.org, plus the one-time
model download from alphacephei.com; wake words run locally.
Tests: `tests/test_toolkit_audiobooks.py` (offline fakes).

## Starting conversations (`proactive.py`)

Ember speaks first in two cases: a friendly two- or three-sentence hello once per
run, four seconds after the first Gemini session starts
(`BAYMAX_STARTUP_GREETING=0` disables), and a
greeting on new eye contact (off by default; `BAYMAX_PRESENCE_GREETING=1` enables:
on ember-002 the mic stopped reaching Gemini for 144 s right after it started,
alongside pose fall detection on an i5-8200Y). Eye
contact uses MediaPipe Face Landmarker (`mediapipe`, model ~3.6 MB downloaded
once from storage.googleapis.com to `.models/`, checksum-pinned) on the existing
camera frames, which reach it through `fall_detection.frame_observers`; no new
camera stream. A look counts when the face is turned to the camera, both irises
are centred, the face is within about 1-1.5 m (detector limit) and it lasts
~0.8 s after nobody looked for 30 s. The dashboard timeline shows "Eye contact
started" and "Eye contact ended" (with why it did or did not greet). It only
greets when nobody has spoken for
`BAYMAX_PRESENCE_QUIET_S` (120), at most every `BAYMAX_PRESENCE_COOLDOWN_S`
(600; the startup hello counts), not while Ember is talking or a book plays.
Runs at `BAYMAX_PRESENCE_FPS` (4; ~8 ms/frame on a desktop CPU). The startup note
includes the device's current local date/time, UTC offset, timezone name and a
matching greeting, using the standard-library clock (no new dependencies).
Timezone/offset metadata is internal context; the spoken greeting omits it.
It asks Gemini to mention the local AM/PM time, notice a clearly visible detail in the
existing video, and ask one relevant question about what's happening there.
It does not prompt offers of reminders or other tasks; the general persona
offers capabilities only when they directly address an expressed need.
No sleepy waking-up roleplay or humming, invented surroundings, presumed moves
to a new place, or inferred physical location from a timezone. If the view is
unclear/unavailable, ask where Ember is or what's happening. City/state names need
user confirmation. Correct device clock/timezone configuration is required.
Data sent: the `[STARTUP]`/`[PRESENCE]` text note to the existing Gemini session;
no extra capture, provider request or persisted scene is added. The four-second
delay gives the existing three-second video sender time for its first frame;
it cannot guarantee a camera is available or a model follows the prompt.
Tests: `tests/test_toolkit_proactive.py` (fake faces, clocks, sessions, local
AM/PM/timezone/daypart boundaries, once-per-run greeting and reconnect retry).
Live robot voice, scene grounding and missing-camera checks remain pending.

## Optional CPU fall detection

An actual fall-alert dispatch now notifies the eye controller to show surprise,
overriding model expressions and delayed thinking. Missing/disabled eyes do not
prevent the existing check-in. No external notification is added; fake-alert
dispatch and priority tests cover the integration. See [eye behavior](../eyes/README.md).

Enable the experimental CPU checkbox in Setup and restart Ember. It reuses camera frames, reports performance/state in the monitor and can ask a possible-fall check-in through Gemini; no external alerts are sent. See [fall inputs, dependencies, outputs and validation](FALL_DETECTION.md).

## Conversation discovery

Five additional public tools provide trivia, TV shows, poetry, radio
station discovery and recipes. See [CONVERSATION.md](CONVERSATION.md) for inputs,
provider limits, enablement and checks. They use the existing Discovery checkbox.

## Everyday tasks

[Timers, reminders and household lists](everyday/README.md) persist locally and use the existing Gemini session for spoken notifications.
