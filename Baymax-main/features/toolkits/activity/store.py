"""Local SQLite store for activity segments. Numbers and labels only, never images.

The file is toolkit_activity.sqlite3 next to the code, or under BAYMAX_DATA_DIR.
WAL mode lets devUI read while Ember writes. Shadow mode: nothing here is sent
anywhere yet.
"""
from datetime import datetime, time as day_time, timedelta
import os
from pathlib import Path
import sqlite3
import threading

ROOT = Path(__file__).resolve().parents[3]
RETENTION_DAYS = 90

COLUMNS = ("id", "start", "end", "posture", "hint", "confidence", "still_share", "movement",
           "hand_to_mouth", "talking_s", "samples", "open")
NAMES = ", ".join(f'"{c}"' for c in COLUMNS)  # "end" is an SQL keyword.


def default_path():
    return Path(os.getenv("BAYMAX_DATA_DIR") or ROOT) / "toolkit_activity.sqlite3"


def retention_days():
    try:
        return max(1, int(os.getenv("BAYMAX_ACTIVITY_RETENTION_DAYS", str(RETENTION_DAYS))))
    except ValueError:
        return RETENTION_DAYS


class Store:
    def __init__(self, path=None):
        self.path = Path(path or default_path())
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS segments (
            id TEXT PRIMARY KEY, start REAL NOT NULL, "end" REAL NOT NULL, posture TEXT NOT NULL,
            hint TEXT NOT NULL, confidence REAL, still_share REAL, movement REAL,
            hand_to_mouth INTEGER, talking_s REAL, samples INTEGER, open INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS segments_start ON segments(start)")
        self.db.commit()

    def recover(self):
        """A segment left open by a crash or power cut ends where it was last saved."""
        with self.lock:
            self.db.execute("UPDATE segments SET open = 0 WHERE open = 1")
            self.db.commit()

    def save(self, segment, is_open=False):
        row = {**segment, "open": int(is_open)}
        with self.lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO segments ({NAMES}) VALUES ({', '.join('?' for _ in COLUMNS)})",
                [row.get(c) for c in COLUMNS])
            self.db.commit()

    def prune(self, now, days=None):
        cutoff = now - (days or retention_days()) * 86400
        with self.lock:
            removed = self.db.execute('DELETE FROM segments WHERE "end" < ?', (cutoff,)).rowcount
            self.db.commit()
        return removed

    def between(self, start, end):
        with self.lock:
            return _between(self.db, start, end)

    def close(self):
        with self.lock:
            self.db.close()


def _between(db, start, end):
    rows = db.execute(f'SELECT {NAMES} FROM segments WHERE "end" > ? AND start < ? ORDER BY start',
                      (start, end)).fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def day_bounds(date_text):
    """Local midnight to midnight for YYYY-MM-DD, as epoch seconds."""
    day = datetime.strptime(date_text, "%Y-%m-%d").date()
    start = datetime.combine(day, day_time()).astimezone()
    end = datetime.combine(day + timedelta(days=1), day_time()).astimezone()
    return start.timestamp(), end.timestamp()


def timeline(date_text, path=None):
    """One local day's segments (clipped to the day) and seconds per hint, for devUI."""
    start, end = day_bounds(date_text)
    path = Path(path or default_path())
    if not path.exists():
        return {"date": date_text, "segments": [], "totals": {}}
    # Read-only: devUI must never create, change or recover the live store.
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        segments = _between(db, start, end)
    finally:
        db.close()
    totals = {}
    for segment in segments:
        segment["start"], segment["end"] = max(segment["start"], start), min(segment["end"], end)
        totals[segment["hint"]] = totals.get(segment["hint"], 0) + segment["end"] - segment["start"]
    return {"date": date_text, "segments": segments, "totals": {k: round(v) for k, v in totals.items()}}
