"""Where the menu and activity calendar come from, and the last good copy of each.

Two kinds: "activities" (an ICS calendar or a CSV of date,time,title,location) and "menu"
(a CSV of date,meal,items). For each kind, a staff upload on the robot's dashboard wins over
the env setting (EMBER_FACILITY_CALENDAR_URL / EMBER_FACILITY_MENU_CSV, a URL or a file path),
because it is the most recent thing staff did. The last copy that parsed is kept on disk;
when a refresh fails Ember keeps answering from it and says how old it is.
"""
import csv
import io
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from . import ics

KINDS = ("activities", "menu")
ENV = {"activities": "EMBER_FACILITY_CALENDAR_URL", "menu": "EMBER_FACILITY_MENU_CSV"}
MAX_BYTES = 2_000_000
MEALS = {"breakfast": "breakfast", "brunch": "brunch", "lunch": "lunch", "dinner": "dinner",
         "supper": "dinner", "snack": "snack", "snacks": "snack", "dessert": "dessert", "all day": "all day"}
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
HEADERS = {
    "date": ("date", "day", "weekday"),
    "time": ("time", "start", "start time", "starts", "begins"),
    "end": ("end", "end time", "ends"),
    "title": ("title", "activity", "event", "name", "what"),
    "location": ("location", "place", "room", "where"),
    "meal": ("meal", "course", "service"),
    "items": ("items", "menu", "dishes", "dish", "food", "description"),
}


def data_dir():
    root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[3]))
    return root / "facility"


def refresh_seconds():
    try:
        return max(5, int(os.getenv("EMBER_FACILITY_REFRESH_MINUTES", "60"))) * 60
    except ValueError:
        return 3600


# ── CSV ───────────────────────────────────────────────────────────────────────
def parse_day(value):
    """A date (2026-11-13, 11/13/2026, 11/13/26) or a weekday name, which repeats every week."""
    value = value.strip()
    lowered = value.lower().rstrip(".")
    for index, name in enumerate(DAY_NAMES):
        if lowered in (name, name[:3]):
            return index
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%B %d %Y", "%B %d, %Y", "%a %m/%d/%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date {value!r}")


def parse_time(value):
    """'14:00', '2:00 PM', '2pm', '10 a.m.' → 'HH:MM'; blank → None (all day)."""
    value = value.strip().lower().replace(".", "")
    if not value or value in ("all day", "allday", "tba", "-"):
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", value)
    if not match:
        raise ValueError(f"Unrecognised time {value!r}")
    hour, minute, half = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(f"Unrecognised time {value!r}")
        hour = hour % 12 + (12 if half.startswith("p") else 0)
    elif match.group(2) is None:
        raise ValueError(f"Ambiguous time {value!r}; use 14:00 or 2 PM")
    if hour > 23 or minute > 59:
        raise ValueError(f"Unrecognised time {value!r}")
    return f"{hour:02d}:{minute:02d}"


