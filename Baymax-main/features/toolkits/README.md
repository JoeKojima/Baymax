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

## Gentle daily check-ins (`checkins.py`)

Ember checks in like a friend up to three times a day: a morning "How did you
sleep?", an afternoon "Have you had some water today?" and an evening "How was
your day?". There is at most one attempt per window per day. If nobody answers,
it is not repeated.

**When Ember checks in (all must be true):**
- **Not at boot.** Ember has been connected for at least
  `BAYMAX_CHECKIN_UPTIME_S` (1200). A check-in is never the startup hello.
- **Inside a window**, on the device-local clock:
  - `BAYMAX_CHECKIN_MORNING` (08:00-11:00)
  - `BAYMAX_CHECKIN_AFTERNOON` (14:00-16:30)
  - `BAYMAX_CHECKIN_EVENING` (18:00-20:30)

  `0` turns a window off.
- **Quiet and likely not alone.** The last conversation ended at least
  `BAYMAX_CHECKIN_QUIET_S` (300) ago but no more than `BAYMAX_CHECKIN_AROUND_S`
  (10800) ago. Recent conversation is the only "someone is home" signal that
  needs no extra camera work, so an empty room (or nobody since boot) hears
  nothing.
- **Not busy.** Ember isn't speaking, thinking or calling a tool, and no
  audiobook is playing.

**Behaviour:**
- The note asks for one warm sentence, no medical advice, and to drop it if the
  person doesn't want to chat.
- "Done today" is kept in `toolkit_checkins.json` under `BAYMAX_DATA_DIR`
  (gitignored), so a restart doesn't ask again. A failed send is retried later
  in the window.

**Other details:**
- **Disable:** `BAYMAX_CHECKINS=0` (or `BAYMAX_TOOLKIT_ENABLED=0`).
- **Data sent:** only the `[CHECK-IN]` text note to the existing Gemini session.
  Answers are ordinary conversation; nothing is saved or sent elsewhere.
- **Runs through** `fall_detection.session_tasks`; `proactive.py` is unchanged.
- **Tests:** `tests/test_toolkit_checkins.py` (fake clocks, session and state
  file), plus the shared check that no session task blocks the event loop.

## Facility morning check-in (`care_checkin.py`)

Facility mode only (`EMBER_CARE_ENABLED=1`, see `features/care/README.md`). It replaces the
friendly morning check-in above with one that staff see; afternoon and evening are unchanged.
With facility mode off, nothing here runs and the check-ins above behave exactly as before.

**Flow, once a day in `EMBER_CARE_CHECKIN_WINDOW` (08:00-10:30):**
1. When it's quiet (same uptime/quiet/not-busy/no-audiobook rules as above, but *without*
   the "someone talked in the last 3 hours" rule, because staff expect this one), Ember is
   sent a `[CARE CHECK-IN]` note and asks how they are, in one warm sentence.
2. After they answer, Gemini calls `check_in_result(outcome, summary, quote?, private?)`:
   - `ok`: they are fine;
   - `attention`: they mentioned something staff would want to know (feeling unwell, pain,
     a fall, feeling low, confusion, wanting someone to come). Ember reports what was said;
     the tool's rules forbid diagnosing, symptom questions and medical advice.
   This becomes a `check_in` care event with their words as the quote.
3. No speech within `EMBER_CARE_CHECKIN_REPLY_S` (180) of asking: one gentle retry after
   `EMBER_CARE_CHECKIN_RETRY_S` (1800), if still in the window (`EMBER_CARE_CHECKIN_RETRIES`, 1).
   Still nothing: one `no_answer` event (`data.reason="no_reply"`).
4. Window over and Ember never got to ask (busy, just restarted, offline): one `no_answer`
   event (`reason="not_reached"`).
5. They answered but Gemini didn't call the tool within `EMBER_CARE_CHECKIN_RESULT_S` (600):
   an `info` event (`reason="no_result"`), not on the exceptions list. Ember doesn't guess.
6. An answer after a `no_answer` is still recorded (`answered_late`) and the `no_answer` is
   marked handled, so staff don't chase it.

`attention` and `no_answer` show on the staff exceptions list and in the morning digest.

**Details:**
- **Inputs:** the device clock; existing `runtime_monitor` state and user transcripts (to
  see whether anyone spoke after asking). No extra microphone or camera use.
- **Outputs:** the `[CARE CHECK-IN]` note to the existing Gemini session; `check_in` care
  events (`data`: `check`, `attempts`, `reason`, `answered_late`; see
  `docs/facility/SITE_CONTRACT.md` §1).
- **Persistence:** today's progress (attempts, last ask, result) is kept in the care log's
  settings (`morning_check_in`), so restarts don't repeat, lose or double-record it.
- **External data flows:** Gemini gets the note and declares the tool (only in facility mode).
  The care log forwards events to the facility page as described in its README.
- **Disable:** `EMBER_CARE_CHECKIN=0` or `EMBER_CARE_CHECKIN_WINDOW=0` (the friendly morning
  check-in then comes back), `EMBER_CARE_ENABLED=0` or `BAYMAX_TOOLKIT_ENABLED=0`.
  `BAYMAX_CHECKINS=0` turns off only the friendly check-ins.
- **Tests:** `tests/test_toolkit_care_checkin.py` (fake clock, session, monitor and care log):
  ok, attention, private, no reply, retry, not reached, missing result, late answer,
  restart, tool validation, declarations, and facility mode off = unchanged check-ins.
  Live scenarios: `evals/scenarios_care_checkin.json` (for `evals/live_eval.py --scenarios`).

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
