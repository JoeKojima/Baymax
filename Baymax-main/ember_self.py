"""
Ember's sense of self.

1. Profile — who Ember cares for, set by their family in the app: the
   person's name, a few words about them, and the people in their life.
   Stored on the robot as ember_profile.json, which is written by:
     - the local dashboard (baymax_app.py, "Profile" tab), or
     - sync_profile_from_cloud(), which fetches GET /api/device/profile from
       the cloud (device-key auth, like /api/device/jobs) at each Gemini
       session start and every few minutes during one, when BAYMAX_DEVICE_KEY
       is set. Offline, the last copy is used. Edits made mid-session are
       passed to Ember with profile_update_message().

2. Identity — build_identity() turns ember_identity.md + the profile into
   the first part of Gemini's system instruction: who Ember is, its body,
   role and priorities, the person it cares for, and what it can and can't
   do. The capability list comes from the features actually running, so
   "what can you do?" stays accurate as features are added or turned off.

3. Live status — SelfState holds live facts (time of day, whether someone is
   in view, when they last talked) and produces short "[SELF] ..." updates
   for the Gemini session: one at session start, then only when something
   has changed and stayed changed (debounced), plus a periodic time refresh.
"""
import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import dataclass

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IDENTITY_PATH = os.path.join(SCRIPT_DIR, "ember_identity.md")
PROFILE_PATH = os.path.join(SCRIPT_DIR, "ember_profile.json")

DEFAULT_ROBOT_NAME = "Ember"
PROFILE_TIMEOUT = 5

# Limits on family-entered text (it goes into the system instruction)
MAX_NAME = 60
MAX_ABOUT = 1000
MAX_NOTES = 200
MAX_PEOPLE = 20

# Used if ember_identity.md is missing or unreadable, so Ember never boots
# without knowing who it is.
_FALLBACK_IDENTITY = (
    "You are {robot_name}, a companion robot for older adults, living in the "
    "home of {user}. You offer compassionate help with daily tasks and good "
    "company. You are a robot, not a person. Your priorities are their safety, "
    "their wellbeing, and then helping and keeping them company. You are not a "
    "doctor.\n\n{about_user}\n\nThe people in their life:\n{household}\n\n"
    "What you can do:\n{capabilities}\n\nWhat you can't do:\n{limitations}"
)


# ─── Profile ─────────────────────────────────────────────────────────────────
def _clean(value, limit):
    text = " ".join(str(value or "").split())   # collapse whitespace/newlines
    return text[:limit].strip()


def normalize_profile(data):
    """Validate and trim a profile from the app or the cloud.
    Unknown fields are dropped; bad types become empty."""
    data = data if isinstance(data, dict) else {}
    people = data.get("household")
    people = people if isinstance(people, list) else []
    household = []
    for person in people[:MAX_PEOPLE]:
        if not isinstance(person, dict):
            continue
        name = _clean(person.get("name"), MAX_NAME)
        if name:
            household.append({
                "name": name,
                "relationship": _clean(person.get("relationship"), MAX_NAME),
                "notes": _clean(person.get("notes"), MAX_NOTES),
            })
    profile = {
        "user_name": _clean(data.get("user_name"), MAX_NAME),
        "about": _clean(data.get("about"), MAX_ABOUT),
        "household": household,
    }
    robot_name = _clean(data.get("robot_name"), MAX_NAME)
    if robot_name:
        profile["robot_name"] = robot_name
    for key in ("updated_at", "updated_by"):
        if data.get(key):
            profile[key] = _clean(data[key], 100)
    return profile


def load_profile(path=None):
    path = path or PROFILE_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return normalize_profile(json.load(f))
    except (OSError, ValueError):
        return normalize_profile({})


def save_profile(profile, path=None):
    path = path or PROFILE_PATH
    profile = normalize_profile(profile)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    os.replace(tmp, path)
    return profile


def sync_profile_from_cloud(cloud_url, device_key, path=None):
    """Fetch the family-entered profile from the cloud and cache it locally.
    Returns "updated", "unchanged", "none" (no profile set yet), or "offline".
    Never raises: on any failure the cached copy stays in use."""
    if not device_key:
        return "offline"
    try:
        res = requests.get(f"{cloud_url.rstrip('/')}/api/device/profile",
                           headers={"Authorization": f"Bearer {device_key}"},
                           timeout=PROFILE_TIMEOUT)
        if res.status_code == 404:
            return "none"
        res.raise_for_status()
        profile = normalize_profile(res.json())
    except (requests.RequestException, ValueError) as e:
        print(f"[SELF] Profile sync failed ({e}); using the saved profile.")
        return "offline"
    if profile == load_profile(path):
        return "unchanged"
    save_profile(profile, path)
    return "updated"


# ─── Identity ────────────────────────────────────────────────────────────────
@dataclass
class Capability:
    """One thing Ember can do.  `when_off` describes the gap honestly if the
    feature is disabled on this robot (None = just leave it out)."""
    does: str
    enabled: bool = True
    when_off: str = None


def _bullets(lines):
    return "\n".join(f"- {line}" for line in lines)


