"""Night activity: one `night_wake` care event per episode of the resident talking during the night.

Uses only what the runtime already has: the user speech transcripts in `runtime_monitor`.
It opens no microphone or camera and takes neither of the monitor's single observer slots;
a session task reads the transcript list every 30 seconds. Episode state lives in the care
log's settings table, so an episode survives a restart in the middle of the night. See FLAGS.md.
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta

from . import store

STATE_KEY = "night_tracker"
DEFAULT_WINDOW = "23:00-06:00"


def window(value=None):
    """((start_h, start_m), (end_h, end_m)) from "HH:MM-HH:MM", or None when switched off."""
    value = (os.getenv("EMBER_CARE_NIGHT", DEFAULT_WINDOW) if value is None else value).strip()
    if value.lower() in ("", "0", "off", "none"):
        return None
    try:
        start, end = value.split("-")
        bounds = tuple(tuple(int(part) for part in side.strip().split(":")) for side in (start, end))
        if any(len(b) != 2 or not (0 <= b[0] < 24 and 0 <= b[1] < 60) for b in bounds) or bounds[0] == bounds[1]:
            raise ValueError
        return bounds
    except ValueError:
        print(f"[CARE] EMBER_CARE_NIGHT={value!r} is not HH:MM-HH:MM; using {DEFAULT_WINDOW}.", flush=True)
        return window(DEFAULT_WINDOW)


def gap_seconds():
    try:
        return max(1.0, float(os.getenv("EMBER_CARE_NIGHT_GAP_MINUTES", "30"))) * 60
    except ValueError:
        return 1800.0


def in_night(at, bounds):
    """True if epoch `at` falls in the local night window (which may cross midnight)."""
    moment = datetime.fromtimestamp(at)
    now, start, end = (moment.hour, moment.minute), bounds[0], bounds[1]
    return start <= now < end if start < end else (now >= start or now < end)


def night_of(at):
    """The date a night is filed under: the evening it began (02:00 Saturday belongs to Friday)."""
    moment = datetime.fromtimestamp(at)
    return (moment.date() - timedelta(days=1) if moment.hour < 12 else moment.date()).isoformat()


def _read(db):
    row = db.execute("SELECT value FROM settings WHERE key=?", (STATE_KEY,)).fetchone()
    return json.loads(row["value"]) if row else {"last_seen": None, "episode": None}


def _write(db, state):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (STATE_KEY, json.dumps(state)))


def update(log, times, now, bounds=None, gap=None):
    """Fold new user-speech start times into episodes; returns the IDs of night_wake events recorded.

    Read-modify-write in one locked transaction, so the runtime and the dashboard process
    (which closes stale episodes) never record the same episode twice.
    """
    bounds = bounds if bounds is not None else window()
    gap = gap if gap is not None else gap_seconds()
    if bounds is None or not store.enabled():
        return []
    closed = []
    with log.db() as db:
        state = _read(db)
        last_seen, episode = state.get("last_seen"), state.get("episode")
        for at in sorted(t for t in times if last_seen is None or t > last_seen):
            last_seen = at
            if not in_night(at, bounds):
                continue
            if episode and at - episode["end"] <= gap:
                episode.update(end=at, count=episode["count"] + 1)
            else:
                if episode:
                    closed.append(episode)
                episode = {"start": at, "end": at, "count": 1, "night": night_of(at)}
        if episode and now - episode["end"] > gap:
            closed.append(episode)
            episode = None
        _write(db, {"last_seen": last_seen, "episode": episode})
    return [i for i in (_record(log, e) for e in closed) if i is not None]


def close_stale(log, now=None):
    """Record an episode left open when the runtime stopped (called from the dashboard loop)."""
    return update(log, [], log.clock() if now is None else now)


def _record(log, episode):
    start, end = datetime.fromtimestamp(episode["start"]), datetime.fromtimestamp(episode["end"])
    count = episode["count"]
    span = f"at {start:%H:%M}" if end - start < timedelta(minutes=1) else f"from {start:%H:%M} to {end:%H:%M}"
    summary = f"Awake at night and talking with Ember {span} ({count} time{'s' if count != 1 else ''})"
    try:
        return log.record("night_wake", summary, status="info", data={
            "night": episode["night"], "start": store.iso(episode["start"]), "end": store.iso(episode["end"]),
            "utterances": count, "source": "speech_transcripts"})
    except Exception as error:
        print(f"[CARE] Could not record night activity: {type(error).__name__}", flush=True)
        return None


def user_speech_times():
    """Start times of the resident's recent utterances, from the monitor's existing transcript list."""
    import runtime_monitor as monitor
    with monitor.lock:
        return [t["time"] for t in monitor.transcripts if t.get("speaker") == "user" and (t.get("text") or "").strip()]


async def session_task(session, shutdown_event):
    while not shutdown_event.is_set():
        try:
            if store.enabled() and window() is not None:
                await asyncio.to_thread(update, store.log, user_speech_times(), time.time())
        except Exception as error:
            print(f"[CARE] Night activity check failed: {type(error).__name__}", flush=True)
        # shutdown_event is a threading.Event: never call its wait() here, it blocks the event loop.
        await asyncio.sleep(30)


def register():
    from features.toolkits.fall_detection import session_tasks
    if session_task not in session_tasks:
        session_tasks.append(session_task)
