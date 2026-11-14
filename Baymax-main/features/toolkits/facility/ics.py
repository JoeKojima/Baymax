"""A small, careful iCalendar (RFC 5545) reader for facility activity calendars.

Supported: line folding, escaped text, all-day events (VALUE=DATE), UTC times ('Z'), TZID
times (IANA names via zoneinfo; unknown names fall back to the device's local time),
floating times, DTEND/DURATION, STATUS:CANCELLED, EXDATE, RECURRENCE-ID overrides, and
RRULE with FREQ=DAILY or FREQ=WEEKLY (INTERVAL, COUNT, UNTIL, plain BYDAY such as MO,WE).
Not supported (the event shows only on its first date, and is counted in `unsupported`):
MONTHLY/YEARLY rules, ordinal BYDAY (1TU, -1FR), BYMONTHDAY, BYSETPOS, RDATE.
"""
import re
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MAX_OCCURRENCES = 5000  # Daily for ~13 years; stops runaway rules.


def unfold(text):
    """Lines with RFC 5545 folding undone (CRLF followed by a space or tab continues a line)."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def unescape(value):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def split_property(line):
    """('DTSTART', {'TZID': 'America/New_York'}, '20261113T140000')."""
    head, sep, value = line.partition(":")
    # A ':' inside a quoted parameter value (TZID="a:b") is part of the head.
    while head.count('"') % 2 and sep:
        more, sep, value = value.partition(":")
        head += ":" + more
    name, *params = head.split(";")
    parsed = {}
    for param in params:
        key, _, val = param.partition("=")
        parsed[key.strip().upper()] = val.strip().strip('"')
    return name.strip().upper(), parsed, value


def zone(tzid, local):
    if tzid and ZoneInfo is not None:
        try:
            return ZoneInfo(tzid)
        except Exception:
            pass
    return local


def parse_when(value, params, local):
    """(datetime aware, all_day). All-day values are midnight in the device's timezone."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time(), local), True
    match = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not match:
        raise ValueError(f"Unrecognised date-time {value!r}")
    naive = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    if match.group(3):
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=zone(params.get("TZID"), local)), False


def parse_duration(value):
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not match:
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    result = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                       minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -result if sign == "-" else result


def parse_rule(value):
    rule = {}
    for part in value.split(";"):
        key, _, val = part.partition("=")
        if key:
            rule[key.strip().upper()] = val.strip().upper()
    return rule


def supported(rule):
    if rule.get("FREQ") not in ("DAILY", "WEEKLY"):
        return False
    if set(rule) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}:
        return False
    return all(day in WEEKDAYS for day in rule.get("BYDAY", "MO").split(","))


class Event:
    __slots__ = ("uid", "start", "end", "all_day", "title", "location", "rule", "exdates", "recurrence_id",
                 "cancelled")

    def __init__(self):
        self.uid = self.end = self.rule = self.recurrence_id = None
        self.cancelled = False
        self.start, self.all_day, self.title, self.location, self.exdates = None, False, "", "", set()


class Calendar:
    def __init__(self, events, unsupported=0, skipped=0):
        self.events, self.unsupported, self.skipped = events, unsupported, skipped

    def on(self, day, local):
        """Occurrences that start on (or, for all-day events, cover) `day` in `local` time, by start time."""
        overrides = {(e.uid, e.recurrence_id) for e in self.events if e.recurrence_id is not None}
        found = []
        for event in self.events:
            if event.cancelled:
                continue  # Kept only so a cancelled single occurrence hides the original.
            for start in occurrences(event, day, local):
                if event.recurrence_id is None and (event.uid, start) in overrides:
                    continue  # This occurrence was moved or edited; the override event shows it.
                length = (event.end - event.start) if event.end else (timedelta(days=1) if event.all_day else timedelta())
                if event.all_day:
                    first = start.date()
                    last = first + max(timedelta(days=1), length) - timedelta(days=1)
                    if first <= day <= last:
                        found.append(item(event, start, length, local))
                elif start.astimezone(local).date() == day:
                    found.append(item(event, start, length, local))
        return sorted(found, key=lambda i: (not i["all_day"], i["starts_at"] or "", i["title"]))


def item(event, start, length, local):
    if event.all_day:
        return {"title": event.title, "location": event.location or None, "all_day": True,
                "time": None, "starts_at": None, "ends_at": None}
    start_local = start.astimezone(local)
    end_local = (start + length).astimezone(local) if length else None
    return {"title": event.title, "location": event.location or None, "all_day": False,
            "time": start_local.strftime("%H:%M"), "starts_at": start_local.isoformat(timespec="minutes"),
            "ends_at": end_local.isoformat(timespec="minutes") if end_local else None}


