# Everyday tasks: timers, reminders and household lists

Three Gemini tools: `local_clock`, `reminder`, and `household_list`. No new
packages or external APIs. Available when the main toolkit is enabled; set
`EMBER_EVERYDAY_ENABLED=0` (or `BAYMAX_TOOLKIT_ENABLED=0`) to hide tools and pause
notifications. Re-enable to deliver overdue reminders. Restart after changing env.

- Timer: `reminder(action="create", text="Check oven", seconds=1200)`.
- Absolute reminder: first read `local_clock`, clarify the date/time and timezone,
  then pass `due_at` as ISO date/time with an explicit UTC offset instead of seconds.
  Ambiguous timezone/DST assumptions must be clarified. One-time only, within one year.
- List or cancel pending reminders with `action="list"` / `action="cancel", id=...`.
- `household_list` supports add/show/remove/clear with a named list (default shopping).
  Remove uses the exact item ID; clear requires an explicit user request. Duplicate
  exact items are ignored. Limits: 100 pending reminders, 20 lists, 100 items/list.

## Repeating reminders and check-ins

Reminders can repeat like Apple Reminders' custom repeat (`recurrence.py`):
hourly, daily, weekly, monthly or yearly, every N, chosen weekdays, days of the
month (or the last day), "the second Tuesday" / "the last weekday", chosen months,
and an end date. Times are wall-clock times in the robot's timezone, so "8 AM
daily" stays at 8 AM across daylight saving. By voice: `reminder(action="create",
text=..., due_at=<first time>, repeat={"freq": "weekly", "weekdays": ["MO", "TH"]})`.
After downtime a repeat is announced once, then moves to its next time.

Every announcement is kept as an occurrence. Ember asks whether it was done and
records the answer with `reminder(action="answer", occurrence=..., outcome=...)`;
with no answer it asks once more after 10 minutes, then logs "no answer". Each
reminder is labelled with who set it (`voice` or `dashboard`); reminders set on
the Ember app come in through `features/toolkits/family/` and cancellations from
the app are applied the same way.

## Storage and data flow

SQLite at `BAYMAX_DATA_DIR/toolkit_everyday.sqlite3`, default `Baymax-main/`.
Transactions survive restart without partial writes. The database and journals are
Git-ignored. Shared device data, not speaker-specific. With a device key, reminders
and their check-in history are synchronised with the Ember app (see `family/`). No
identity is inferred. Reminder text, due times and requested list results enter the
existing Gemini conversation; nothing is sent to a new provider. Storage is not
encrypted; use normal OS account/file protections. No recordings or capture added.

## Notifications and limitations

The existing live-session task hook checks every five seconds, at most five due
reminders per message. It waits while Ember speaks/thinks/calls a tool, during an
active audiobook and for three seconds after recent conversation. Spoken alerts
need the runtime and Gemini connection; no OS wakeup or offline alarm. Overdue
reminders remain pending across downtime. Device clock/timezone must be correct;
timers use persisted wall-clock deadlines, so clock adjustments affect them.

Failures retain pending reminders for retry. A successful Gemini message submission
marks them announced; that is not proof audio was heard. A crash between submission and
storage update can cause a repeat. Cancelling very near delivery can race with a
message already submitted. These are convenience reminders, not guaranteed alarms.
Text is treated as data, never as a command to execute. History (announcements and
answers) is kept 180 days, once the Ember app has it.

## Tests and review

From `Baymax-main`: `python -m unittest discover -s tests -p "test_toolkit_*.py"` (everyday,
reminder repeats and family),
then `python check_release.py --report .local-review/everyday.md`.
Tests cover restart/overdue behavior, timezone/duration validation, list isolation,
deduplication, capacity, storage failure, disabled/busy behavior, delivery retries,
registration and core configuration/dispatch.

Live checks pending: spoken creation and confirmation, timer expiry, interruption,
reminder delivery after restart, list operations, ambiguous times, lost connection,
and disabling tools. No live capture is started by offline tests. Follow
`TESTING_PROTOCOL.md` before release approval; hardware parity is not established.

Offline evidence (2026-10-21): 220 core tests and 123 toolkit tests passed, no skips. The new everyday suite contains 10 tests. The notification hook is integrated with the documented v8 desktop runtime; v9/v10 and deployed robot parity are unverified.
