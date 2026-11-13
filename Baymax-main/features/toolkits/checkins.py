"""Gentle daily check-ins: Ember asks how someone slept, about water, about their day.

At most once per window per day (morning, afternoon, evening on the device-local
clock), only after Ember has been running a while (never at boot or as the
startup hello), only when it is quiet, and only when someone talked to Ember in
the last few hours, so it does not speak to an empty room. One attempt per
window: an unanswered check-in is not repeated. Answers are ordinary
conversation; nothing is saved or sent anywhere. The live session comes through
`fall_detection.session_tasks`; "done today" is kept in a small local file.
"""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import time

import runtime_monitor as monitor

NOTES = {
    "morning": "ask how they slept, or how their morning is going",
    "afternoon": "ask whether they have had some water today, or how their afternoon is going",
    "evening": "ask how their day was",
}
DEFAULT_WINDOWS = {"morning": "08:00-11:00", "afternoon": "14:00-16:30", "evening": "18:00-20:30"}
NOTE = ("[CHECK-IN] It is {name} and it has been quiet for a while. Check in gently, like a friend, "
        "in one short, warm sentence: {ask}. This is friendly conversation, not a health check: give "
        "no medical advice unless asked. If they do not answer or would rather not chat, let it go "
        "and do not ask again.")


def _setting(name, default):
    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return float(default)


def enabled():
    return os.getenv("BAYMAX_TOOLKIT_ENABLED", "1") != "0" and os.getenv("BAYMAX_CHECKINS", "1") != "0"


def window(text):
    """'08:00-11:00' -> (480, 660) minutes after midnight; blank, '0' or invalid -> None."""
    try:
        start, end = (datetime.strptime(part.strip(), "%H:%M") for part in text.split("-"))
    except (AttributeError, ValueError):
        return None
    start, end = start.hour * 60 + start.minute, end.hour * 60 + end.minute
    return (start, end) if start < end else None


def windows(env=os.environ):
    """Configured windows; BAYMAX_CHECKIN_MORNING=0 (or an invalid value) turns one off."""
    result = {}
    for name, default in DEFAULT_WINDOWS.items():
        span = window(env.get(f"BAYMAX_CHECKIN_{name.upper()}", default))
        if span is not None:
            result[name] = span
    return result


def _path():
    root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[2]))
    return root / "toolkit_checkins.json"


def _last_conversation():
    with monitor.lock:
        return max((t.get("updated_at", t["time"]) for t in monitor.transcripts), default=0.0)


def _book_playing():
    try:
        from .audiobooks import player
        return player.playing and not player.paused
    except Exception:
        return False


class CheckIns:
    def __init__(self, path=None, now=datetime.now, clock=time.monotonic, wall=time.time,
                 last_conversation=_last_conversation, book_playing=_book_playing):
        self.path = Path(path) if path else None
        self.now, self.clock, self.wall = now, clock, wall
        self.last_conversation, self.book_playing = last_conversation, book_playing
        self.started = None   # First live session; reconnects keep it, a restart resets it.
        self.sleep = asyncio.sleep

    def file(self):
        return self.path or _path()

    def done(self, day):
        try:
            data = json.loads(self.file().read_text())
        except (OSError, ValueError):
            return set()
        return set(data.get("done", [])) if data.get("date") == day else set()

    def mark(self, day, name):
        path = self.file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"date": day, "done": sorted(self.done(day) | {name})}))
        except OSError:
            print("[CHECK-IN] Could not save today's check-ins; may ask again after a restart.", flush=True)

    def due(self):
        """(window name, note) to say now, or None."""
        if not enabled() or self.started is None:
            return None
        if self.clock() - self.started < _setting("BAYMAX_CHECKIN_UPTIME_S", 20 * 60):
            return None  # Never at boot: the startup hello is the only greeting then.
        now = self.now()
        minute = now.hour * 60 + now.minute
        name = next((n for n, (start, end) in windows().items() if start <= minute < end), None)
        if name is None or name in self.done(now.date().isoformat()):
            return None
        # Someone is likely around: they talked with Ember in the last few hours,
        # but not in the last few minutes (do not cut into a conversation or its pause).
        quiet = self.wall() - self.last_conversation()
        if quiet < _setting("BAYMAX_CHECKIN_QUIET_S", 5 * 60) or quiet > _setting("BAYMAX_CHECKIN_AROUND_S", 3 * 3600):
            return None
        if self.book_playing() or monitor.state in ("speaking", "thinking", "calling_tool"):
            return None
        return name, NOTE.format(name=f"the {name}", ask=NOTES[name])

    async def session_task(self, session, shutdown_event):
        if self.started is None:
            self.started = self.clock()
        while not shutdown_event.is_set():
            pending = self.due()
            if pending:
                name, note = pending
                try:
                    await session.send_client_content(turns={"role": "user", "parts": [{"text": note}]},
                                                      turn_complete=True)
                except Exception as error:
                    print(f"[CHECK-IN] Could not reach Gemini: {error}", flush=True)
                else:
                    # One attempt per window, answered or not.
                    self.mark(self.now().date().isoformat(), name)
                    print(f"[CHECK-IN] {name} check-in", flush=True)
                    monitor.record("check-in", f"{name.capitalize()} check-in")
            await self.sleep(30)


checkins = CheckIns()


def register():
    from .fall_detection import session_tasks
    if checkins.session_task not in session_tasks:
        session_tasks.append(checkins.session_task)


register()
