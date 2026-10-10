"""
Per-person conversation memory — so Ember greets people it knows by name,
picks up where they left off ("How did the interview go, Sarah?"), and
notices when someone looks different ("Did you get a haircut?").

1. ConversationLog records every finished utterance in order, with who was
   in front of the camera at the time. Each line is appended to
   conversation_pending.jsonl as it happens, so a crash or power cut loses
   nothing: unsaved conversations are summarised on the next start.

2. When a conversation ends (CONVERSATION_GAP of silence) — not only at
   shutdown — summarise() asks Gemini Flash to pull out what's worth
   remembering and *who it's about*. Each memory is saved to ChromaDB with
   a "person" field, so memories_about("Sarah") recalls Sarah's things.

3. check_appearance() describes how a person looks (hair, glasses, facial
   hair, accessories — never body, weight, skin or age) and compares it with
   the description saved last time. Only the text description is stored,
   never the image.

4. arrival_prompt() turns all of that into the "[ARRIVED] ..." message that
   prompts Ember to greet someone it knows when they come into view.

Gemini calls are passed in as `generate(prompt, image_jpeg=None) -> str`, so
everything here can be tested without network access.
"""
import datetime as dt
import json
import os
import re
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_PATH = os.path.join(SCRIPT_DIR, "conversation_pending.jsonl")

CONVERSATION_GAP = 180.0      # seconds of silence that ends a conversation
MIN_USER_TURNS = 1            # don't summarise conversations nobody spoke in
MEMORIES_PER_PERSON = 5       # recalled when someone arrives


# ─── Conversation log ────────────────────────────────────────────────────────
class ConversationLog:
    """Finished utterances in order: {"t", "speaker": "user"|"ember", "text",
    "present": [names in view]}. Persisted line by line until summarised."""

    def __init__(self, path=None, clock=time.time):
        self.path = path or PENDING_PATH
        self._clock = clock
        self._lock = threading.Lock()
        self._turns = self._load()

    def _load(self):
        turns = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        turn = json.loads(line)
                        if isinstance(turn, dict) and turn.get("text"):
                            turns.append(turn)
                    except ValueError:
                        continue            # a half-written last line after a crash
        except OSError:
            pass
        if turns:
            print(f"[CONVO] {len(turns)} unsaved lines from last time will be remembered")
        return turns

    def add(self, speaker, text, present=()):
        text = " ".join(str(text or "").split())
        if not text:
            return
        turn = {"t": self._clock(), "speaker": "ember" if speaker != "user" else "user",
                "text": text, "present": list(present)}
        with self._lock:
            self._turns.append(turn)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(turn) + "\n")
            except OSError as e:
                print(f"[CONVO] Could not save line: {e}")

    def finished(self, gap=None):
        """The pending conversation, if it has gone quiet for `gap` seconds
        (default CONVERSATION_GAP, read at call time so it can be tuned)."""
        gap = CONVERSATION_GAP if gap is None else gap
        with self._lock:
            if self._turns and self._clock() - self._turns[-1]["t"] >= gap:
                return list(self._turns)
        return []

    def pending(self):
        with self._lock:
            return list(self._turns)

    def mark_saved(self, turns):
        """Drop turns that have been summarised (new ones may have arrived since)."""
        saved = {(t["t"], t["text"]) for t in turns}
        with self._lock:
            self._turns = [t for t in self._turns if (t["t"], t["text"]) not in saved]
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    for t in self._turns:
                        f.write(json.dumps(t) + "\n")
                os.replace(tmp, self.path)
            except OSError as e:
                print(f"[CONVO] Could not update {self.path}: {e}")


def _clock_text(ts):
    d = dt.datetime.fromtimestamp(ts)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def format_transcript(turns, robot_name="Ember"):
    """Readable transcript; notes who was in view whenever that changes."""
    lines, last_present = [], None
    for t in turns:
        present = t.get("present") or []
        if present != last_present:
            who = ", ".join(present) if present else "nobody visible"
            lines.append(f"[{_clock_text(t['t'])} — in front of {robot_name}: {who}]")
            last_present = present
        speaker = robot_name if t["speaker"] == "ember" else "Person"
        lines.append(f"{speaker}: {t['text']}")
    return "\n".join(lines)


