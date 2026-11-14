"""Documentation drafts staff review and sign, built from the care log (feature 4).

Ember drafts; staff decide. Every draft is deterministic templating over staff-visible care
events (no model call), keeps the IDs of the events it came from, and moves
draft -> edited -> signed. A signed draft is final: the database refuses to change it, and
editing it starts a new version. Drafts carry summaries only, never the resident's own
words, because they leave the robot (facility page, the facility's EHR).

Drafts live in a `care_drafts` table in the care log's own SQLite file; store.py is unchanged.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta

from . import store

KINDS = {
    "daily_note": "Daily note (non-clinical)",
    "activity_log": "Activity log",
    "service_log": "Service log",
    "fall_timeline": "Fall incident timeline",
}
STATES = ("draft", "edited", "signed")
FALL_WINDOW = 72 * 3600
MAX_BODY = 20000
NOTICE = ("Drafted by Ember from its care log for staff review. Not a clinical record: "
          "nothing here is final until a staff member edits as needed and signs.")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS care_drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, "
    "version INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, period_start REAL NOT NULL, "
    "period_end REAL NOT NULL, body TEXT NOT NULL, source_ids TEXT NOT NULL, state TEXT NOT NULL, "
    "generated_at REAL NOT NULL, edited_by TEXT, edited_at REAL, signed_by TEXT, signed_account TEXT, "
    "signed_at REAL, supersedes INTEGER, UNIQUE (key, version))",
    # Signed means final, whoever writes to the file.
    "CREATE TRIGGER IF NOT EXISTS care_drafts_signed_final BEFORE UPDATE ON care_drafts "
    "WHEN OLD.state = 'signed' BEGIN SELECT RAISE(ABORT, 'signed drafts are final'); END",
    "CREATE TRIGGER IF NOT EXISTS care_drafts_signed_kept BEFORE DELETE ON care_drafts "
    "WHEN OLD.state = 'signed' BEGIN SELECT RAISE(ABORT, 'signed drafts are final'); END",
)


class DraftError(ValueError):
    pass


def enabled():
    return store.enabled() and os.getenv("EMBER_CARE_DRAFTS", "1") != "0"


def _when(event_or_iso, fmt="%H:%M"):
    value = event_or_iso["at"] if isinstance(event_or_iso, dict) else event_or_iso
    return datetime.fromisoformat(value).strftime(fmt)


def _ref(event):
    return f"[#{event['id']}]"


def _handled(event):
    if event.get("handled_at"):
        return f"handled by {event['handled_by']} at {_when(event['handled_at'], '%a %d %b %H:%M')}"
    return "still open" if event["status"] in store.EXCEPTIONS else ""


def _line(event, extra=""):
    category = f" ({event['category']})" if event.get("category") else ""
    tail = f"; {extra}" if extra else ""
    return f"- {_when(event)} {event['summary']}{category}{tail} {_ref(event)}"


def _header(title, resident, period):
    return [title, f"Resident/unit: {resident}", period, NOTICE, ""]


def _plural(count, word, many=None):
    return f"{count} {word if count == 1 else many or word + 's'}"


# ── Templates (pure functions of events, so tests can pin them down) ─────────
def daily_note(events, day, resident):
    """The day in sections. Requests show how they were handled."""
    by_kind = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)
    lines = _header("Daily note (non-clinical)", resident, f"Date: {day:%A %d %B %Y}")
    if not events:
        return "\n".join(lines + ["No events were logged for this day."])
    requests = by_kind.get("request", [])
    handled = sum(1 for e in requests if e.get("handled_at"))
    counts = [_plural(len(by_kind.get("check_in", [])), "check-in"),
              f"{_plural(len(requests), 'request')} ({handled} handled)",
              _plural(len(by_kind.get("activity", [])), "activity", "activities")]
    lines += ["Overview: " + ", ".join(counts) + ".", ""]
    sections = (
        ("Safety", ("fall", "help_request")), ("Check-ins", ("check_in",)), ("Requests", ("request",)),
        ("Activities", ("activity",)), ("Meals", ("meal",)), ("Night activity", ("night_wake",)),
        ("Notes", ("note",)), ("For staff review (Ember suggests; staff decide)", ("flag",)))
    for heading, kinds in sections:
        items = [e for kind in kinds for e in by_kind.get(kind, [])]
        if items:
            items.sort(key=lambda e: (e["at"], e["id"]))
            lines.append(heading)
            for e in items:
                status = _handled(e) if e["kind"] in ("request", "help_request", "fall") \
                    else ("needs a look" if e["status"] in store.EXCEPTIONS else "")
                lines.append(_line(e, status))
            lines.append("")
    return "\n".join(lines).rstrip()


def activity_log(events, day, resident):
    lines = _header("Activity log", resident, f"Date: {day:%A %d %B %Y}")
    items = [e for e in events if e["kind"] == "activity"]
    if not items:
        return "\n".join(lines + ["No activities were logged for this day."])
    return "\n".join(lines + [f"{_plural(len(items), 'entry', 'entries')}:"]
                     + [_line(e) for e in items])


def service_log(events, day, resident):
    lines = _header("Service log", resident, f"Date: {day:%A %d %B %Y}")
    items = [e for e in events if e["kind"] in ("request", "help_request")]
    if not items:
        return "\n".join(lines + ["No requests were logged for this day."])
    lines.append("Requested | Request | Handled")
    for e in items:
        kind = "help request" if e["kind"] == "help_request" else (e.get("category") or "request")
        if e.get("handled_at"):
            minutes = round((datetime.fromisoformat(e["handled_at"]) - datetime.fromisoformat(e["at"])).total_seconds() / 60)
            outcome = f"{_handled(e)} ({minutes} min)"
        else:
            outcome = _handled(e) or e["status"]
        lines.append(f"{_when(e)} | {e['summary']} ({kind}) | {outcome} {_ref(e)}")
    done = sum(1 for e in items if e.get("handled_at"))
    lines += ["", f"{done} of {len(items)} handled."]
    return "\n".join(lines)


def fall_timeline(fall, events, resident, now):
    """What happened from the fall to 72 hours after it, in order."""
    start = datetime.fromisoformat(fall["at"])
    end = start + timedelta(seconds=FALL_WINDOW)
    lines = _header("Fall incident timeline", resident,
                    f"Fall logged: {start:%A %d %B %Y %H:%M}. Covers until {end:%a %d %b %H:%M}.")
    data = fall.get("data") or {}
    rows = [(fall["at"], f"Fall logged: {fall['summary']}"
             + (f" (source: {data['source']})" if data.get("source") else "") + f" {_ref(fall)}")]
    if fall.get("handled_at"):
        rows.append((fall["handled_at"], f"Marked handled by staff ({fall['handled_by']}) {_ref(fall)}"))
    for e in events:
        if e["id"] == fall["id"]:
            continue
        linked = (e.get("data") or {}).get("fall_event_id") == fall["id"]
        label = {"check_in": "Check-in", "help_request": "Help request", "fall": "Another fall logged",
                 "night_wake": "Night activity"}.get(e["kind"], "Ember" if linked else None)
        if label is None:
            continue
        extra = f"; {_handled(e)}" if e["kind"] in ("help_request", "fall") and _handled(e) else ""
        rows.append((e["at"], f"{label}: {e['summary']}{extra} {_ref(e)}"))
    rows.sort(key=lambda row: datetime.fromisoformat(row[0]))
    lines += [f"- {_when(at, '%a %d %b %H:%M')} {text}" for at, text in rows]
    if not fall.get("handled_at"):
        lines += ["", "Not yet marked handled by staff."]
    if datetime.fromtimestamp(now).astimezone() < end:
        lines += ["", "The 72-hour follow-up window is still open; Ember updates this draft until it is edited or signed."]
    return "\n".join(lines)


# ── Storage ──────────────────────────────────────────────────────────────────
class Drafts:
    def __init__(self, log=None):
        self._log = log

    @property
    def log(self):
        return self._log or store.log  # Follows store.log, so tests can patch it.

    def db(self):
        return _Connection(self.log)

    def resident(self):
        return os.getenv("EMBER_CARE_RESIDENT_NAME", "").strip() or "the resident"

    # Generation
    def generate_day(self, day, kinds=("daily_note", "activity_log", "service_log")):
        start, end = store.day_bounds(day, self.log.clock)
        events = self.log.events(start, end, audience="staff", limit=5000)
        templates = {"daily_note": daily_note, "activity_log": activity_log, "service_log": service_log}
        result = []
        for kind in kinds:
            sources = events if kind == "daily_note" else [
                e for e in events if e["kind"] in (("activity",) if kind == "activity_log" else ("request", "help_request"))]
            result.append(self._upsert(f"{kind}:{day.isoformat()}", kind, f"{KINDS[kind]}, {day:%d %B %Y}",
                                       start, end, templates[kind](sources, day, self.resident()),
                                       [e["id"] for e in sources]))
        return result

    def generate_fall(self, fall_id):
        [fall] = [e for e in self.log.events(kinds=["fall"], audience="staff", limit=5000) if e["id"] == fall_id] or [None]
        if fall is None:
            raise DraftError("no fall event with that ID")
        start = datetime.fromisoformat(fall["at"]).timestamp()
        events = self.log.events(start, start + FALL_WINDOW, audience="staff", limit=5000)
        body = fall_timeline(fall, events, self.resident(), self.log.clock())
        linked = [e["id"] for e in events if e["id"] == fall_id or e["kind"] in ("check_in", "help_request", "fall", "night_wake")
                  or (e.get("data") or {}).get("fall_event_id") == fall_id]
        return self._upsert(f"fall_timeline:{fall_id}", "fall_timeline",
                            f"{KINDS['fall_timeline']}, {datetime.fromisoformat(fall['at']):%d %B %Y %H:%M}",
                            start, start + FALL_WINDOW, body, linked)

    def refresh_falls(self, days=4):
        """Create or update timelines for recent falls (until edited or signed)."""
        since = self.log.clock() - days * 86400
        return [self.generate_fall(e["id"]) for e in self.log.events(since, kinds=["fall"], audience="staff")]

    def _upsert(self, key, kind, title, start, end, body, source_ids):
        """New draft, or refresh an untouched one. Staff edits and signatures are never overwritten."""
        now = self.log.clock()
        with self.db() as db:
            latest = db.execute("SELECT * FROM care_drafts WHERE key=? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
            if latest is None:
                draft_id = db.execute(
                    "INSERT INTO care_drafts (key, version, kind, title, period_start, period_end, body, source_ids, "
                    "state, generated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (key, 1, kind, title, start, end, body, json.dumps(source_ids), "draft", now)).lastrowid
            elif latest["state"] == "draft" and (latest["body"], json.loads(latest["source_ids"])) != (body, source_ids):
                draft_id = latest["id"]
                db.execute("UPDATE care_drafts SET body=?, source_ids=?, generated_at=? WHERE id=?",
                           (body, json.dumps(source_ids), now, draft_id))
            else:
                return self._dict(latest)
            row = db.execute("SELECT * FROM care_drafts WHERE id=?", (draft_id,)).fetchone()
        return self._sent(self._dict(row))

    # Staff actions
    def edit(self, draft_id, body, by):
        """Save staff wording. Editing a signed draft starts a new version instead."""
        body = store.text(body, MAX_BODY, "body")
        by = store.text(by, 120, "by")
        now = self.log.clock()
        with self.db() as db:
            row = self._row(db, draft_id)
            if row["state"] == "signed":
                newer = db.execute("SELECT MAX(version) FROM care_drafts WHERE key=?", (row["key"],)).fetchone()[0]
                if newer != row["version"]:
                    raise DraftError("a newer version of this draft exists; edit that one")
                draft_id = db.execute(
                    "INSERT INTO care_drafts (key, version, kind, title, period_start, period_end, body, source_ids, "
                    "state, generated_at, edited_by, edited_at, supersedes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["key"], row["version"] + 1, row["kind"], row["title"], row["period_start"], row["period_end"],
                     body, row["source_ids"], "edited", row["generated_at"], by, now, row["id"])).lastrowid
            else:
                db.execute("UPDATE care_drafts SET body=?, state='edited', edited_by=?, edited_at=? WHERE id=?",
                           (body, by, now, draft_id))
            row = self._row(db, draft_id)
        return self._sent(self._dict(row))

    def sign(self, draft_id, name, account=None):
        name = store.text(name, 120, "name")
        with self.db() as db:
            row = self._row(db, draft_id)
            if row["state"] == "signed":
                raise DraftError("this draft is already signed")
            db.execute("UPDATE care_drafts SET state='signed', signed_by=?, signed_account=?, signed_at=? WHERE id=?",
                       (name, account, self.log.clock(), draft_id))
            row = self._row(db, draft_id)
        return self._sent(self._dict(row))

    # Reading
    def get(self, draft_id):
        with self.db() as db:
            return self._dict(self._row(db, draft_id))

    def list(self, kind=None, state=None, all_versions=False, limit=200):
        with self.db() as db:
            rows = db.execute("SELECT * FROM care_drafts ORDER BY period_start DESC, key, version DESC").fetchall()
        result, seen = [], set()
        for row in rows:
            if not all_versions and row["key"] in seen:
                continue
            seen.add(row["key"])
            if (kind and row["kind"] != kind) or (state and row["state"] != state):
                continue
            result.append(self._dict(row))
        return result[:limit]

    def _row(self, db, draft_id):
        row = db.execute("SELECT * FROM care_drafts WHERE id=?", (int(draft_id),)).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return row

    def _dict(self, r):
        draft = {"id": r["id"], "key": r["key"], "version": r["version"], "kind": r["kind"], "title": r["title"],
                 "state": r["state"], "period_start": store.iso(r["period_start"]),
                 "period_end": store.iso(r["period_end"]), "body": r["body"],
                 "source_event_ids": json.loads(r["source_ids"]), "generated_at": store.iso(r["generated_at"]),
                 "body_sha256": hashlib.sha256(r["body"].encode("utf-8")).hexdigest(), "supersedes": r["supersedes"]}
        for field in ("edited", "signed"):
            if r[f"{field}_at"]:
                draft[f"{field}_by"], draft[f"{field}_at"] = r[f"{field}_by"], store.iso(r[f"{field}_at"])
        return draft

    def _sent(self, draft):
        store.forward("/api/device/care-draft", draft)
        return draft


class _Connection:
    """The care log's own connection, with the drafts table added."""
    def __init__(self, log):
        self.context = log.db()

    def __enter__(self):
        db = self.context.__enter__()
        for statement in _SCHEMA:
            db.execute(statement)
        return db

    def __exit__(self, *exc):
        return self.context.__exit__(*exc)