def occurrences(event, day, local):
    """Starts of `event` that could fall on `day` (a day either side, for timezone shifts)."""
    window_start = datetime.combine(day - timedelta(days=1), time(), local)
    window_end = datetime.combine(day + timedelta(days=2), time(), local)
    span = (event.end - event.start) if (event.all_day and event.end) else timedelta()
    rule = event.rule if event.rule and supported(event.rule) else None
    if rule is None:
        yield event.start  # Calendar.on() decides whether it falls on the day.
        return
    for start in expand(event, rule):
        if start >= window_end:
            return
        if start + span >= window_start and start not in event.exdates:
            yield start


def expand(event, rule):
    """Occurrence starts in order, computed in the event's own zone (wall-clock, so DST-correct)."""
    tz = event.start.tzinfo
    first = event.start
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until, _ = parse_when(rule["UNTIL"], {}, tz)
            if re.fullmatch(r"\d{8}", rule["UNTIL"]):
                until += timedelta(days=1) - timedelta(seconds=1)  # A date UNTIL includes that whole day.
        except ValueError:
            until = None
    produced = 0
    if rule["FREQ"] == "DAILY":
        steps = (first.replace(tzinfo=None) + timedelta(days=n * interval) for n in range(MAX_OCCURRENCES))
    else:
        days = sorted({WEEKDAYS.index(d) for d in rule.get("BYDAY", WEEKDAYS[first.weekday()]).split(",")})
        week_start = WEEKDAYS.index(rule.get("WKST", "MO")) if rule.get("WKST") in WEEKDAYS else 0
        anchor = first.date() - timedelta(days=(first.weekday() - week_start) % 7)

        def weekly():
            for n in range(MAX_OCCURRENCES // 7 + 1):
                base = anchor + timedelta(weeks=n * interval)
                for offset in range(7):
                    candidate = base + timedelta(days=offset)
                    if candidate.weekday() in days and candidate >= first.date():
                        yield datetime.combine(candidate, first.time())
        steps = weekly()
    for naive in steps:
        start = naive.replace(tzinfo=tz)
        if until is not None and start > until:
            return
        if count is not None and produced >= count:
            return
        produced += 1  # EXDATEs still count towards COUNT (RFC 5545).
        yield start
        if produced >= MAX_OCCURRENCES:
            return


def parse(text, local):
    """A Calendar from ICS text. Raises ValueError when it isn't a calendar at all."""
    lines = unfold(text)
    if not lines or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise ValueError("Not an iCalendar file (it must start with BEGIN:VCALENDAR)")
    events, current, depth, unsupported, skipped = [], None, 0, 0, 0
    for line in lines:
        name, params, value = split_property(line)
        if name == "BEGIN":
            if value.strip().upper() == "VEVENT" and current is None:
                current, depth = Event(), 0
            elif current is not None:
                depth += 1  # VALARM and other nested components are ignored.
            continue
        if name == "END":
            if current is not None and depth:
                depth -= 1
            elif current is not None and value.strip().upper() == "VEVENT":
                if current.start is None or (current.cancelled and current.recurrence_id is None):
                    skipped += 1
                else:
                    if current.rule and not supported(current.rule):
                        unsupported += 1
                    events.append(current)
                current = None
            continue
        if current is None or depth:
            continue
        try:
            if name == "SUMMARY":
                current.title = unescape(value)[:200]
            elif name == "LOCATION":
                current.location = unescape(value)[:200]
            elif name == "UID":
                current.uid = value.strip()
            elif name == "DTSTART":
                current.start, current.all_day = parse_when(value, params, local)
            elif name == "DTEND":
                current.end, _ = parse_when(value, params, local)
            elif name == "DURATION" and current.start is not None and current.end is None:
                length = parse_duration(value)
                current.end = current.start + length if length else None
            elif name == "RRULE":
                current.rule = parse_rule(value)
            elif name == "EXDATE":
                for part in value.split(","):
                    current.exdates.add(parse_when(part, params, local)[0])
            elif name == "RECURRENCE-ID":
                current.recurrence_id = parse_when(value, params, local)[0]
            elif name == "STATUS" and value.strip().upper() == "CANCELLED":
                current.cancelled = True
        except ValueError:
            if name == "DTSTART":
                current.start = None  # Unreadable start: skip the event rather than guess.
    for event in events:
        if not event.title:
            event.title = "Untitled event"
    return Calendar(events, unsupported, skipped)
