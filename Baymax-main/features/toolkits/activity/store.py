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
           "hand_to_mouth", "talking_s", "samples", "restless", "open")
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
            hand_to_mouth INTEGER, talking_s REAL, samples INTEGER, restless INTEGER DEFAULT 0,
            open INTEGER NOT NULL DEFAULT 0)""")
        if "restless" not in _present(self.db):   # Stores made before restless nights.
            self.db.execute("ALTER TABLE segments ADD COLUMN restless INTEGER DEFAULT 0")
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


def _present(db):
    return {row[1] for row in db.execute("PRAGMA table_info(segments)")}


def _between(db, start, end):
    # devUI reads older stores read-only, so they may lack newer columns.
    columns = [c for c in COLUMNS if c in _present(db)]
    names = ", ".join(f'"{c}"' for c in columns)
    rows = db.execute(f'SELECT {names} FROM segments WHERE "end" > ? AND start < ? ORDER BY start',
                      (start, end)).fetchall()
    return [{**{c: None for c in COLUMNS}, **dict(zip(columns, row))} for row in rows]


def day_bounds(date_text):
    """Local midnight to midnight for YYYY-MM-DD, as epoch seconds."""
    day = datetime.strptime(date_text, "%Y-%m-%d").date()
    start = datetime.combine(day, day_time()).astimezone()
    end = datetime.combine(day + timedelta(days=1), day_time()).astimezone()
    return start.timestamp(), end.timestamp()


def _read(path, start, end):
    """Segments overlapping [start, end) from the store, read-only, or [] when there is none.
    devUI must never create, change or recover the live store."""
    path = Path(path or default_path())
    if not path.exists():
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        return _between(db, start, end)
    finally:
        db.close()


def timeline(date_text, path=None):
    """One local day's segments (clipped to the day) and seconds per hint, for devUI."""
    start, end = day_bounds(date_text)
    segments = _read(path, start, end)
    totals = {}
    for segment in segments:
        segment["start"], segment["end"] = max(segment["start"], start), min(segment["end"], end)
        totals[segment["hint"]] = totals.get(segment["hint"], 0) + segment["end"] - segment["start"]
    return {"date": date_text, "segments": segments, "totals": {k: round(v) for k, v in totals.items()}}


# --- Nights ---------------------------------------------------------------------------------
RESTING = ("lying", "out_of_sight")
NIGHT_FROM_H, NIGHT_TO_H = 18, 12   # The night before a day: 18:00 the day before to 12:00.
NIGHT_BREAK_S = 45 * 60    # Rest separated by less than this is one block (a get-up, not waking).
NIGHT_MIN_S = 2 * 3600     # The main rest block must be at least this long to count as the night.
RESTLESS_NIGHT_GET_UPS = 3          # Provisional "restless night": this many get-ups...
RESTLESS_NIGHT_PER_HOUR = 6.0       # ...or this many restless spells per hour of visible lying.


def night_bounds(date_text):
    day = datetime.strptime(date_text, "%Y-%m-%d").date()
    start = datetime.combine(day - timedelta(days=1), day_time(NIGHT_FROM_H)).astimezone()
    end = datetime.combine(day, day_time(NIGHT_TO_H)).astimezone()
    return start.timestamp(), end.timestamp()


def night_summary(segments, start, end):
    """The main rest block between start and end, from segment rows, or None.

    Rest = lying or resting out of sight. Rest stretches less than NIGHT_BREAK_S apart join
    one block; the longest block of at least NIGHT_MIN_S is the night. A get-up is a break
    inside it where the person was seen up or left the view (a gap without data isn't)."""
    rows = []
    for s in sorted(segments, key=lambda r: r["start"]):
        a, b = max(s["start"], start), min(s["end"], end)
        if b > a:
            rows.append({**s, "start": a, "end": b})
    blocks = []
    for i, s in enumerate(rows):
        if s["posture"] not in RESTING:
            continue
        if blocks and s["start"] - rows[blocks[-1][1]]["end"] <= NIGHT_BREAK_S:
            blocks[-1][1] = i
        else:
            blocks.append([i, i])
    def rest_s(block):
        return sum(r["end"] - r["start"] for r in rows[block[0]:block[1] + 1] if r["posture"] in RESTING)
    blocks = [b for b in blocks if rows[b[1]]["end"] - rows[b[0]]["start"] >= NIGHT_MIN_S]
    if not blocks:
        return None
    first, last = max(blocks, key=rest_s)
    inside = rows[first:last + 1]
    get_ups, up_s, previous_rest = 0, 0.0, True
    for r in inside:
        rest = r["posture"] in RESTING
        if not rest:
            up_s += r["end"] - r["start"]
            if previous_rest:
                get_ups += 1
        previous_rest = rest
    lying_s = sum(r["end"] - r["start"] for r in inside if r["posture"] == "lying")
    hidden_s = sum(r["end"] - r["start"] for r in inside if r["posture"] == "out_of_sight")
    restless = sum(r.get("restless") or 0 for r in inside if r["posture"] == "lying")
    per_hour = restless / (lying_s / 3600) if lying_s >= 600 else None
    return {
        "bedtime": inside[0]["start"], "wake": inside[-1]["end"],
        "rest_s": round(lying_s + hidden_s), "out_of_sight_s": round(hidden_s),
        "get_ups": get_ups, "up_s": round(up_s),
        "restless": restless, "restless_per_hour": None if per_hour is None else round(per_hour, 1),
        "restless_night": get_ups >= RESTLESS_NIGHT_GET_UPS
        or (per_hour is not None and per_hour >= RESTLESS_NIGHT_PER_HOUR),
    }


def night(date_text, path=None):
    """The night before a local day (18:00 the day before to 12:00), for devUI."""
    start, end = night_bounds(date_text)
    return night_summary(_read(path, start, end), start, end)
