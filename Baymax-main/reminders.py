"""
Reminders — "remind me to take my medication in 1 hour" / "at 5:00 pm".

Gemini Live sets and manages reminders through function calling (see
REMINDER_TOOLS); this module turns the spoken time into a due time on the
robot's own clock, persists reminders to `reminders.json` so they survive
restarts, and tells the main loop when one is due.  The main loop then prompts
Gemini to speak up and ask whether the user has done it.

- If the user doesn't say anything after a reminder, it is repeated up to
  MAX_RETRIES times, RETRY_INTERVAL_SECONDS apart.
- The user's answer is recorded with record_reminder_response (done / not done)
  so it shows up in the dashboard transcript.
- Daily reminders ("every day at 8 am") reschedule themselves.

Usage:
    reminders = ReminderManager(on_event=lambda text: ...)
    reminders.handle_tool_call("set_reminder", {"task": "take medication",
                                                "minutes_from_now": 60})
    due = reminders.due()                 # poll ~1 s
    prompt = reminders.prompt_for(due)    # send to Gemini, then:
    reminders.mark_announced(due)
"""
import datetime as dt
import difflib
import json
import os
import re
import threading
import time
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REMINDERS_PATH = os.path.join(SCRIPT_DIR, "reminders.json")

# ─── Config ──────────────────────────────────────────────────────────────────
RETRY_INTERVAL_SECONDS = 300     # re-ask after 5 min if the user said nothing
MAX_RETRIES = 2                  # ...at most this many extra times
MISSED_GRACE_SECONDS = 2 * 3600  # overdue by more than this at startup → missed
MAX_MINUTES_AHEAD = 7 * 24 * 60
HISTORY_KEEP_SECONDS = 7 * 24 * 3600

# ─── Gemini function declarations ───────────────────────────────────────────
REMINDER_TOOLS = [
    {
        "name": "set_reminder",
        "description": (
            "Set a reminder. When it is due you will be prompted to remind the "
            "user and ask whether they did it. Give EITHER minutes_from_now "
            "(for 'in 1 hour' → 60) OR time (for 'at 5 pm')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {
                    "type": "STRING",
                    "description": "What to remind them about, e.g. 'take your medication'",
                },
                "minutes_from_now": {
                    "type": "NUMBER",
                    "description": "Relative delay in minutes, e.g. 60 for 'in an hour'",
                },
                "time": {
                    "type": "STRING",
                    "description": "Clock time exactly as the user said it, e.g. '5:00 pm', "
                                   "'17:30', '9am', 'noon'",
                },
                "days_from_now": {
                    "type": "INTEGER",
                    "description": "Only with time: 0 = today, 1 = tomorrow. Omit to use "
                                   "the next time that clock time comes around.",
                },
                "repeat_daily": {
                    "type": "BOOLEAN",
                    "description": "True if the user wants this every day",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List upcoming reminders and the current time.",
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel an upcoming reminder.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "Which reminder, e.g. 'medication'"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "record_reminder_response",
        "description": (
            "After reminding the user, record whether they have done it "
            "(e.g. taken their medication)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "Which reminder"},
                "done": {"type": "BOOLEAN", "description": "True if they did it"},
            },
            "required": ["done"],
        },
    },
]

REMINDER_INSTRUCTIONS = (
    "REMINDERS: When the user asks you to remind them of something, call "
    "set_reminder. Use minutes_from_now for relative times ('in an hour' → 60) "
    "and time for clock times, passing the time exactly as they said it "
    "('5:00 pm', 'noon'). Then confirm briefly using the 'when' from the "
    "result. Use list_reminders / cancel_reminder when they ask.\n"
    "A message starting with '[REMINDER]' means a reminder is due right now: "
    "speak up warmly and briefly, remind them, and ask whether they have done "
    "it (for medication: ask if they've taken it). When they answer, call "
    "record_reminder_response. If they haven't done it, encourage them, and "
    "offer to remind them again in a little while."
)


