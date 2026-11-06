"""Small transactional local store; no network or device access.

Reminders can repeat (recurrence.py). Every announcement is kept as an
occurrence with its check-in answer (done / not done / no answer), so the
dashboard can show a history. Reminders and occurrences carry a revision
number; the cloud sync uploads rows whose revision it hasn't seen yet.
"""
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import recurrence

PENDING_LIMIT = 100
REASK_AFTER = 600        # seconds without an answer before asking once more
GIVE_UP_AFTER = 600      # seconds after the second ask before logging "no answer"
KEEP_HISTORY = 180 * 86400

REMINDER_COLUMNS = {   # added to databases made before repeats existed
    "rule": "TEXT", "start": "TEXT", "tz": "TEXT", "source": "TEXT NOT NULL DEFAULT 'voice'",
    "created_by": "TEXT", "cloud_id": "TEXT", "created_at": "REAL", "updated_at": "REAL",
    "rev": "INTEGER NOT NULL DEFAULT 1", "synced_rev": "INTEGER NOT NULL DEFAULT 0",
}


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
    def __init__(self, path=None, clock=time.time, tz=None):
        root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[3]))
        self.path = Path(path) if path else root / "toolkit_everyday.sqlite3"
        self.clock = clock
        self.tz = tz or recurrence.device_timezone()

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending')")
            connection.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, text TEXT NOT NULL, UNIQUE(name,text))")
            connection.execute("BEGIN IMMEDIATE")
            self._schema(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _schema(self, db):
        have = {r["name"] for r in db.execute("PRAGMA table_info(reminders)")}
        missing = [c for c in REMINDER_COLUMNS if c not in have]
        for column in missing:
            db.execute(f"ALTER TABLE reminders ADD COLUMN {column} {REMINDER_COLUMNS[column]}")
        if missing:
            # Reminders saved before repeats existed: one-time, set by voice.
            now = self.clock()
            for row in db.execute("SELECT id, due FROM reminders WHERE start IS NULL").fetchall():
                db.execute("UPDATE reminders SET start=?, tz=?, created_at=?, updated_at=? WHERE id=?",
                           (recurrence.local_start(row["due"], self.tz), self.tz, now, now, row["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS reminders_cloud_id ON reminders(cloud_id) WHERE cloud_id IS NOT NULL")
        db.execute("CREATE TABLE IF NOT EXISTS occurrences (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "reminder_id INTEGER NOT NULL, due REAL NOT NULL, announced_at REAL, reasked_at REAL, "
                   "outcome TEXT, answered_at REAL, rev INTEGER NOT NULL DEFAULT 1, synced_rev INTEGER NOT NULL DEFAULT 0)")

    def now(self):
        return {"now": iso(self.clock()), "timezone": self.tz,
                "note": "Device's configured timezone; ask if the user's differs"}

    # ── Reminders ────────────────────────────────────────────────────────────
    def reminder(self, action, text=None, seconds=None, due_at=None, id=None, repeat=None,
                 occurrence=None, outcome=None):
        """The voice tool: create, list, cancel, or answer a check-in."""
        if action == "create":
            text = label(text)
            if id is not None or occurrence is not None or outcome is not None or (seconds is None) == (due_at is None):
                raise ValueError("Provide exactly one of seconds or due_at, and no ID")
            now = self.clock()
            if seconds is not None:
                if type(seconds) is not int or not 1 <= seconds <= 31_536_000:
                    raise ValueError("Duration must be 1-31536000 whole seconds")
                due = now + seconds
            else:
                due = timestamp(due_at)
            rule = recurrence.normalize(repeat)
            if rule is not None:
                due = math.ceil(due / 60) * 60   # repeats run on whole minutes
            with self.db() as db:
                rid, first = self._create(db, text, due, rule, "voice", None, None)
                saved = {"saved": True, "id": rid, "text": text, "due_at": iso(first),
                         "notice": "Shared device reminder. Spoken alerts need Ember running and connected; overdue reminders are announced on reconnection."}
                if rule is not None:
                    saved["repeat"] = recurrence.describe(rule, recurrence.local_start(due, self.tz), self.tz)
                return saved
        if any(value is not None for value in (text, seconds, due_at, repeat)):
            raise ValueError("List/cancel/answer do not accept reminder text, time or repeat")
        if action == "list" and id is None and occurrence is None and outcome is None:
            with self.db() as db:
                return {"reminders": [self.describe_row(r) for r in
                                      db.execute("SELECT * FROM reminders WHERE state='pending' ORDER BY due,id")]}
        if action == "cancel" and occurrence is None and outcome is None:
            identifier(id)
            with self.db() as db:
                return {"cancelled": self._cancel(db, "id", id), "id": id}
        if action == "answer" and id is None:
            identifier(occurrence)
            if outcome not in ("done", "not_done"):
                raise ValueError("outcome is done or not_done")
            with self.db() as db:
                changed = db.execute(
                    "UPDATE occurrences SET outcome=?, answered_at=?, rev=rev+1 "
                    "WHERE id=? AND announced_at IS NOT NULL AND outcome IS NULL",
                    (outcome, self.clock(), occurrence)).rowcount
                return {"recorded": bool(changed), "occurrence": occurrence, "outcome": outcome}
        raise ValueError("Use create, list, cancel or answer with its supported arguments")

    def _create(self, db, text, due, rule, source, created_by, cloud_id, start=None):
        """@return (id, first due). `start` is a local start time (dashboard); else from `due`."""
        now = self.clock()
        start = start or recurrence.local_start(due, self.tz)
        if rule is not None or start is not None and due is None:
            due = recurrence.next_after(rule, start, self.tz, now)
        if due is None or not now < due <= now + 31_536_000:
            raise ValueError("Choose a future time within one year")
        if db.execute("SELECT count(*) FROM reminders WHERE state='pending'").fetchone()[0] >= PENDING_LIMIT:
            raise ValueError("At most 100 pending reminders; cancel one first")
        result = db.execute(
            "INSERT INTO reminders(text,due,rule,start,tz,source,created_by,cloud_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (text, due, json.dumps(rule) if rule else None, start, self.tz, source, created_by, cloud_id, now, now))
        return result.lastrowid, due

    def _cancel(self, db, column, value):
        row = db.execute(f"SELECT id FROM reminders WHERE {column}=? AND state='pending'", (value,)).fetchone()
        if row is None:
            return False
        db.execute("UPDATE reminders SET state='cancelled', updated_at=?, rev=rev+1 WHERE id=?", (self.clock(), row["id"]))
        db.execute("DELETE FROM occurrences WHERE reminder_id=? AND announced_at IS NULL", (row["id"],))
        return True

    def describe_row(self, row):
        rule = json.loads(row["rule"]) if row["rule"] else None
        result = {"id": row["id"], "text": row["text"], "due_at": iso(row["due"]),
                  "set_by": "dashboard" if row["source"] == "dashboard" else "voice"}
        if rule is not None:
            result["repeat"] = recurrence.describe(rule, row["start"], row["tz"] or self.tz)
        return result

    @staticmethod
    def record(row):
        return {"id": row["id"], "text": row["text"], "due_at": iso(row["due"])}

    def due(self):
        """Due reminders (at most 5), each with the occurrence its check-in answers."""
        now = self.clock()
        with self.db() as db:
            found = []
            for row in db.execute("SELECT * FROM reminders WHERE state='pending' AND due<=? ORDER BY due,id LIMIT 5", (now,)).fetchall():
                occ = db.execute("SELECT id FROM occurrences WHERE reminder_id=? AND due=? AND announced_at IS NULL",
                                 (row["id"], row["due"])).fetchone()
                occ_id = occ["id"] if occ else db.execute(
                    "INSERT INTO occurrences(reminder_id,due) VALUES (?,?)", (row["id"], row["due"])).lastrowid
                item = self.describe_row(row)
                item["occurrence"] = occ_id
                if now - row["due"] > 300:
                    item["was_due"] = iso(row["due"])
                found.append(item)
            return found

    def delivered(self, occurrence_ids):
        """Gemini took the announcement: log it and move each reminder on."""
        now = self.clock()
        with self.db() as db:
            for occ_id in occurrence_ids:
                occ = db.execute("SELECT * FROM occurrences WHERE id=?", (identifier(occ_id),)).fetchone()
                if occ is None or occ["announced_at"] is not None:
                    continue
                db.execute("UPDATE occurrences SET announced_at=?, rev=rev+1 WHERE id=?", (now, occ_id))
                # An earlier occurrence still unanswered won't be answered now.
                db.execute("UPDATE occurrences SET outcome='no_answer', rev=rev+1 WHERE reminder_id=? AND id<>? "
                           "AND announced_at IS NOT NULL AND outcome IS NULL", (occ["reminder_id"], occ_id))
                row = db.execute("SELECT * FROM reminders WHERE id=?", (occ["reminder_id"],)).fetchone()
                if row is None or row["state"] != "pending":
                    continue
                rule = json.loads(row["rule"]) if row["rule"] else None
                upcoming = recurrence.next_after(rule, row["start"], row["tz"] or self.tz, max(now, row["due"])) if rule else None
                if upcoming is None:
                    db.execute("UPDATE reminders SET state=?, updated_at=?, rev=rev+1 WHERE id=?",
                               ("ended" if rule else "announced", now, row["id"]))
                else:
                    db.execute("UPDATE reminders SET due=?, updated_at=?, rev=rev+1 WHERE id=?", (upcoming, now, row["id"]))

    def followups(self):
        """Announced but unanswered: due a second ask (returned), or logged as no answer."""
        now = self.clock()
        with self.db() as db:
            db.execute("UPDATE occurrences SET outcome='no_answer', rev=rev+1 WHERE outcome IS NULL "
                       "AND reasked_at IS NOT NULL AND reasked_at<=?", (now - GIVE_UP_AFTER,))
            rows = db.execute("SELECT o.id AS occurrence, r.text FROM occurrences o JOIN reminders r ON r.id=o.reminder_id "
                              "WHERE o.outcome IS NULL AND o.reasked_at IS NULL AND o.announced_at IS NOT NULL "
                              "AND o.announced_at<=? ORDER BY o.id LIMIT 5", (now - REASK_AFTER,)).fetchall()
            return [dict(r) for r in rows]

    def reasked(self, occurrence_ids):
        with self.db() as db:
            db.executemany("UPDATE occurrences SET reasked_at=?, rev=rev+1 WHERE id=? AND reasked_at IS NULL",
                           [(self.clock(), identifier(i)) for i in occurrence_ids])

    # ── Dashboard (cloud inbox) ──────────────────────────────────────────────
    def dashboard_create(self, cloud_id, text, start, repeat, created_by):
        """A reminder made on the dashboard; `start` is local "YYYY-MM-DDTHH:MM". Idempotent."""
        cloud_id, text = label(cloud_id, 64), label(text)
        created_by = label(created_by, 80) if created_by else None
        rule = recurrence.normalize(repeat)
        start = recurrence.local_start(recurrence.parse_start(label(start, 25), self.tz).timestamp(), self.tz)
        with self.db() as db:
            row = db.execute("SELECT id FROM reminders WHERE cloud_id=?", (cloud_id,)).fetchone()
            if row:
                return row["id"]
            due = recurrence.parse_start(start, self.tz).timestamp() if rule is None else None
            return self._create(db, text, due, rule, "dashboard", created_by, cloud_id, start=start)[0]

    def dashboard_cancel(self, id=None, cloud_id=None):
        with self.db() as db:
            if cloud_id is not None:
                return self._cancel(db, "cloud_id", label(cloud_id, 64))
            return self._cancel(db, "id", identifier(id))

    # ── Sync ─────────────────────────────────────────────────────────────────
    def changes(self, limit=200):
        """Rows the cloud hasn't seen at their current revision."""
        with self.db() as db:
            reminders = [{
                "robot_id": r["id"], "cloud_id": r["cloud_id"], "text": r["text"], "start": r["start"],
                "tz": r["tz"] or self.tz, "rule": json.loads(r["rule"]) if r["rule"] else None,
                "next_due": r["due"] if r["state"] == "pending" else None, "state": r["state"],
                "source": r["source"], "created_by": r["created_by"], "created_at": r["created_at"],
                "updated_at": r["updated_at"], "rev": r["rev"],
            } for r in db.execute("SELECT * FROM reminders WHERE rev>synced_rev ORDER BY id LIMIT ?", (limit,))]
            occurrences = [{
                "robot_id": o["id"], "reminder_robot_id": o["reminder_id"], "due": o["due"],
                "announced_at": o["announced_at"], "outcome": o["outcome"], "answered_at": o["answered_at"],
                "rev": o["rev"],
            } for o in db.execute("SELECT * FROM occurrences WHERE rev>synced_rev AND announced_at IS NOT NULL "
                                  "ORDER BY id LIMIT ?", (limit,))]
            return {"tz": self.tz, "reminders": reminders, "occurrences": occurrences}

    def synced(self, reminders=(), occurrences=()):
        """Mark (id, rev) pairs as uploaded; later edits raise rev again and are re-sent."""
        with self.db() as db:
            db.executemany("UPDATE reminders SET synced_rev=max(synced_rev,?) WHERE id=?", [(rev, i) for i, rev in reminders])
            db.executemany("UPDATE occurrences SET synced_rev=max(synced_rev,?) WHERE id=?", [(rev, i) for i, rev in occurrences])
            # Old history the cloud already has.
            cutoff = self.clock() - KEEP_HISTORY
            db.execute("DELETE FROM occurrences WHERE rev<=synced_rev AND coalesce(answered_at, announced_at, due)<?", (cutoff,))
            db.execute("DELETE FROM reminders WHERE state<>'pending' AND rev<=synced_rev AND updated_at<? "
                       "AND id NOT IN (SELECT reminder_id FROM occurrences)", (cutoff,))

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
