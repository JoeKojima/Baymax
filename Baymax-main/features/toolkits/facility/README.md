# Facility toolkit: menu and activity answers, activity reminders, staff requests

Facility mode only (`EMBER_CARE_ENABLED=1`). Aim: fewer repeated questions and front-desk
calls, with nothing new for staff to learn. They keep producing the activity calendar, the
weekly menu and email they already have; Ember reads the first two and sends the third.

## What Ember can do
- **`facility_info(topic, day?)`**: `topic` is `menu` or `activities`; `day` is `today`
  (default), `tomorrow`, a weekday name or `YYYY-MM-DD` (up to a week back, two months ahead).
  Returns the items staff provided for that day. The prompt rule: answer only from these
  items, never invent menu items, times or places; if there is nothing, say so and suggest
  the front desk. When the latest refresh failed, the result says how old the copy is.
- **`remind_activity(title, day?, minutes_before?)`**: "Bingo at 2? Remind me." It finds the
  activity in the calendar (the time comes from the calendar, not from Gemini), creates a
  one-time reminder in the **existing everyday reminder store** (announced by its session
  task as usual), and records a care `activity` event (`info`, "Plans to attend Bingo
  (Fri 14:00)"). If two activities match, or it starts within a few minutes, it says so
  instead.
- **`staff_request(category, details, urgent?)`**:

  | category | care kind | Ember says |
  |---|---|---|
  | `maintenance` (broken light, leak, heating) | `request` | "I've let maintenance know." |
  | `housekeeping` (towels, linens, cleaning) | `request` | "I've let housekeeping know." |
  | `front_desk` (packages, transport, appointments) | `request` | "I've let the front desk know." |
  | `dining` (meal issues) | `request` | "I've let the dining team know." |
  | `care_aide` (wants someone to come, help getting up) | `help_request` | "I've let the care staff know." |

  Every request is logged with `status="open"` (it goes on the staff exceptions list) and
  `data={"route": category, "urgent": bool, "source": "staff_request"}`. Ember never promises
  a time ("I can't say exactly when they'll come").
  **Emergencies:** with `urgent=true` (hurt, fallen, can't breathe, chest pain, "it's an
  emergency") the request becomes a `help_request` to `care_aide` with `urgent: true` and a
  summary starting `URGENT:`, and Ember first tells the resident to use their pendant or
  pull cord (`EMBER_EMERGENCY_WORDING`). Ember is not an emergency system; the log entry is
  a backup, not the alarm. If logging fails, Ember still says the emergency wording; for a
  normal request it says it couldn't pass it on and suggests the front desk or call button.

All tool results are data, never instructions. Nothing clinical is recorded: the care log
refuses non-listed kinds, and `details` is a short plain summary.

## Inputs
- **Activity calendar** (`EMBER_FACILITY_CALENDAR_URL`): an `http(s)://` or `webcal://` URL
  or a file path. ICS (exported by most engagement tools) or CSV with columns
  `date,time,title,location` (optional `end`). Sniffed by content.
- **Menu** (`EMBER_FACILITY_MENU_CSV`): URL or file path, CSV with columns `date,meal,items`.
- **Staff upload on the robot dashboard:** `/care/facility` (a plain upload form), or
  `POST /api/facility/upload/<activities|menu>` with a file or the raw body;
  `DELETE` (or the form's "Remove upload") removes it. `GET /api/facility/status` shows
  the source, last update, last error and row counts. **Staff only** (paired accounts in
  `EMBER_STAFF_EMAILS`); 404 when facility mode is off. Files are validated before they
  are saved, max 2 MB. An upload wins over the env setting for that kind.
- **CSV details:** headers are matched loosely (`day`, `start time`, `activity`, `room`,
  `dish`, …), a UTF-8 BOM from Excel is fine. Dates: `2026-11-13`, `11/13/2026`,
  `11/13/26`, or a weekday name (`Monday`) for a weekly menu that repeats; a dated row
  replaces the weekday row for the same meal/title. Times: `14:00`, `2pm`, `10:30 a.m.`;
  blank means all day; a bare `2` is refused as ambiguous. Unreadable rows are skipped and
  counted.
- **ICS details** (`ics.py`, no new dependency): line folding, escapes, all-day and
  multi-day events, UTC `Z` times, `TZID` (IANA names via `zoneinfo`; an unknown name is
  treated as the robot's local time), floating times, `DTEND`/`DURATION`,
  `STATUS:CANCELLED`, `EXDATE`, `RECURRENCE-ID` (moved or cancelled single occurrences),
  `RRULE` `FREQ=DAILY|WEEKLY` with `INTERVAL`, `COUNT`, `UNTIL`, plain `BYDAY`.
  **Unsupported:** `MONTHLY`/`YEARLY`, ordinal `BYDAY` (`1TU`), `BYMONTHDAY`, `BYSETPOS`,
  `RDATE`. Such events show only on their first date, and `status` counts them as
  `unsupported_repeats` so staff know to check. Times are answered in the robot's local
  timezone.

## Caching and refresh
The last copy that parsed is kept in `BAYMAX_DATA_DIR/facility/cache/`. URL/file sources
are re-read every `EMBER_FACILITY_REFRESH_MINUTES` (default 60) by a background session task
(and on a tool call if due; fetch timeout 8 s). A failed fetch or a file that no longer
parses keeps the last good copy, records the error, and the tool tells Ember the copy's date.

## Request emails (routing outward)
Set `EMBER_ROUTE_EMAIL_<ROUTE>` (`MAINTENANCE`, `HOUSEKEEPING`, `FRONT_DESK`, `DINING`,
`CARE_AIDE`). Many work-order systems accept tickets by email, so that address is the
integration. Unset routes are not emailed; they still reach staff through the facility page,
`/care` and the morning digest.

The tools run in the Ember runtime; email goes from the dashboard process (`baymax_app.py`).
Its loop polls the care log every 30 s for staff-visible, still-open `request`/
`help_request` events from the last 24 hours with a routed address, and sends each one with
the dashboard's Gmail settings (`BAYMAX_EMAIL_FROM`/`BAYMAX_EMAIL_PASSWORD`, synchronous
SMTP so success is known). `BAYMAX_DATA_DIR/facility/route_emails.sqlite3` records each
event as sending → sent/failed: an event is sent once; a failure retries up to 5 times;
a crash mid-send never sends it twice. Without SMTP configured nothing is marked, so
requests still open and under a day old go once email is set up. Requests handled before the email went out, and private non-safety
events, are not sent.

**The email holds** the summary, the route, the time, the unit name (`KNOWN_DEVICES`) and a
reference number, plus the line that Ember is not an emergency system. **Never the
resident's words**: email leaves the robot and work-order systems are widely readable, and
`details` already carries what staff need.

## External data flows
- **Gemini:** the tool declarations and prompt; the day's menu/activity items Ember asked
  for; the request category and the short summary Gemini itself wrote.
- **Calendar/menu URLs** (if configured): a plain GET to the facility's own URL. The URL's
  query string (often a private token) is never shown in status or logs.
- **Gmail SMTP → route addresses:** the request email above.
- **Facility page:** requests and activity events go through the care log's normal uplink
  (`features/care`), with `data.route` / `data.urgent` (see `docs/facility/SITE_CONTRACT.md`).

## Dependencies
Standard library only (`csv`, `zoneinfo`, `urllib`, `smtplib`, `sqlite3`), plus Flask
(already used by the dashboard). On Windows `zoneinfo` needs the `tzdata` package for
`TZID` names; without it TZID times are read as local time.

## Enabling and disabling
```
EMBER_CARE_ENABLED=1                 # required: these tools exist only in facility mode
EMBER_FACILITY_ENABLED=1             # 0 removes the three tools and the prompt section
EMBER_FACILITY_CALENDAR_URL=...      # optional; or upload on /care/facility
EMBER_FACILITY_MENU_CSV=...          # optional; or upload on /care/facility
EMBER_ROUTE_EMAIL_MAINTENANCE=...    # optional, per route
EMBER_EMERGENCY_WORDING=...          # optional; match the building's pendant/pull cord/call button
```
`BAYMAX_TOOLKIT_ENABLED=0` also turns them off. With facility mode off the tools are not
declared, calls return an error and log nothing, and the dashboard routes return 404.
`remind_activity` needs the everyday toolkit (`EMBER_EVERYDAY_ENABLED`). Restart the runtime
and the dashboard after changing these.

## Tests
`python -m unittest tests.test_toolkit_facility` (offline: no network, Gemini, SMTP or
devices):
- ICS: folding/escapes, UTC and TZID, the local day near midnight, all-day and multi-day,
  weekly/daily rules with COUNT/UNTIL/INTERVAL across a DST change, EXDATE, moved and
  cancelled occurrences, unsupported rules, alarms, bad input.
- CSV: weekday menus with dated overrides, loose headers, BOM, time formats, skipped rows.
- Sources: last good copy on failure and refresh timing, URL tokens hidden, upload over env.
- Tools: day selection, answers only from the source, missing data, reminders through the
  everyday store, ambiguity, route mapping, emergency wording (and configurable), logging
  failures, invalid arguments, facility mode off → tools absent and inert.
- Emails: once per request, no quote, unrouted/private/handled/old not sent, retry limit,
  SMTP not configured.
- Routes: staff only, facility mode off, validation, form upload, status, removal.

Layer 1 live scenarios (Gemini in the loop, stubbed tool results) are in
`live_scenarios.json` here, for `evals/live_eval.py --scenarios` (run with
`EMBER_CARE_ENABLED=1`).