# ─── Summarising into per-person memories ───────────────────────────────────
def build_summary_prompt(turns, profile, robot_name="Ember"):
    user = profile.get("user_name") or ""
    people = [f"{user} (the person {robot_name} lives with and cares for)"] if user else []
    people += [f"{h['name']} ({h['relationship']})" if h.get("relationship") else h["name"]
               for h in profile.get("household", [])]
    day = dt.datetime.fromtimestamp(turns[0]["t"])
    date = f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}"
    return (
        f"You are the memory of {robot_name}, a companion robot for an older adult. "
        f"Below is a conversation from {date}. The transcript doesn't label which person "
        "said each line; the bracketed notes say who was in front of the robot's camera "
        "at the time. When only one known person is in view, the 'Person' lines are "
        "almost certainly them. When several people are in view, use names, context "
        "(\"my mom\", \"I'm Sarah\") and common sense; if you can't tell who something "
        "is about, use \"\" for person.\n\n"
        + (f"People {robot_name} knows: {'; '.join(people)}.\n\n" if people else "")
        + "Extract what's worth remembering for future conversations: personal facts, "
        "plans and upcoming events (with dates when given), worries, feelings, "
        "preferences, and the people they mentioned. Also add ONE line per person who "
        "took part, of the form \"On " + date + " you talked with <name> about ...\", so "
        f"{robot_name} can pick up where they left off. Write each memory as a short, "
        "self-contained sentence using the person's name, not \"the user\".\n\n"
        "Reply with JSON only: {\"memories\": [{\"person\": \"<name or empty>\", "
        "\"memory\": \"<sentence>\"}]}. If nothing is worth remembering, reply "
        "{\"memories\": []}.\n\n"
        "--- CONVERSATION ---\n" + format_transcript(turns, robot_name) + "\n--- END ---"
    )


def _json_from_text(text):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object")
    return json.loads(text[start:end + 1])


def _canonical_person(name, profile):
    """Map Flash's name to the family's spelling; unknown or generic → ""."""
    name = " ".join(str(name or "").split())
    if not name or name.lower() in ("user", "the user", "unknown", "unclear", "none", "person"):
        return ""
    known = [profile.get("user_name") or ""] + [h["name"] for h in profile.get("household", [])]
    for k in known:
        if k and (k.lower() == name.lower() or k.split()[0].lower() == name.split()[0].lower()):
            return k
    return name


def parse_memories(text, profile):
    """Flash's reply → [{"person", "memory"}]. Tolerates code fences; if the
    reply isn't JSON at all, each line becomes an unattributed memory."""
    try:
        data = _json_from_text(text)
        items = data.get("memories", []) if isinstance(data, dict) else []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            memory = " ".join(str(item.get("memory") or "").split())
            if memory:
                out.append({"person": _canonical_person(item.get("person"), profile),
                            "memory": memory})
        return out
    except (ValueError, AttributeError):
        lines = [ln.strip(" -*•\t") for ln in (text or "").splitlines()]
        return [{"person": "", "memory": ln} for ln in lines
                if ln and ln != "NOTHING_TO_REMEMBER" and not ln.startswith("{")]


def summarise(turns, profile, generate, robot_name="Ember"):
    """Turn a finished conversation into per-person memories (may be [])."""
    if sum(t["speaker"] == "user" for t in turns) < MIN_USER_TURNS:
        return []
    return parse_memories(generate(build_summary_prompt(turns, profile, robot_name)), profile)


def save_memories(embedder, items, when=None):
    """Store memories in ChromaDB with person + timestamp metadata."""
    if not items:
        return 0
    when = when or time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    ids = [f"memory_{stamp}_{i}_{int(when * 1000) % 100000}" for i in range(len(items))]
    metadatas = [{"source": "conversation_summary", "timestamp": stamp, "time": float(when),
                  "person": item["person"], "line_index": str(i)}
                 for i, item in enumerate(items)]
    embedder.save([item["memory"] for item in items], ids=ids, metadatas=metadatas)
    return len(items)


def _all_memories(embedder):
    collection = getattr(embedder, "_collection", None)
    if collection is None:
        return []
    got = collection.get(include=["documents", "metadatas"])
    docs = got.get("documents") or []
    metas = got.get("metadatas") or [None] * len(docs)   # missing metadata → unattributed
    out = []
    for doc, meta in zip(docs, metas):
        meta = meta or {}
        when = meta.get("time")
        if when is None:                        # older memories only have "timestamp"
            try:
                when = time.mktime(time.strptime(meta.get("timestamp", ""), "%Y%m%d_%H%M%S"))
            except ValueError:
                when = 0.0
        out.append({"memory": doc, "person": meta.get("person", ""), "time": float(when)})
    return out


def memories_about(embedder, name, profile, limit=None):
    """Newest memories about `name`. Memories saved before people were tracked
    have no person and are treated as being about the person Ember cares for."""
    user = (profile.get("user_name") or "").lower()
    key = (name or "").lower()
    picked = [m for m in _all_memories(embedder)
              if m["person"].lower() == key or (key == user and key and not m["person"])]
    picked.sort(key=lambda m: m["time"], reverse=True)
    return [m["memory"] for m in picked[:limit or MEMORIES_PER_PERSON]]


