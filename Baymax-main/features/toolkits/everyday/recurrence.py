"""Repeat rules for reminders, modelled on Apple Reminders' custom repeat.

A rule is a small JSON object; times are local wall-clock times in the robot's
timezone, so "every day at 8:00" stays at 8:00 across daylight-saving changes.

    {"freq": "hourly" | "daily" | "weekly" | "monthly" | "yearly",
     "interval": 1-999,                 # every N hours/days/weeks/months/years
     "weekdays": ["MO", "WE"],          # weekly: which days (default: the start's)
     "month_days": [1, 15, -1],         # monthly "Each": days of the month, -1 = last
     "nth": {"n": 2, "day": "TU"},      # monthly/yearly "On the": 1-5 or -1 (last),
                                        #   a weekday or "day" / "weekday" / "weekend"
     "months": [1, 7],                  # yearly: which months (default: the start's)
     "until": "2027-06-30"}             # End Repeat: last day, inclusive (optional)

The first occurrence is the reminder's start (local "YYYY-MM-DDTHH:MM").
"""
import calendar
import json
import math
import os
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

FREQS = ("hourly", "daily", "weekly", "monthly", "yearly")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_SETS = {"day": DAYS, "weekday": DAYS[:5], "weekend": DAYS[5:]}
ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", -1: "last"}
MAX_PERIODS = 2000      # periods searched for the next occurrence before giving up


def device_timezone():
    """IANA name of the robot's timezone (TZ, /etc/localtime or /etc/timezone), else UTC."""
    names = [os.getenv("TZ", "").lstrip(":")]
    try:
        target = os.path.realpath("/etc/localtime")
        names.append(target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "")
    except OSError:
        pass
    try:
        with open("/etc/timezone") as f:
            names.append(f.read().strip())
    except OSError:
        pass
    for name in names:
        if name:
            try:
                ZoneInfo(name)
                return name
            except Exception:
                continue
    return "UTC"


def _ints(values, low, high, extra=()):
    if not isinstance(values, list) or not values or len(values) > 31:
        raise ValueError("Use a nonempty list")
    for v in values:
        if type(v) is not int or not (low <= v <= high or v in extra):
            raise ValueError("Value out of range")
    return values


def normalize(rule):
    """@return the validated rule (a new dict), or None for a one-time reminder."""
    if rule is None:
        return None
    if isinstance(rule, str):
        rule = json.loads(rule)
    if not isinstance(rule, dict):
        raise ValueError("A repeat rule is an object")
    if set(rule) - {"freq", "interval", "weekdays", "month_days", "nth", "months", "until"}:
        raise ValueError("Unsupported repeat fields")
    freq = rule.get("freq")
    if freq not in FREQS:
        raise ValueError("freq must be hourly, daily, weekly, monthly or yearly")
    interval = rule.get("interval", 1)
    if type(interval) is not int or not 1 <= interval <= 999:
        raise ValueError("interval must be 1-999")
    out = {"freq": freq, "interval": interval}
    if rule.get("weekdays") is not None:
        days = rule["weekdays"]
        if freq != "weekly" or not isinstance(days, list) or not days or any(d not in DAYS for d in days):
            raise ValueError("weekdays are MO-SU, for weekly repeats")
        out["weekdays"] = sorted(set(days), key=DAYS.index)
    if rule.get("month_days") is not None:
        if freq != "monthly":
            raise ValueError("month_days are for monthly repeats")
        out["month_days"] = sorted(set(_ints(rule["month_days"], 1, 31, (-1,))), key=lambda d: 32 if d == -1 else d)
    if rule.get("nth") is not None:
        nth = rule["nth"]
        if (freq not in ("monthly", "yearly") or "month_days" in out or not isinstance(nth, dict)
                or set(nth) != {"n", "day"} or nth["n"] not in ORDINALS or type(nth["n"]) is not int
                or nth["day"] not in DAYS + tuple(DAY_SETS)):
            raise ValueError("nth is {n: 1-5 or -1, day: MO-SU, day, weekday or weekend}")
        out["nth"] = {"n": nth["n"], "day": nth["day"]}
    if rule.get("months") is not None:
        if freq != "yearly":
            raise ValueError("months are for yearly repeats")
        out["months"] = sorted(set(_ints(rule["months"], 1, 12)))
    if rule.get("until") is not None:
        if not isinstance(rule["until"], str):
            raise ValueError("until is a YYYY-MM-DD date")
        out["until"] = date.fromisoformat(rule["until"]).isoformat()
    return out


def parse_start(start, tz):
    """Local "YYYY-MM-DDTHH:MM" in zone tz -> aware datetime (seconds dropped)."""
    value = datetime.fromisoformat(start)
    if value.utcoffset() is not None:
        value = value.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return value.replace(second=0, microsecond=0, tzinfo=ZoneInfo(tz))


def local_start(timestamp, tz):
    """Unix time -> the local "YYYY-MM-DDTHH:MM" used as a rule's start."""
    return datetime.fromtimestamp(timestamp, ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def _at(day, clock, zone):
    return datetime.combine(day, clock, tzinfo=zone).timestamp()


def _nth_days(year, month, nth):
    wanted = DAY_SETS.get(nth["day"], (nth["day"],))
    matches = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
               if DAYS[date(year, month, d).weekday()] in wanted]
    n = nth["n"]
    if n == -1:
        return matches[-1:]
    return matches[n - 1:n]


