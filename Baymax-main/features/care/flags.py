"""Care-level review flags: patterns in the logged, non-clinical events, for staff to review.

Ember suggests; staff decide. Each flag is a `flag` care event with status `attention`, a
plain factual summary ending "for staff review of care level", and the counts and event IDs
behind it. Nothing here judges health: it counts what was already logged. See FLAGS.md.
"""
import json
import math
import os
import time
from datetime import datetime

from . import night, store

DAY = 86400
SUFFIX = " — for staff review of care level"
STATE_KEY = "flag_state"
LAST_RUN_KEY = "flags_last_run"
EVIDENCE_CAP = 20
# Words a flag must never use: Ember counts events, it does not assess the resident.
BANNED = ("diagnos", "dementia", "decline", "medication", "medical", "clinical", "cognitive", "symptom",
          "illness", "disease", "sick", "health", "needs more care", "deteriorat", "confus", "frail")

DEFAULTS = {
    "EVERY_DAYS": 7, "WINDOW_DAYS": 14, "BASELINE_DAYS": 28, "MIN_BASELINE_DAYS": 14, "RATIO": 1.5,
    "HELP_MIN": 4, "REQUEST_MIN": 10, "NIGHTS": 5, "FALLS": 2, "FALL_DAYS": 30, "CHECKIN_MIN": 3,
    "COOLDOWN_DAYS": 14,
}


def setting(name):
    default = DEFAULTS[name]
    try:
        value = float(os.getenv(f"EMBER_CARE_FLAGS_{name}", default))
        return value if value >= 0 else default
    except ValueError:
        return default


def enabled():
    return store.enabled() and os.getenv("EMBER_CARE_FLAGS", "1") != "0"


def plural(n, word, many=None):
    return f"{n} {word if n == 1 else (many or word + 's')}"


# ── Rules ────────────────────────────────────────────────────────────────────
# Each rule: which events count, how to count them, and when to raise a flag.
def count_events(events):
    return len(events)


def count_nights(events):
    return len({(e.get("data") or {}).get("night") or night.night_of(datetime.fromisoformat(e["at"]).timestamp())
                for e in events})


RULES = {
    "help_requests": {
        "kinds": ("help_request",), "statuses": None, "count": count_events, "compare": "ratio", "min": "HELP_MIN",
        "say": lambda n, days, usual: f"{plural(n, 'request')} for a person to come in the last {days} days{usual}"},
    "requests": {
        "kinds": ("request",), "statuses": None, "count": count_events, "compare": "ratio", "min": "REQUEST_MIN",
        "say": lambda n, days, usual: f"{plural(n, 'request')} for staff in the last {days} days{usual}"},
    "night_activity": {
        "kinds": ("night_wake",), "statuses": None, "count": count_nights, "compare": "more", "min": "NIGHTS",
        "say": lambda n, days, usual: f"Awake at night on {n} of the last {days} nights{usual}"},
    "check_ins": {
        # A handled check-in was an attention/no-answer one that staff closed; it still counts.
        "kinds": ("check_in",), "statuses": ("attention", "no_answer", "open", "handled"), "count": count_events,
        "compare": "absolute", "min": "CHECKIN_MIN",
        "say": lambda n, days, usual: f"{plural(n, 'check-in')} needed a look or got no answer in the last {days} days{usual}"},
    "falls": {
        "kinds": ("fall",), "statuses": None, "count": count_events, "compare": "absolute", "min": "FALLS",
        "window": "FALL_DAYS",
        "say": lambda n, days, usual: f"{plural(n, 'fall')} recorded in the last {days} days{usual}"},
}


def history_start(log):
    with log.db() as db:
        row = db.execute("SELECT MIN(at) AS first FROM events WHERE kind != 'flag'").fetchone()
    return row["first"]


