# Ember toolkit

Reusable features live here; they never open microphones or cameras.

## Experimental public tools

This combined branch includes #7's desktop/debugging tools plus sixteen opt-in
public tools for personalized headlines, weather/air quality, currency reference
rates, holidays, earthquakes, background knowledge and book discovery. Enable the
experiment and choose groups in desktop Setup; tool activity appears in the live
monitor. Robot v10 supports the same public tools through its adapter. See
[inputs, providers, data flows, enabling/disabling and tests](EXPERIMENTAL.md).

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
Volume: `BAYMAX_BOOK_VOLUME` (0-1, default full).

Book mode: echo cancellation leaves enough narration that Gemini answered the
narrator as the person. While a book plays, `gate` (in `processing.input_gates`)
withholds the mic from Gemini and feeds it to `wakeword.py`, an offline Vosk
listener (`vosk==0.3.45`, small English model ~41 MB in `models/`, gitignored;
fetch once with `python features/toolkits/wakeword.py --download`). "Hey Ember",
"Ember stop/pause", or a lone "stop"/"pause" pauses the book; the book also
pauses after every chapter. Either way Ember gets an `[AUDIOBOOK]` note naming
the book, author and chapter so it can talk about it, and `resume` continues.
Without Vosk or the model the mic stays open (the old behaviour). Rules were
tuned on LibriVox narration (see the `wakeword.py` docstring). No bookmarks yet.
Data sent: search terms and file requests to archive.org, plus the one-time
model download from alphacephei.com; wake words run locally.
Tests: `tests/test_toolkit_audiobooks.py` (offline fakes).

## Radio and recordings (`listen.py`)

One `listen` tool plays live internet radio (`play_radio` with a genre, station
name, or a `public_radio` stationuuid as `station_id`, optional `country_code`)
and openly licensed recordings from the Internet Archive (`play_recording` with
what to hear). `pause`, `resume`, `stop`, `next` (recordings), `status` and
`volume` control whatever is playing, an audiobook included.

- **Shared player:** both run through the audiobook `Player` (`audiobooks.py`),
  which now plays a *program*: a book, a station or a recording. Only one plays
  at a time, and starting one replaces the other. Like books, playback is mixed into Ember's
  speaker stream (echo reference included), holds while Ember speaks, withholds
  the mic from Gemini, and pauses on the offline wake words with a `[RADIO]` or
  `[RECORDING]` note to Gemini. `BAYMAX_BOOK_VOLUME` sets the starting volume
  for all of them.
- **Radio:** `find_stations` asks Radio Browser (de1 mirror, most-listened first,
  tag search then name search) and keeps stations whose codec is MP3, Ogg or
  FLAC, not HLS, and passing Radio Browser's last check. Before playing, the
  stream is opened to confirm its Content-Type. miniaudio decodes it
  incrementally on the feeder thread through a `StreamableSource` (no new
  dependency). AAC/AAC+ and HLS stations are skipped with a clear error, since
  miniaudio has no AAC decoder. Stream URLs are third-party data, so each URL
  and redirect must be http(s) and resolve only to public internet addresses
  (never the robot's home network). Up to 3 checked redirects are followed;
  ICY metadata is not requested.
- **Stalls and drops:** 10 s without data or a dropped connection reconnects
  (2/4/8 s backoff). After 3 failed reconnects Ember is told the station stopped.
  Buffering is the player's ~2 s. Paused radio is not buffered; `resume`
  reconnects to the live stream.
- **Recordings:** an Archive full-text search (`mediatype:audio`, LibriVox
  excluded) is ranked by title match and popularity. Only items whose license
  URL is public domain/CC0, CC BY or CC BY-SA are used (the Open Definition
  licenses; any NC or ND item is skipped), and the item's own metadata is
  checked again. MP3 tracks (at most 100) download one at a time to the
  audiobook cache and play straight through. Status gives the title, creator,
  license and archive.org page so Ember can attribute it. Licenses are as stated
  by the uploader.
- **Check-in:** after `BAYMAX_LISTEN_CHECK_MIN` minutes (default 30; 0 never)
  of radio or recordings, playback pauses and Ember asks whether to keep
  listening. This way nobody is stuck with a closed mic, and due reminders
  (which wait for silence) get a chance.
- **Enable/disable:** `BAYMAX_LISTEN_ENABLED=0` removes the tool and its prompt
  section. It is on by default, and needs `BAYMAX_TOOLKIT_ENABLED`. `public_radio`
  still never returns stream URLs to Gemini.
- **Data sent:**
  - to Radio Browser: the search term, country code or station ID;
  - to the chosen station's server: a normal HTTP stream request;
  - to archive.org: search words and file requests.

  No account, mic audio or transcript is sent.
- **Tests:** `tests/test_toolkit_listen.py` (fake network/decoder: playback,
  wake pause and reconnecting resume, check-in, drops, unplayable codecs, open
  licenses, public-address and redirect checks, one-at-a-time, wiring and
  switch). Existing `tests/test_toolkit_audiobooks.py` runs unchanged.

## Starting conversations (`proactive.py`)

Ember speaks first in two cases: a one-sentence hello once per run when the
first Gemini session starts (`BAYMAX_STARTUP_GREETING=0` disables), and a
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
Runs at `BAYMAX_PRESENCE_FPS` (4; ~8 ms/frame on a desktop CPU). Data sent: only
the `[STARTUP]`/`[PRESENCE]` text note to Gemini, which already receives the
video. Tests: `tests/test_toolkit_proactive.py` (fake faces, clocks, sessions).

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