def export_text(draft, device):
    """Plain text for attaching to the facility's EHR."""
    unit = device.get("name") or device.get("serial") or "Ember"
    lines = [draft["body"], "", "-" * 60,
             f"Document: {draft['title']} (version {draft['version']}, Ember unit {unit}, draft #{draft['id']})",
             f"Source care-log events: {', '.join('#' + str(i) for i in draft['source_event_ids']) or 'none'}"]
    if draft["state"] == "signed":
        lines.append(f"Signed by {draft['signed_by']} on {_when(draft['signed_at'], '%d %B %Y %H:%M %z')}")
        lines.append(f"Text fingerprint (SHA-256): {draft['body_sha256']}")
    else:
        lines.append("UNSIGNED DRAFT: not for filing until a staff member signs it.")
    return "\n".join(lines) + "\n"


# ── Schedule ─────────────────────────────────────────────────────────────────
def due(now, done_on, at):
    hour, minute = (int(part) for part in at.split(":"))
    return done_on != now.date().isoformat() and (now.hour, now.minute) >= (hour, minute)


def run_schedule(drafts, now=None):
    """Morning: yesterday's daily note, activity and service logs. Always: recent fall timelines."""
    if not enabled():
        return False
    now = now or datetime.fromtimestamp(drafts.log.clock()).astimezone()
    made = False
    at = os.getenv("EMBER_CARE_DRAFT_TIME", "07:00").strip() or "07:00"
    if due(now, drafts.log.setting("drafts_generated_on"), at):
        drafts.log.set_setting("drafts_generated_on", now.date().isoformat())
        drafts.generate_day(now.date() - timedelta(days=1))
        made = True
    drafts.refresh_falls()
    return made


drafts = Drafts()