def _days_in_month(year, month, rule, anchor_day):
    if "nth" in rule:
        return _nth_days(year, month, rule["nth"])
    last = calendar.monthrange(year, month)[1]
    days = {last if d == -1 else d for d in rule.get("month_days", [anchor_day])}
    return sorted(d for d in days if d <= last)   # a 31st is skipped in shorter months


def _period_index(rule, d0, day):
    """Index of the period containing local date `day` (0 = the start's period)."""
    freq = rule["freq"]
    if freq == "daily":
        return (day - d0).days // rule["interval"]
    if freq == "weekly":
        return ((day - timedelta(days=day.weekday())) - (d0 - timedelta(days=d0.weekday()))).days // 7 // rule["interval"]
    if freq == "monthly":
        return ((day.year - d0.year) * 12 + day.month - d0.month) // rule["interval"]
    return (day.year - d0.year) // rule["interval"]


def _candidates(rule, start, first_period):
    """Occurrence timestamps from period `first_period` on, ascending."""
    zone, clock, d0, step = start.tzinfo, start.time().replace(tzinfo=None), start.date(), rule["interval"]
    freq = rule["freq"]
    for k in range(max(0, first_period), max(0, first_period) + MAX_PERIODS):
        if freq == "daily":
            yield _at(d0 + timedelta(days=k * step), clock, zone)
        elif freq == "weekly":
            week = d0 - timedelta(days=d0.weekday()) + timedelta(weeks=k * step)
            for code in rule.get("weekdays", [DAYS[d0.weekday()]]):
                yield _at(week + timedelta(days=DAYS.index(code)), clock, zone)
        elif freq == "monthly":
            y, m = divmod(d0.month - 1 + k * step, 12)
            for d in _days_in_month(d0.year + y, m + 1, rule, d0.day):
                yield _at(date(d0.year + y, m + 1, d), clock, zone)
        else:  # yearly
            year = d0.year + k * step
            for month in rule.get("months", [d0.month]):
                if "nth" in rule:
                    days = _nth_days(year, month, rule["nth"])
                else:
                    days = [d0.day] if d0.day <= calendar.monthrange(year, month)[1] else []
                for d in days:
                    yield _at(date(year, month, d), clock, zone)


def next_after(rule, start, tz, after):
    """Next occurrence strictly after Unix time `after`, or None once the repeat ended.

    A one-time reminder (rule None) occurs only at its start.
    """
    begin = parse_start(start, tz)
    first = begin.timestamp()
    if rule is None:
        return first if first > after else None
    until = None
    if rule.get("until"):
        until = _at(date.fromisoformat(rule["until"]), dtime(23, 59, 59), begin.tzinfo)
    if rule["freq"] == "hourly":
        period = rule["interval"] * 3600
        k = 0 if after < first else math.floor((after - first) / period) + 1
        ts = first + k * period
        return ts if until is None or ts <= until else None
    after_day = datetime.fromtimestamp(max(after, first), begin.tzinfo).date()
    for ts in _candidates(rule, begin, _period_index(rule, begin.date(), after_day) - 1):
        if ts < first:
            continue
        if until is not None and ts > until:
            return None
        if ts > after:
            return ts
    return None


def _clock(start):
    return start.strftime("%I:%M %p").lstrip("0")


def describe(rule, start, tz):
    """Short English summary, e.g. 'every 2 weeks on Monday and Wednesday at 9:00 AM'."""
    begin = parse_start(start, tz)
    if rule is None:
        return begin.strftime("once, on %a %b %d %Y at ") + _clock(begin)
    n, freq = rule["interval"], rule["freq"]
    unit = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[freq]
    text = f"every {unit}" if n == 1 else f"every {n} {unit}s"
    if freq == "hourly":
        text += " from " + _clock(begin)
    else:
        if freq == "weekly":
            days = [DAY_NAMES[DAYS.index(d)] for d in rule.get("weekdays", [DAYS[begin.weekday()]])]
            text += " on " + (" and ".join(days) if len(days) < 3 else ", ".join(days[:-1]) + " and " + days[-1])
        elif "nth" in rule:
            day = rule["nth"]["day"]
            name = {"day": "day", "weekday": "weekday", "weekend": "weekend day"}.get(day) or DAY_NAMES[DAYS.index(day)]
            text += f" on the {ORDINALS[rule['nth']['n']]} {name}"
        elif freq == "monthly":
            days = ["last day" if d == -1 else _ordinal(d) for d in rule.get("month_days", [begin.day])]
            text += " on the " + " and ".join(days)
        if freq == "yearly":
            months = [calendar.month_name[m] for m in rule.get("months", [begin.month])]
            text += (" of " if "nth" in rule else " in ") + " and ".join(months)
            if "nth" not in rule:
                text += f" on the {_ordinal(begin.day)}"
        text += " at " + _clock(begin)
    if rule.get("until"):
        text += ", until " + date.fromisoformat(rule["until"]).strftime("%b %d %Y")
    return text


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
