"""Small transactional local store; no network or device access."""
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def label(value, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Use nonempty text of at most {maximum} characters")
    return value.strip()


def identifier(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Use the numeric ID returned by the tool")
    return value


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(label(value, 60))
        if parsed.utcoffset() is None:
            raise ValueError
        result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, OverflowError):
        raise ValueError("Use an ISO date/time with a UTC offset; clarify the user's timezone") from None


def iso(value):
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path=None, clock=time.time):
        root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[3]))
        self.path = Path(path) if path else root / "toolkit_everyday.sqlite3"
        self.clock = clock

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending')")
            connection.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, text TEXT NOT NULL, UNIQUE(name,text))")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def now(self):
        return {"now": iso(self.clock()), "timezone": "Device's configured timezone; ask if the user's differs"}

    def reminder(self, action, text=None, seconds=None, due_at=None, id=None):
        if action == "create":
            text = label(text)
            if id is not None or (seconds is None) == (due_at is None):
                raise ValueError("Provide exactly one of seconds or due_at, and no ID")
            now = self.clock()
            if seconds is not None:
                if type(seconds) is not int or not 1 <= seconds <= 31_536_000:
                    raise ValueError("Duration must be 1-31536000 whole seconds")
                due = now + seconds
            else:
                due = timestamp(due_at)
            if not now < due <= now + 31_536_000:
                raise ValueError("Choose a future time within one year")
            with self.db() as db:
                if db.execute("SELECT count(*) FROM reminders WHERE state='pending'").fetchone()[0] >= 100:
                    raise ValueError("At most 100 pending reminders; cancel one first")
                result = db.execute("INSERT INTO reminders(text,due) VALUES (?,?)", (text, due))
                return {"saved": True, "id": result.lastrowid, "text": text, "due_at": iso(due),
                        "notice": "Shared device reminder. Spoken alerts need Ember running and connected; overdue reminders are announced on reconnection."}
        if any(value is not None for value in (text, seconds, due_at)):
            raise ValueError("List/cancel does not accept reminder text or time")
        if action == "list" and id is None:
            with self.db() as db:
                return {"reminders": [self.record(r) for r in db.execute("SELECT * FROM reminders WHERE state='pending' ORDER BY due,id")]}
        if action == "cancel":
            identifier(id)
            with self.db() as db:
                result = db.execute("DELETE FROM reminders WHERE id=? AND state='pending'", (id,))
                return {"cancelled": bool(result.rowcount), "id": id}
        raise ValueError("Use create, list or cancel with its supported arguments")

    @staticmethod
    def record(row):
        return {"id": row["id"], "text": row["text"], "due_at": iso(row["due"])}

    def due(self):
        with self.db() as db:
            return [self.record(r) for r in db.execute("SELECT * FROM reminders WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 5", (self.clock(),))]

    def delivered(self, ids):
        with self.db() as db:
            # Delete delivered items: no private reminder history retained.
            db.executemany("DELETE FROM reminders WHERE id=?", [(identifier(i),) for i in ids])

    def items(self, action, name="shopping", text=None, id=None):
        name = label(name, 40).casefold()
        if action not in ("add", "show", "remove", "clear"):
            raise ValueError("Use add, show, remove or clear")
        if action == "add":
            text = label(text)
            if id is not None:
                raise ValueError("Add does not accept an ID")
        elif text is not None or (action != "remove" and id is not None):
            raise ValueError("Unsupported list arguments")
        if action == "remove":
            identifier(id)
        with self.db() as db:
            if action == "add":
                count = db.execute("SELECT count(*) FROM items WHERE name=?", (name,)).fetchone()[0]
                if count >= 100 or (count == 0 and db.execute("SELECT count(DISTINCT name) FROM items").fetchone()[0] >= 20):
                    raise ValueError("At most 20 lists and 100 items per list")
                db.execute("INSERT OR IGNORE INTO items(name,text) VALUES (?,?)", (name, text))
            elif action == "remove":
                changed = db.execute("DELETE FROM items WHERE name=? AND id=?", (name, id)).rowcount
            elif action == "clear":
                db.execute("DELETE FROM items WHERE name=?", (name,))
            result = {"name": name, "items": [dict(r) for r in db.execute("SELECT id,text FROM items WHERE name=? ORDER BY id", (name,))], "shared": True}
            if action == "remove":
                result["removed"] = bool(changed)
            if action in ("add", "clear"):
                result["saved"] = True
            return result
