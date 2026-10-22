"""Messages from the dashboard, replies to them and the person's profile
summary, kept on the robot until the cloud has them. SQLite; no network."""
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MESSAGE_MAX = 500
KEEP_READ = 30 * 86400   # read and synced messages are kept this long


class FamilyStore:
    def __init__(self, path=None, clock=time.time):
        root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[3]))
        self.path = Path(path) if path else root / "toolkit_family.sqlite3"
        self.clock = clock

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT, text TEXT NOT NULL, "
                               "created_at TEXT, read_out_at REAL, synced INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY, reply_to TEXT, text TEXT NOT NULL, "
                               "at REAL NOT NULL, synced INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK (id = 1), summary TEXT, "
                               "for_updated_at TEXT, synced INTEGER NOT NULL DEFAULT 0)")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ── Messages ──
    def add_messages(self, messages):
        with self.db() as db:
            for m in messages:
                if isinstance(m.get("id"), str) and isinstance(m.get("text"), str):
                    db.execute("INSERT OR IGNORE INTO messages (id, sender, text, created_at) VALUES (?,?,?,?)",
                               (m["id"], m.get("sender_name"), m["text"][:MESSAGE_MAX], m.get("created_at")))

    def unread(self, limit=3):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, sender, text, created_at FROM messages WHERE read_out_at IS NULL ORDER BY created_at, id LIMIT ?",
                (limit,))]

    def read_out(self, ids):
        with self.db() as db:
            db.executemany("UPDATE messages SET read_out_at=? WHERE id=? AND read_out_at IS NULL",
                           [(self.clock(), i) for i in ids])

    def add_reply(self, message_id, text):
        """@return the reply's id; raises ValueError for an unknown message or bad text."""
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MESSAGE_MAX:
            raise ValueError(f"Reply text must be 1-{MESSAGE_MAX} characters")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
                raise ValueError("Unknown message_id; use the one from the [MESSAGE] note")
            reply_id = str(uuid.uuid4())
            db.execute("INSERT INTO replies (id, reply_to, text, at) VALUES (?,?,?,?)",
                       (reply_id, message_id, text.strip(), self.clock()))
            return reply_id

    # ── Profile ──
    def set_profile(self, summary, for_updated_at):
        with self.db() as db:
            db.execute("INSERT INTO profile (id, summary, for_updated_at, synced) VALUES (1,?,?,0) "
                       "ON CONFLICT (id) DO UPDATE SET summary=excluded.summary, for_updated_at=excluded.for_updated_at, synced=0",
                       (summary, for_updated_at))

    def profile_for(self):
        """updated_at of the notes the stored summary is from, or None."""
        with self.db() as db:
            row = db.execute("SELECT for_updated_at FROM profile WHERE id=1").fetchone()
            return row["for_updated_at"] if row else None

    def resend_profile(self):
        with self.db() as db:
            db.execute("UPDATE profile SET synced=0 WHERE id=1")

    def profile(self):
        if not self.path.exists():   # building Ember's instructions must not create storage
            return ""
        with self.db() as db:
            row = db.execute("SELECT summary FROM profile WHERE id=1").fetchone()
            return (row["summary"] or "") if row else ""

    # ── Sync ──
    def pending_sync(self, limit=15):
        with self.db() as db:
            delivered = [{"id": r["id"], "at": r["read_out_at"]} for r in db.execute(
                "SELECT id, read_out_at FROM messages WHERE read_out_at IS NOT NULL AND synced=0 LIMIT ?", (limit,))]
            replies = [dict(r) for r in db.execute(
                "SELECT id, reply_to, text, at FROM replies WHERE synced=0 ORDER BY at LIMIT ?", (limit,))]
            row = db.execute("SELECT summary, for_updated_at FROM profile WHERE id=1 AND synced=0").fetchone()
            profile = {"summary": row["summary"] or "", "for_updated_at": row["for_updated_at"]} if row else None
            return {"delivered": delivered, "replies": replies, "profile": profile}

    def synced(self, delivered=(), replies=(), profile_for=None):
        with self.db() as db:
            db.executemany("UPDATE messages SET synced=1 WHERE id=?", [(i,) for i in delivered])
            db.executemany("UPDATE replies SET synced=1 WHERE id=?", [(i,) for i in replies])
            if profile_for is not None:
                db.execute("UPDATE profile SET synced=1 WHERE for_updated_at=?", (profile_for,))
            cutoff = self.clock() - KEEP_READ
            db.execute("DELETE FROM replies WHERE synced=1 AND at<?", (cutoff,))
            db.execute("DELETE FROM messages WHERE synced=1 AND read_out_at<? "
                       "AND id NOT IN (SELECT reply_to FROM replies)", (cutoff,))