# ─── Time helpers ────────────────────────────────────────────────────────────
_TIME_RE = re.compile(
    r"^\s*(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>[ap])?\.?\s*(?:m\.?)?\s*$",
    re.IGNORECASE,
)


def parse_clock_time(text):
    """'5:00 pm' / '17:30' / '9am' / 'noon' → (hour, minute, has_am_pm).
    Raises ValueError if it can't be understood."""
    t = (text or "").strip().lower()
    t = t.replace("o'clock", "").replace("oclock", "").strip()
    if t in ("noon", "midday", "12 noon"):
        return 12, 0, True
    if t == "midnight":
        return 0, 0, True
    match = _TIME_RE.match(t)
    if not match:
        raise ValueError(f"Couldn't understand the time '{text}'")
    hour, minute = int(match.group("h")), int(match.group("m") or 0)
    ampm = match.group("ampm")
    if minute > 59 or hour > 23 or (ampm and not 1 <= hour <= 12):
        raise ValueError(f"'{text}' isn't a valid time")
    if ampm:
        hour = hour % 12 + (12 if ampm == "p" else 0)
    return hour, minute, bool(ampm)


def resolve_due(now, minutes_from_now=None, time_text=None, days_from_now=None):
    """Turn the tool arguments into a local due datetime (naive, local time)."""
    if minutes_from_now is not None:
        minutes = float(minutes_from_now)
        if not 0 < minutes <= MAX_MINUTES_AHEAD:
            raise ValueError("Reminders can be from 1 minute up to 7 days ahead.")
        return now + dt.timedelta(minutes=minutes)

    if not time_text:
        raise ValueError("Need either minutes_from_now or a time.")
    hour, minute, has_am_pm = parse_clock_time(time_text)

    def at(day_offset, h):
        day = (now + dt.timedelta(days=day_offset)).date()
        return dt.datetime.combine(day, dt.time(h, minute))

    if days_from_now is not None:
        due = at(int(days_from_now), hour)
        if not has_am_pm and hour < 12 and due <= now and at(int(days_from_now), hour + 12) > now:
            due = at(int(days_from_now), hour + 12)   # "at 5" said in the afternoon
        if due <= now:
            raise ValueError("That time has already passed.")
        return due

    # Next upcoming occurrence.  Without am/pm, "5:00" could be 5 am or 5 pm —
    # take whichever comes first.
    hours = [hour] if has_am_pm or hour == 0 or hour >= 12 else [hour, hour + 12]
    candidates = [at(d, h) for d in (0, 1) for h in hours]
    return min(c for c in candidates if c > now)


def describe_due(due, now):
    """'5:00 PM today' / 'tomorrow at 9:00 AM' / 'Friday, Oct 9 at 8:00 AM'."""
    clock = f"{due.hour % 12 or 12}:{due.minute:02d} {'AM' if due.hour < 12 else 'PM'}"
    days = (due.date() - now.date()).days
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"{due.strftime('%A, %b')} {due.day} at {clock}"