def rows(text):
    """Dict rows keyed by our column names; header names are matched loosely."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(reader, None)
    if not header:
        raise ValueError("The CSV is empty")
    columns = {}
    for index, name in enumerate(header):
        key = re.sub(r"\s+", " ", name.strip().lower())
        for field, aliases in HEADERS.items():
            if key in aliases and field not in columns:
                columns[field] = index
    for number, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        yield number, {field: (row[i].strip() if i < len(row) else "") for field, i in columns.items()}, columns


class Table:
    """Parsed CSV entries; each has a fixed date or a weekday that repeats."""

    def __init__(self, kind, entries, skipped):
        self.kind, self.entries, self.skipped = kind, entries, skipped

    def on(self, day, local=None):
        matches = [e for e in self.entries if e["day"] == day or e["day"] == day.weekday()]
        # A dated row replaces the weekly row for the same meal/title on that date.
        dated = {(e.get("meal") or e.get("title")) for e in matches if isinstance(e["day"], date)}
        result = [e for e in matches if isinstance(e["day"], date) or (e.get("meal") or e.get("title")) not in dated]
        if self.kind == "menu":
            order = list(MEALS.values())
            return [{"meal": e["meal"], "items": e["items"]} for e in
                    sorted(result, key=lambda e: order.index(e["meal"]) if e["meal"] in order else 99)]
        return sorted(({"title": e["title"], "location": e["location"] or None, "all_day": e["time"] is None,
                        "time": e["time"], "starts_at": None if e["time"] is None or local is None else
                        datetime.combine(day, datetime.strptime(e["time"], "%H:%M").time(), local).isoformat(timespec="minutes"),
                        "ends_at": None} for e in result), key=lambda i: (not i["all_day"], i["time"] or "", i["title"]))


def parse_menu(text):
    entries, skipped = [], 0
    for _, row, columns in rows(text):
        if not {"date", "items"} <= set(columns):
            raise ValueError("A menu CSV needs columns date, meal, items")
        try:
            day = parse_day(row["date"])
            meal = row.get("meal", "").lower().strip()
            items = re.sub(r"\s+", " ", row["items"])[:300]
            if not items:
                raise ValueError
            entries.append({"day": day, "meal": MEALS.get(meal, meal[:30] or "menu"), "items": items})
        except ValueError:
            skipped += 1
    if not entries:
        raise ValueError("No menu rows could be read (columns: date, meal, items)")
    return Table("menu", entries, skipped)


def parse_activities_csv(text):
    entries, skipped = [], 0
    for _, row, columns in rows(text):
        if not {"date", "title"} <= set(columns):
            raise ValueError("An activities CSV needs columns date, time, title, location")
        try:
            title = row["title"][:200]
            if not title:
                raise ValueError
            entries.append({"day": parse_day(row["date"]), "time": parse_time(row.get("time", "")),
                            "title": title, "location": row.get("location", "")[:200]})
        except ValueError:
            skipped += 1
    if not entries:
        raise ValueError("No activity rows could be read (columns: date, time, title, location)")
    return Table("activities", entries, skipped)


def parse(kind, text, local):
    """A parsed calendar or table, or ValueError. Activities may be ICS or CSV (sniffed)."""
    if kind == "menu":
        return parse_menu(text)
    if text.lstrip("﻿ \r\n\t").upper().startswith("BEGIN:VCALENDAR"):
        calendar = ics.parse(text.lstrip("﻿ \r\n\t"), local)
        if not calendar.events:
            raise ValueError("The calendar has no events")
        return calendar
    return parse_activities_csv(text)


# ── Fetching and the last good copy ──────────────────────────────────────────
def read_source(location):
    """Text of a URL (http, https, webcal) or a local file path. Raises OSError/ValueError."""
    if re.match(r"(?i)^(https?|webcal)://", location):
        url = re.sub(r"(?i)^webcal://", "https://", location)
        request = urllib.request.Request(url, headers={"User-Agent": "Ember-facility-calendar/1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(MAX_BYTES + 1)
    else:
        with open(Path(location).expanduser(), "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("The file is larger than 2 MB")
    return raw.decode("utf-8-sig", errors="replace")


def describe(location):
    """Where a source is, without query strings (calendar URLs often carry a private token)."""
    if re.match(r"(?i)^(https?|webcal)://", location):
        return re.sub(r"[?#].*$", "", location)
    return Path(location).name


class Sources:
    def __init__(self, folder=None, clock=time.time):
        self.folder = Path(folder) if folder else None
        self.clock = clock

    @property
    def root(self):
        return self.folder or data_dir()

    def upload_path(self, kind):
        return self.root / "uploads" / f"{kind}.txt"

    def cache_paths(self, kind):
        return self.root / "cache" / f"{kind}.txt", self.root / "cache" / f"{kind}.json"

    def configured(self, kind):
        """('upload', path) or ('env', location) or None."""
        if self.upload_path(kind).exists():
            return "upload", str(self.upload_path(kind))
        location = os.getenv(ENV[kind], "").strip()
        return ("env", location) if location else None

    def meta(self, kind):
        try:
            return json.loads(self.cache_paths(kind)[1].read_text())
        except (OSError, ValueError):
            return {}

    def save_upload(self, kind, text, local):
        """Validate then store a staff upload. Returns a short summary; raises ValueError."""
        if kind not in KINDS:
            raise ValueError("kind must be activities or menu")
        if len(text.encode()) > MAX_BYTES:
            raise ValueError("The file is larger than 2 MB")
        parsed = parse(kind, text, local)
        path = self.upload_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.store(kind, text, "upload", "staff upload")
        return summary(parsed)

    def remove_upload(self, kind):
        try:
            self.upload_path(kind).unlink()
            return True
        except FileNotFoundError:
            return False

    def store(self, kind, text, origin, where):
        text_path, meta_path = self.cache_paths(kind)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        now = self.clock()
        meta_path.write_text(json.dumps({"updated_at": now, "checked_at": now, "origin": origin, "source": where,
                                         "error": None}))

    def refresh(self, kind, local, force=False):
        """Fetch and validate the configured source if due. Keeps the last good copy on failure."""
        source = self.configured(kind)
        if source is None:
            return
        meta = self.meta(kind)
        origin, location = source
        where = describe(location) if origin == "env" else "staff upload"
        # Uploads are validated and cached when staff upload them; only env sources go stale.
        due = force or meta.get("origin") != origin or meta.get("source") != where or \
            (origin == "env" and self.clock() - meta.get("checked_at", 0) >= refresh_seconds())
        if not due:
            return
        try:
            text = read_source(location)
            parse(kind, text, local)
            self.store(kind, text, origin, where)
        except Exception as error:
            meta.update(checked_at=self.clock(), error=f"{type(error).__name__}: could not refresh",
                        source=meta.get("source") or describe(location), origin=meta.get("origin") or origin)
            path = self.cache_paths(kind)[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(meta))
            print(f"[FACILITY] Could not refresh the {kind}: {type(error).__name__}", flush=True)

    def load(self, kind, local):
        """(parsed or None, meta). Only the last good copy is ever parsed for answers."""
        self.refresh(kind, local)
        if self.configured(kind) is None:
            return None, {}
        try:
            text = self.cache_paths(kind)[0].read_text(encoding="utf-8")
            return parse(kind, text, local), self.meta(kind)
        except (OSError, ValueError):
            return None, self.meta(kind)

    def status(self, local):
        result = {}
        for kind in KINDS:
            parsed, meta = self.load(kind, local)
            source = self.configured(kind)
            result[kind] = {"configured": source[0] if source else None, "source": meta.get("source"),
                            "updated_at": iso(meta.get("updated_at")), "checked_at": iso(meta.get("checked_at")),
                            "error": meta.get("error"), **(summary(parsed) if parsed else {})}
        return result


def summary(parsed):
    if isinstance(parsed, ics.Calendar):
        return {"events": len(parsed.events), "unsupported_repeats": parsed.unsupported, "skipped": parsed.skipped}
    return {"rows": len(parsed.entries), "skipped": parsed.skipped}


def iso(value):
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds") if value else None
