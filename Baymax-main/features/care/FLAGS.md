# Night activity and care-level review flags

Facilities bill by care level, and a resident whose needs have grown often keeps getting
more help from staff before anyone reassesses them. This feature points staff at residents
who may be due for a reassessment, with the logged events behind it. **Ember suggests;
staff decide.** It counts events already in the care log. It never judges the resident's
health and never says a resident "needs more care". Every flag says it is *for staff
review of care level*.

Two parts: `night.py` records night activity, and `flags.py` evaluates the rules.

## Night activity (`night.py`)
- **Input:** the start times of the resident's utterances in `runtime_monitor.transcripts`,
  the transcripts Gemini Live already produces. It opens no microphone or camera and takes
  neither monitor observer slot. A session task (registered with
  `fall_detection.session_tasks` from `gemini_tools.py`) reads the list every 30 seconds
  and does its database work off the event loop.
- **Output:** one `night_wake` event (status `info`) per episode. Speech inside the night
  window that is more than `EMBER_CARE_NIGHT_GAP_MINUTES` after the last utterance starts a
  new episode. Summary: "Awake at night and talking with Ember from 02:10 to 02:40 (5 times)".
  `data` holds `night` (the evening's date, so 02:00 Saturday counts for Friday), `start`,
  `end`, `utterances` and `source: "speech_transcripts"`. No words are stored.
- **Restarts:** the open episode and the last utterance seen are kept in the care log's
  settings table, in one locked transaction per update. An episode continues across a
  restart. If the runtime stays down, the dashboard's loop records the stale episode.
- **Limitations:**
  - Only talking is seen. A resident who is up but silent isn't recorded. The fall
    toolkit's pose output (`fall_detection.worker.last_result`) has no presence or motion
    signal, and it is off by default, so it isn't used. Camera-based activity is left to
    the activity-tracking branch.
  - A TV or radio that Gemini transcribes as user speech counts as activity. Staff should
    read night activity as "Ember heard talking", not as proof the resident was awake.
  - Nothing is seen while the runtime is down or not connected.

## Flags (`flags.py`)
Rules compare the last `WINDOW_DAYS` (14) with up to `BASELINE_DAYS` (28) before it. The
baseline is scaled to the window's length.

| Rule | Counts | Raises a flag when |
|---|---|---|
| `help_requests` | `help_request` events | ≥ `HELP_MIN` (4), and ≥ `RATIO` (1.5) × baseline |
| `requests` | `request` events | ≥ `REQUEST_MIN` (10), and ≥ `RATIO` × baseline |
| `night_activity` | distinct nights with a `night_wake` | ≥ `NIGHTS` (5) of 14, and more than the baseline (a steady, long-standing pattern isn't raised again and again) |
| `check_ins` | `check_in` with `attention`, `no_answer`, `open`, or `handled` (closed by staff) | ≥ `CHECKIN_MIN` (3) in 14 days |
| `falls` | `fall` events | ≥ `FALLS` (2) in `FALL_DAYS` (30) |

- **Minimum history:** baseline comparisons need at least `MIN_BASELINE_DAYS` (14) of log
  before the window. Until then, the ratio rules use their fixed minimum only, and `data`
  says so (`basis: "absolute"` with a `note`).
- **Cooldown:** a rule isn't flagged again within `COOLDOWN_DAYS` (14) unless its count is
  at least 25% (and at least 1) higher than at its last flag.
- **Privacy:** rules use the staff view of the log. Events the resident kept private
  (other than safety events) aren't counted or cited.
- **Output:** `care.record("flag", summary, status="attention", data=...)`, so the flag goes
  onto the staff exceptions list, the morning digest and the facility page. Summary
  examples:
  - "5 requests for a person to come in the last 14 days (usually about 2) — for staff
    review of care level"
  - "2 falls recorded in the last 30 days — for staff review of care level"
- **`data` fields:** `rule`, `review: "staff"`, `basis` (`baseline` or `absolute`),
  `note`, `window {days, since, until}`, `baseline {days, since, until, count,
  per_window}` or null, `current`, `threshold {min, ratio?}`, `event_ids` (the most recent
  20 events counted: the evidence), `event_count` and `event_ids_truncated`.
  `docs/facility/SITE_CONTRACT.md` has the same schema for the website.

## Scheduling
- **Weekly:** the dashboard process's existing care loop (`dashboard._digest_loop`, every
  60 s) calls `flags.evaluate_if_due()`. The last run time is claimed atomically in the
  care log's settings, so restarts and a second process don't run it twice. A run that
  fails is retried on the next loop.
- **On demand (staff only):**
  - `POST /api/care/flags` runs the rules now;
  - `GET /api/care/flags?days=60` lists recent flags;
  - `python -m features.care.flags` runs them on the robot.

  Cooldowns still apply.

## Configuration
Both parts are on whenever facility mode is on (`EMBER_CARE_ENABLED=1`).
```
EMBER_CARE_NIGHT=23:00-06:00           # local night window; "off" disables night activity
EMBER_CARE_NIGHT_GAP_MINUTES=30        # quiet time that ends an episode
EMBER_CARE_FLAGS=1                     # 0 disables flags
EMBER_CARE_FLAGS_EVERY_DAYS=7          # weekly evaluation; 0 = on demand only
EMBER_CARE_FLAGS_WINDOW_DAYS=14
EMBER_CARE_FLAGS_BASELINE_DAYS=28
EMBER_CARE_FLAGS_MIN_BASELINE_DAYS=14
EMBER_CARE_FLAGS_RATIO=1.5
EMBER_CARE_FLAGS_HELP_MIN=4
EMBER_CARE_FLAGS_REQUEST_MIN=10
EMBER_CARE_FLAGS_NIGHTS=5
EMBER_CARE_FLAGS_FALLS=2
EMBER_CARE_FLAGS_FALL_DAYS=30
EMBER_CARE_FLAGS_CHECKIN_MIN=3
EMBER_CARE_FLAGS_COOLDOWN_DAYS=14
```
Invalid or negative values fall back to the defaults. Restart the runtime (night) and the
dashboard (flags) after changing these.

## Dependencies and external data flows
- **Dependencies:** standard library only. Flask is used for the routes, through the
  existing care dashboard.
- **External data flows:** none of its own. Night and flag events reach tadashirobotics.com
  and the morning digest like any other staff-visible care event (see `README.md`).
  Nothing is sent to Gemini.

## Tests
`python -m unittest tests.test_care_flags`. Offline and deterministic: a fake clock and
synthetic histories in a temporary SQLite file. It covers:
- the night window, and episodes that cross midnight;
- that transcripts are counted only once;
- episodes that survive a restart, and stale episodes;
- that the session task never blocks the event loop;
- each rule firing and not firing;
- the cooldown and worsening;
- minimum and partial history;
- capped evidence;
- that private events aren't counted;
- environment thresholds;
- the weekly run happening once across restarts, and a retry after a failure;
- that disabled care records nothing;
- banned clinical wording;
- staff-only routes.