# ─── Manager ─────────────────────────────────────────────────────────────────
class ReminderManager:
    """Thread-safe reminder store.  `clock` returns epoch seconds (injectable
    for tests); `on_event(text)` receives log lines for the dashboard."""

    def __init__(self, path=REMINDERS_PATH, clock=time.time, on_event=None):
        self._path = path
        self._clock = clock
        self._on_event = on_event or (lambda text: None)
        self._lock = threading.RLock()
        self._last_user_speech = 0.0
        self._reminders = self._load()
        self._handle_missed()

    # ── Tool dispatch ────────────────────────────────────────────────────────
    def handle_tool_call(self, name, args):
        args = dict(args or {})
        handlers = {
            "set_reminder": lambda: self.set_reminder(
                args.get("task"), args.get("minutes_from_now"), args.get("time"),
                args.get("days_from_now"), bool(args.get("repeat_daily"))),
            "list_reminders": self.list_reminders,
            "cancel_reminder": lambda: self.cancel(args.get("task")),
            "record_reminder_response": lambda: self.record_response(
                args.get("task"), bool(args.get("done"))),
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return handler()
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            print(f"[REMINDER] {name} failed: {e}")
            return {"error": str(e)}

    # ── Commands ─────────────────────────────────────────────────────────────
    def set_reminder(self, task, minutes_from_now=None, time_text=None,
                     days_from_now=None, repeat_daily=False):
        task = (task or "").strip()
        if not task:
            raise ValueError("What should I remind them about?")
        now = self._now()
        due = resolve_due(now, minutes_from_now, time_text, days_from_now)
        reminder = {
            "id": uuid.uuid4().hex[:8],
            "task": task,
            "due": due.timestamp(),
            "repeat": "daily" if repeat_daily else "none",
            "status": "pending",
            "attempts": 0,
            "announced_at": None,
            "created": self._clock(),
        }
        with self._lock:
            self._reminders.append(reminder)
            self._save()
        when = describe_due(due, now)
        print(f"[REMINDER] Set '{task}' for {when}"
              f"{' (daily)' if repeat_daily else ''}", flush=True)
        self._on_event(f"[Reminder set] {task} — {when}"
                       f"{', every day' if repeat_daily else ''}")
        return {"status": "set", "task": task, "when": when,
                "repeats_daily": repeat_daily, "current_time": describe_due(now, now)}

    def list_reminders(self):
        now = self._now()
        with self._lock:
            upcoming = sorted((r for r in self._reminders if r["status"] == "pending"),
                              key=lambda r: r["due"])
            return {
                "current_time": describe_due(now, now),
                "reminders": [
                    {"task": r["task"],
                     "when": describe_due(dt.datetime.fromtimestamp(r["due"]), now),
                     "repeats_daily": r["repeat"] == "daily"}
                    for r in upcoming
                ],
            }

    def cancel(self, task):
        with self._lock:
            pending = [r for r in self._reminders if r["status"] == "pending"]
            match = self._match(pending, task)
            if match is None:
                return {"status": "not_found",
                        "reminders": [r["task"] for r in pending]}
            match["status"] = "cancelled"
            self._save()
        self._on_event(f"[Reminder cancelled] {match['task']}")
        return {"status": "cancelled", "task": match["task"]}

    def record_response(self, task, done):
        with self._lock:
            asked = sorted((r for r in self._reminders if r["status"] == "announced"),
                           key=lambda r: r["announced_at"] or 0, reverse=True)
            match = self._match(asked, task) if task else (asked[0] if asked else None)
            if match is None and asked:
                match = asked[0]    # only one thing was asked about recently
            if match is None:
                return {"status": "no_recent_reminder"}
            match["status"] = "done" if done else "not_done"
            match["responded_at"] = self._clock()
            self._save()
        outcome = "done" if done else "NOT done"
        print(f"[REMINDER] '{match['task']}' — user says {outcome}", flush=True)
        self._on_event(f"[Reminder response] {match['task']} — {outcome}")
        return {"status": "recorded", "task": match["task"], "done": done}

    # ── Main-loop integration ────────────────────────────────────────────────
    def user_spoke(self):
        """Call on every user transcription — a reminder the user has
        responded to (in any way) is not repeated."""
        self._last_user_speech = self._clock()

    def due(self):
        """Reminders that should be announced now (new, or repeats)."""
        now = self._clock()
        out = []
        with self._lock:
            for r in self._reminders:
                if r["status"] == "pending" and r["due"] <= now:
                    out.append(r)
                elif (r["status"] == "announced"
                      and now - r["announced_at"] >= RETRY_INTERVAL_SECONDS):
                    if self._last_user_speech >= r["announced_at"]:
                        # They answered but Gemini never recorded it — don't nag
                        r["status"] = "acknowledged"
                        self._save()
                    elif r["attempts"] <= MAX_RETRIES:
                        out.append(r)
                    else:
                        r["status"] = "no_response"
                        self._save()
                        self._on_event(f"[Reminder] No response to: {r['task']}")
        return out

    def prompt_for(self, reminders):
        now = self._now()
        tasks = "; ".join(r["task"] for r in reminders)
        clock = describe_due(now, now).replace(" today", "")
        if all(r["status"] == "announced" for r in reminders):
            return (f"[REMINDER] It's {clock}. A few minutes ago you reminded the user "
                    f"to: {tasks} — but they haven't answered. Gently check in again "
                    f"and ask whether they've done it.")
        return (f"[REMINDER] It's {clock}. The user asked you to remind them to: "
                f"{tasks}. Speak up now — remind them warmly and briefly, and ask "
                f"whether they've done it.")

    def mark_announced(self, reminders):
        now = self._clock()
        with self._lock:
            for r in reminders:
                first = r["status"] == "pending"
                r["status"] = "announced"
                r["announced_at"] = now
                r["attempts"] += 1
                if first and r["repeat"] == "daily":
                    self._schedule_next_day(r)
            self._prune(now)
            self._save()
        for r in reminders:
            self._on_event(f"[Reminder] {r['task']}"
                           f"{' (repeat)' if r['attempts'] > 1 else ''}")

    # ── Internals ────────────────────────────────────────────────────────────
    def _now(self):
        return dt.datetime.fromtimestamp(self._clock())

    def _schedule_next_day(self, r):
        due = dt.datetime.fromtimestamp(r["due"])
        now = self._now()
        while due <= now:
            due += dt.timedelta(days=1)   # keeps the wall-clock time across DST
        self._reminders.append({**r, "id": uuid.uuid4().hex[:8], "due": due.timestamp(),
                                "status": "pending", "attempts": 0, "announced_at": None})

    def _handle_missed(self):
        """At startup: reminders overdue by a lot (robot was off) are missed;
        recently-overdue ones still fire."""
        now = self._clock()
        with self._lock:
            changed = False
            for r in list(self._reminders):
                if r["status"] == "announced":
                    r["status"] = "no_response"     # can't still be waiting after a restart
                    changed = True
                if r["status"] == "pending" and now - r["due"] > MISSED_GRACE_SECONDS:
                    r["status"] = "missed"
                    changed = True
                    print(f"[REMINDER] Missed while offline: {r['task']}")
                    if r["repeat"] == "daily":
                        self._schedule_next_day(r)
            if changed:
                self._save()

    def _match(self, reminders, task):
        if not reminders:
            return None
        if not task:
            return reminders[0] if len(reminders) == 1 else None
        wanted = task.lower()
        filler = {"the", "your", "my", "and", "for", "take", "remind", "reminder", "about"}
        wanted_words = [w for w in re.findall(r"[a-z]+", wanted)
                        if len(w) >= 3 and w not in filler]
        best, best_score = None, 0.0
        for r in reminders:
            name = r["task"].lower()
            name_words = [n for n in re.findall(r"[a-z]+", name)
                          if len(n) >= 3 and n not in filler]
            # "meds" ~ "medication", "pills" ~ "pill": shared 3+ letter stem
            stem_match = any(len(os.path.commonprefix([w, n])) >= 3
                             for w in wanted_words for n in name_words)
            if wanted in name or name in wanted:
                score = 1.0
            elif stem_match:
                score = 0.8
            else:
                score = difflib.SequenceMatcher(None, wanted, name).ratio()
            if score > best_score:
                best, best_score = r, score
        return best if best_score >= 0.5 else None

    def _prune(self, now):
        self._reminders = [r for r in self._reminders
                           if r["status"] in ("pending", "announced")
                           or now - r["due"] < HISTORY_KEEP_SECONDS]

    def _load(self):
        try:
            with open(self._path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self):
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._reminders, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as e:
            print(f"[REMINDER] Could not save reminders: {e}")
