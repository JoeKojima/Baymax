"""Weekly family update email: warm, plain language, and only what the resident agreed to share.

Built from `log.events(audience="family")`, which already returns nothing until consent is
recorded, only consented kinds, and never private events. On top of that the email uses
summaries and counts only, never the resident's own words. No model call.
"""
import os
from datetime import datetime

from . import store

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Counted rather than listed: staff tell family about these in person.
COUNTED = {
    "check_in": "chatted with Ember at {n} daily check-in{s}",
    "request": "asked the staff for help with {n} everyday thing{s}, like housekeeping",
    "help_request": "asked for someone to come by {n} time{s}",
    "night_wake": "was up during the night {n} time{s}",
    "fall": "had {n} fall{s} logged; the care team can tell you more",
    "flag": "had {n} item{s} noted for the care team to review",
}
LISTED = {"activity": "Activities", "meal": "Meals", "note": "Other news"}
TIMES = {1: "", 2: " (twice)"}


def schedule():
    """("sun", 18, 0) from EMBER_CARE_FAMILY_DIGEST="sun 18:00", or None when off."""
    value = os.getenv("EMBER_CARE_FAMILY_DIGEST", "").strip().lower()
    if not value:
        return None
    try:
        day, at = value.split()
        hour, minute = (int(part) for part in at.split(":"))
        DAYS.index(day[:3])
        return day[:3], hour, minute
    except ValueError:
        print("[CARE] EMBER_CARE_FAMILY_DIGEST should look like 'sun 18:00'; family update off.", flush=True)
        return None


def week_key(now):
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def due(now, sent_week, when):
    """Once per ISO week, at or after the configured day and time (catches up later that week)."""
    day, hour, minute = when
    scheduled = (DAYS.index(day), hour, minute)
    return sent_week != week_key(now) and (now.weekday(), now.hour, now.minute) >= scheduled


def family_update(log, name, until):
    """(subject, body) for the week ending `until` (epoch), or None when there's nothing to share."""
    events = log.events(until - 7 * 86400, until, audience="family", limit=5000)
    if not events:
        return None
    start = datetime.fromtimestamp(until - 7 * 86400)
    end = datetime.fromtimestamp(until - 1)
    by_kind = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)
    parts = [f"Hello from Ember. Here is a short note about {name}'s week "
             f"({start:%d %B} to {end:%d %B}).", ""]
    for kind, heading in LISTED.items():
        if kind in by_kind:
            counts = {}
            for e in by_kind[kind]:
                counts[e["summary"]] = counts.get(e["summary"], 0) + 1  # Summaries only, never quotes.
            parts.append(f"{heading}:")
            parts += [f"- {summary}{TIMES.get(n, f' ({n} times)')}" for summary, n in counts.items()]
            parts.append("")
    sentences = []
    for kind, template in COUNTED.items():
        if kind in by_kind:
            n = len(by_kind[kind])
            sentences.append(template.format(n=n, s="" if n == 1 else "s"))
    if sentences:
        parts += [f"This week {name} " + "; ".join(sentences) + ".", ""]
    parts += [f"You get this because {name} agreed to share these updates with family. "
              "For anything else, please contact the care team directly. Ember is a companion, "
              "not a medical device, and this note is not a care record."]
    return f"A note about {name}'s week", "\n".join(parts)


def send_if_due(hooks, family_emails, now=None, log=None):
    """Weekly email to paired family accounts. Nothing is sent without recorded consent."""
    log = log or store.log
    when = schedule()
    if not (store.enabled() and when):
        return False
    now = now or datetime.fromtimestamp(log.clock()).astimezone()
    if not due(now, log.setting("family_digest_week"), when):
        return False
    if not log.family_kinds():
        return False  # No consent: nothing to send, and nothing to remember.
    recipients = family_emails()
    name = os.getenv("EMBER_CARE_RESIDENT_NAME", "").strip() or "your family member"
    message = family_update(log, name, now.timestamp())
    if not (recipients and message):
        return False
    # Remember first: a crash after this risks one missed week, never a duplicate email.
    log.set_setting("family_digest_week", week_key(now))
    hooks["send_email"](recipients, *message, "family update")
    return True