def format_household(people):
    if not people:
        return ("Their family hasn't told you about anyone yet. Learn people's "
                "names naturally as they come up.")
    lines = []
    for p in people:
        line = p["name"]
        if p.get("relationship"):
            line += f" — {p['relationship']}"
        if p.get("notes"):
            line += f" ({p['notes']})"
        lines.append(line)
    return _bullets(lines)


def format_about(profile):
    name = profile.get("user_name")
    about = profile.get("about")
    if not name:
        lines = ["Their family hasn't told you their name yet. If they tell you, use it."]
    else:
        lines = [f"Their name is {name}. Call them {name} unless they ask otherwise."]
    if about:
        lines.append(f"From their family: {about}")
    return "\n".join(lines)


def profile_update_message(profile):
    """Mid-session notice when the family edits the profile in the app."""
    profile = normalize_profile(profile)
    return ("[PROFILE UPDATE] Their family just updated what you know about them. "
            "This replaces what you were told before:\n"
            + format_about(profile)
            + "\nThe people in their life:\n" + format_household(profile["household"]))


def profile_mtime(path=None):
    try:
        return os.path.getmtime(path or PROFILE_PATH)
    except OSError:
        return None


def build_identity(capabilities, profile=None, path=None):
    """Render the identity section of the system instruction."""
    path = path or IDENTITY_PATH
    profile = normalize_profile(profile or {})
    try:
        with open(path, encoding="utf-8") as f:
            template = f.read()
    except OSError as e:
        print(f"[SELF] Could not read {path} ({e}); using built-in identity.")
        template = _FALLBACK_IDENTITY

    template = re.sub(r"<!--.*?-->", "", template, flags=re.DOTALL).strip()

    can = [c.does for c in capabilities if c.enabled]
    cant = [c.when_off for c in capabilities if not c.enabled and c.when_off]
    values = {
        "robot_name": profile.get("robot_name") or DEFAULT_ROBOT_NAME,
        "user": profile["user_name"] or "the person you care for",
        "about_user": format_about(profile),
        "household": format_household(profile["household"]),
        "capabilities": _bullets(can) if can else "- Talk with people.",
        "limitations": _bullets(cant),
    }
    if not cant:
        # Nothing switched off: drop the placeholder's whole line
        template = re.sub(r"[ \t]*\{limitations\}[ \t]*\n?", "", template)
    # Plain replacement (not str.format) so braces typed by the family are harmless
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return re.sub(r"\n{3,}", "\n\n", template)


# ─── Live status ─────────────────────────────────────────────────────────────
def describe_time(now):
    hour = now.hour % 12 or 12
    part = ("early morning" if now.hour < 6 else "morning" if now.hour < 12
            else "afternoon" if now.hour < 17 else "evening" if now.hour < 21 else "night")
    return (f"It's {now.strftime('%A')}, {now.strftime('%B')} {now.day}, "
            f"{hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'} ({part}).")


def describe_last_talked(seconds_ago, name=None):
    """Coarse buckets so the fact only changes a few times an hour."""
    who = name or "them"
    if seconds_ago is None:
        return f"You haven't heard from {who} since you started up."
    if seconds_ago < 10 * 60:
        return None                     # mid-conversation: nothing to say
    if seconds_ago < 60 * 60:
        return f"You last heard from {who} less than an hour ago."
    hours = int(seconds_ago // 3600)
    return f"You last heard from {who} about {hours} hour{'s' if hours != 1 else ''} ago."


class SelfState:
    """Live facts about Ember, turned into "[SELF]" updates for Gemini.

    set(key, sentence) records a fact (None removes it).  pending_update()
    returns the text to send, or None:
      - always at the start of a session,
      - after a fact changes and then stays unchanged for `debounce` seconds
        (so a face flickering in and out of view doesn't spam the session),
        but no more often than every `min_interval` seconds,
      - otherwise every `refresh_interval` seconds, to keep the time current.
    """

    def __init__(self, clock=time.time, debounce=10.0, min_interval=60.0,
                 refresh_interval=1800.0):
        self._clock = clock
        self._debounce = debounce
        self._min_interval = min_interval
        self._refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._facts = {}
        self._sent_facts = None      # facts as of the last update sent
        self._last_change = 0.0
        self._last_sent = None       # None = nothing sent this session

    def set(self, key, sentence):
        with self._lock:
            if sentence is None:
                if key in self._facts:
                    del self._facts[key]
                    self._last_change = self._clock()
            elif self._facts.get(key) != sentence:
                self._facts[key] = sentence
                self._last_change = self._clock()

    def snapshot(self):
        now = dt.datetime.fromtimestamp(self._clock())
        with self._lock:
            facts = [self._facts[k] for k in sorted(self._facts)]
        return " ".join(["[SELF]", describe_time(now)] + facts)

    def new_session(self):
        """Call when a Gemini session starts — it has no status yet."""
        with self._lock:
            self._last_sent = None

    def pending_update(self):
        now = self._clock()
        with self._lock:
            first = self._last_sent is None
            changed = self._facts != self._sent_facts
            settled = now - self._last_change >= self._debounce
            spaced = first or now - self._last_sent >= self._min_interval
            stale = not first and now - self._last_sent >= self._refresh_interval
            if not (first or (changed and settled and spaced) or stale):
                return None
            self._last_sent = now
            self._sent_facts = dict(self._facts)
        return self.snapshot()