def assess(log, rule, now, first):
    """What one rule sees now: counts, baseline, whether it triggers, and the evidence."""
    spec = RULES[rule]
    days = int(setting(spec.get("window", "WINDOW_DAYS")))
    minimum = setting(spec["min"])

    def matching(since, until):
        # Staff view: an event the resident kept private is not used against them.
        found = log.events(since, until, kinds=spec["kinds"], audience="staff", limit=5000)
        return [e for e in found if spec["statuses"] is None or e["status"] in spec["statuses"]]

    current_since = now - days * DAY
    current = matching(current_since, now)
    count = spec["count"](current)
    # Baseline: up to BASELINE_DAYS before the window, as much of it as the log covers.
    baseline_days = setting("BASELINE_DAYS")
    available = (current_since - first) / DAY if first is not None else 0
    baseline = None
    if spec["compare"] != "absolute" and available >= setting("MIN_BASELINE_DAYS") and baseline_days > 0:
        span = min(baseline_days, available)
        before = matching(current_since - span * DAY, current_since)
        baseline = {"days": round(span, 1), "since": store.iso(current_since - span * DAY),
                    "until": store.iso(current_since), "count": spec["count"](before),
                    "per_window": round(spec["count"](before) * days / span, 1)}

    if count < minimum or count == 0:
        triggered = False
    elif baseline is None or spec["compare"] == "absolute":
        triggered = True
    elif spec["compare"] == "ratio":
        triggered = count >= setting("RATIO") * baseline["per_window"]
    else:  # "more": a steady long-standing pattern is not flagged again and again
        triggered = count > baseline["per_window"]

    if spec["compare"] == "absolute":
        basis, note = "absolute", "Fixed threshold."
    elif baseline is None:
        basis = "absolute"
        note = (f"Less than {int(setting('MIN_BASELINE_DAYS'))} days of history before this window; "
                "compared with the fixed threshold only.")
    else:
        basis, note = "baseline", "Compared with the resident's own earlier weeks."
    if baseline is None:
        usual = ""
    elif baseline["count"]:
        usual = f" (usually about {baseline['per_window']:g})"
    else:
        usual = f" (none in the {baseline['days']:g} days before)"
    ids = [e["id"] for e in current]
    return {
        "rule": rule, "triggered": triggered, "current": count,
        "summary": spec["say"](count, days, usual) + SUFFIX,
        "data": {"rule": rule, "review": "staff", "basis": basis, "note": note,
                 "window": {"days": days, "since": store.iso(current_since), "until": store.iso(now)},
                 "baseline": baseline, "current": count,
                 "threshold": {"min": minimum, **({"ratio": setting("RATIO")} if spec["compare"] == "ratio" else {})},
                 "event_ids": ids[-EVIDENCE_CAP:], "event_count": len(ids),
                 "event_ids_truncated": len(ids) > EVIDENCE_CAP}}


def worsened(current, previous):
    """Enough worse than the last flag to raise it again inside the cooldown (+25%, at least +1)."""
    return current >= previous + max(1, math.ceil(previous * 0.25))


def evaluate(log=None, now=None, record=True):
    """Run every rule once; record new flags. Returns the assessments (each with `flagged`)."""
    log = log or store.log
    now = log.clock() if now is None else now
    if not enabled():
        return []
    night.close_stale(log, now)
    first = history_start(log)
    state = log.setting(STATE_KEY, {})
    cooldown = setting("COOLDOWN_DAYS") * DAY
    results = []
    for rule in RULES:
        result = assess(log, rule, now, first)
        previous = state.get(rule)
        result["flagged"] = False
        if result["triggered"]:
            quiet = previous and now - previous["at"] < cooldown and not worsened(result["current"], previous["current"])
            if quiet:
                result["skipped"] = "cooldown"
            elif record:
                event_id = log.record("flag", result["summary"], status="attention", data=result["data"])
                state[rule] = {"at": now, "current": result["current"], "event_id": event_id}
                result.update(flagged=True, event_id=event_id)
        results.append(result)
    if record:
        log.set_setting(STATE_KEY, state)
    return results


def claim_run(log, now, every):
    """Atomically take this week's run, so a restart or a second process doesn't run it twice."""
    with log.db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (LAST_RUN_KEY,)).fetchone()
        last = json.loads(row["value"]) if row else None
        if last is not None and now - last < every * DAY:
            return False, last
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (LAST_RUN_KEY, json.dumps(now)))
        return True, last


def evaluate_if_due(log=None, now=None):
    """Weekly run from the dashboard loop. Returns the results, or None if not due."""
    log = log or store.log
    now = log.clock() if now is None else now
    every = setting("EVERY_DAYS")
    if not enabled() or every <= 0:
        return None
    claimed, last = claim_run(log, now, every)
    if not claimed:
        return None
    try:
        return evaluate(log, now)
    except Exception:
        log.set_setting(LAST_RUN_KEY, last)  # Try again on the next loop instead of next week.
        raise


def recent(log=None, days=60):
    log = log or store.log
    return log.events(log.clock() - days * DAY, kinds=["flag"], audience="staff")


if __name__ == "__main__":  # Manual on-demand run on the robot: python -m features.care.flags
    for item in evaluate(store.log, time.time()):
        print(("FLAGGED " if item["flagged"] else "        ") + item["summary"])