def memory_block(embedder, profile, limit=50):
    """System-instruction section: remembered facts grouped by person."""
    memories = sorted(_all_memories(embedder), key=lambda m: m["time"], reverse=True)[:limit]
    if not memories:
        return ""
    user = profile.get("user_name") or ""
    groups = {}
    for m in memories:
        groups.setdefault(m["person"] or user, []).append(m["memory"])
    lines = ["What you remember from earlier conversations (treat these as established "
             "facts — don't second-guess them):"]
    for person in sorted(groups, key=lambda p: (p != user, p.lower())):
        lines.append(f"About {person or 'the person you care for'}:")
        lines += [f"- {m}" for m in groups[person]]
    return "\n".join(lines)


# ─── Appearance ──────────────────────────────────────────────────────────────
def build_appearance_prompt(previous):
    base = ("This is a photo of a person from a home companion robot's camera. Describe "
            "only their visible appearance in at most 25 words: hair (length, colour, "
            "style), glasses, facial hair, hat, jewellery or other accessories. Never "
            "mention body shape, weight, skin, age, health or attractiveness.")
    if previous:
        base += ("\n\nHow they looked last time: \"" + previous + "\". Has something "
                 "clearly changed that a friend would notice and kindly mention — e.g. a "
                 "haircut or new colour, new glasses, a shaved or new beard? Ignore "
                 "lighting, camera angle and ordinary clothing changes. Only say changed "
                 "if you're confident.")
    return base + ("\n\nReply with JSON only: {\"description\": \"...\", \"changed\": "
                   "true|false, \"change\": \"<short, e.g. 'shorter hair'> or empty\"}")


def check_appearance(generate, image_jpeg, previous=None):
    """→ {"description", "changed", "change"} or None if it couldn't be judged."""
    try:
        data = _json_from_text(generate(build_appearance_prompt(previous), image_jpeg))
    except Exception as e:
        print(f"[APPEARANCE] Check failed: {e}")
        return None
    description = " ".join(str(data.get("description") or "").split())[:200]
    if not description:
        return None
    change = " ".join(str(data.get("change") or "").split())[:100]
    changed = bool(previous) and data.get("changed") is True and bool(change)
    return {"description": description, "changed": changed, "change": change if changed else ""}


# ─── Arrival greeting ────────────────────────────────────────────────────────
def describe_away(seconds, last_seen_ts=None, now_ts=None):
    if seconds is None:
        return None
    if seconds < 3600:
        return f"about {max(1, int(seconds // 60))} minutes ago"
    if seconds < 20 * 3600:
        hours = int(seconds // 3600)
        return f"about {hours} hour{'s' if hours != 1 else ''} ago"
    days = int(round(seconds / 86400))
    if last_seen_ts and days < 7:
        when = dt.datetime.fromtimestamp(last_seen_ts).strftime("%A")
        return "yesterday" if days <= 1 else f"{days} days ago ({when})"
    return f"about {days} days ago"


def arrival_prompt(label, name, away_seconds, last_seen_ts, memories, appearance=None,
                   is_primary=False):
    """The [ARRIVED] message that prompts Ember to greet someone it knows."""
    away = describe_away(away_seconds, last_seen_ts, time.time())
    parts = [f"[ARRIVED] {label} just came into view."]
    if away:
        parts.append(f"You last saw {name} {away}.")
    else:
        parts.append(f"This is the first time you've seen {name} since you learned their face.")
    if memories:
        parts.append(f"Things you remember about {name}, newest first:\n"
                     + "\n".join(f"- {m}" for m in memories))
    if appearance and appearance.get("changed"):
        parts.append(f"{name} looks a little different from last time: {appearance['change']}.")
    ask = (f"Greet {name} warmly by name" if not is_primary
           else f"Welcome {name} back warmly by name")
    guidance = [
        f"{ask} — keep it short and natural.",
        "If something from last time is worth following up (a plan, an event, a worry), "
        "ask about one of them the way a friend would — don't list what you remember.",
    ]
    if appearance and appearance.get("changed"):
        guidance.append("You may also mention the change kindly as a conversation starter "
                        "(e.g. \"Did you get a haircut? It looks nice!\"). Never comment on "
                        "weight, body, skin or age.")
    guidance.append("If they're busy or talking with someone else, a quick hello is enough.")
    return "\n".join(parts) + "\n" + " ".join(guidance)
