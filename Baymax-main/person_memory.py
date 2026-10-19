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
   remembering. Each memory is one moment or fact, stored once, linked to
   EVERYONE it involves ("Sarah told Margaret about her interview" →
   people [Sarah, Margaret]). memories_involving("Margaret") finds every
   memory that includes Margaret. Repeats of an existing memory refresh it
   instead of piling up; forget_person() removes someone everywhere.

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
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_PATH = os.path.join(os.getenv("BAYMAX_DATA_DIR", SCRIPT_DIR), "conversation_pending.jsonl")

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
        self.forgotten = {}          # name -> when they asked to be forgotten (not yet saved)
        self._turns = self._load()

    def _load(self):
        turns, content = [], ""
        try:
            with open(self.path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            pass
        for line in content.splitlines():
            try:
                turn = json.loads(line)
                if isinstance(turn, dict) and turn.get("forget"):
                    self.forgotten[str(turn["forget"])] = float(turn.get("t") or self._clock())
                elif isinstance(turn, dict) and turn.get("text"):
                    turns.append(turn)
            except (ValueError, TypeError):
                continue                    # a half-written last line after a crash
        if turns:
            print(f"[CONVO] {len(turns)} unsaved lines from last time will be remembered")
        if content and not content.endswith("\n"):
            # A half-written line from a crash: rewrite now, or the next line
            # would be glued onto it and lost.
            self._turns = turns
            self._rewrite()
        return turns

    def add(self, speaker, text, present=(), speaker_id=None):
        text = " ".join(str(text or "").split())
        if not text:
            return
        turn = {"t": self._clock(), "speaker": "ember" if speaker != "user" else "user",
                "text": text, "present": list(present)}
        if speaker == 'user' and speaker_id:
            turn['speaker_id'] = speaker_id
        with self._lock:
            self._turns.append(turn)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(turn) + "\n")
            except OSError as e:
                print(f"[CONVO] Could not save line: {e}")

    def finished(self, gap=None, ending=False):
        """The oldest pending conversation that is over: lines up to the first
        silence of `gap` seconds (default CONVERSATION_GAP, read at call time),
        so separate conversations — e.g. yesterday's, recovered after a power
        cut, and today's — are summarised separately. ending=True (shutdown):
        the last conversation counts as over too."""
        gap = CONVERSATION_GAP if gap is None else gap
        with self._lock:
            turns = self._turns
            for i in range(1, len(turns)):
                step = turns[i]["t"] - turns[i - 1]["t"]
                if step >= gap or step < -60:      # silence, or the clock was set back
                    return list(turns[:i])
            if turns and (ending or self._clock() - turns[-1]["t"] >= gap):
                return list(turns)
        return []

    def forget(self, name):
        """Someone asked to be forgotten: take them out of who-was-here for the
        unsaved lines, and leave them out when this conversation is saved —
        including lines said after the request (often their own "forget me")."""
        with self._lock:
            for t in self._turns:
                t["present"] = [n for n in t.get("present") or [] if n.lower() != name.lower()]
            self.forgotten[name] = self._clock()
            self._rewrite()

    def forgotten_names(self):
        with self._lock:
            return set(self.forgotten)

    def forgotten_for(self, turns):
        """Names to leave out of this conversation: asked during it, just
        before it began, or since."""
        if not turns:
            return set()
        start = turns[0]["t"] - CONVERSATION_GAP
        with self._lock:
            return {n for n, t in self.forgotten.items() if t >= start}

    def pending(self):
        with self._lock:
            return list(self._turns)

    def mark_saved(self, turns):
        """Drop turns that have been summarised (new ones may have arrived since)."""
        saved = {(t["t"], t["text"]) for t in turns}
        with self._lock:
            self._turns = [t for t in self._turns if (t["t"], t["text"]) not in saved]
            if turns:
                # Drop the requests that applied to it (see forgotten_for),
                # and any left over for more than a day
                start, end = turns[0]["t"] - CONVERSATION_GAP, turns[-1]["t"]
                # (an old one no unsaved line could still use is dropped too)
                usable_from = (self._turns[0]["t"] - CONVERSATION_GAP if self._turns
                               else self._clock() - 86400)
                self.forgotten = {n: t for n, t in self.forgotten.items()
                                  if not start <= t <= end and t >= usable_from}
            self._rewrite()

    def _rewrite(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for name, t in sorted(self.forgotten.items()):
                    f.write(json.dumps({"forget": name, "t": t}) + "\n")
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


# ─── Memories: one per moment, linked to everyone involved ──────────────────
# A memory is something that happened or a fact ("Sarah told Margaret about
# her job interview on Friday"), stored once, with the list of everyone it
# involves: who said or did it, who it's about, who was there, who's
# mentioned. Recall for a person = every memory that includes them.
# Calibrated on the real all-MiniLM-L6-v2 model: re-said facts score 0.01–0.11,
# but so do some *changed* facts ("hip has been hurting" vs "hip is much better"
# = 0.10; the same summary on another day = 0.01). So a match REPLACES the old
# memory with the new wording — a wrong merge can only drop outdated wording.
DUPLICATE_DISTANCE = 0.08
NOT_PEOPLE = {"user", "the user", "unknown", "unclear", "none", "person", "someone",
              "someone unrecognized", "everyone", "nobody", "family", "the family"}


def known_names(profile, extra=()):
    """Names Ember knows: the person it cares for, their household, and anyone
    else it has met (`extra`), without duplicates."""
    names = [profile.get("user_name") or ""]
    names += [h.get("name", "") for h in profile.get("household", [])]
    names += list(extra)
    out = []
    for n in names:
        n = " ".join(str(n or "").split())
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out


def canonical_name(name, known, robot_name="Ember", strict=False):
    """The family's spelling of a name ("sarah" / "Tom" → "Sarah" / "Tom Jones");
    generic words and the robot itself → ""; other names are kept as given.
    Otherwise a name maps to the only known person sharing its first name
    ("Tom" → "Tom Jones", "Sarah Smith" → "Sarah") — fine for linking a
    memory, but never for forgetting: strict=True accepts exact names only,
    since a visiting "Margaret" must not erase "Margaret Jones"."""
    name = " ".join(str(name or "").split())
    if not name or name.lower() in NOT_PEOPLE or name.lower() == robot_name.lower():
        return ""
    for k in known:
        if k.lower() == name.lower():
            return k
    first = name.split()[0].lower()
    if strict or first in _NOT_FIRST_NAMES:
        return name
    matches = [k for k in known if k.split()[0].lower() == first]
    return matches[0] if len(matches) == 1 else name


def _people_list(names, known, robot_name="Ember"):
    out = []
    for n in names:
        c = canonical_name(n, known, robot_name)
        if c and c.lower() not in {o.lower() for o in out}:
            out.append(c)
    return out


def _encode_people(people):
    """Chroma metadata must be scalar, so the list is stored as "|Sarah|Margaret|"."""
    return "|" + "|".join(people) + "|" if people else ""


def _decode_people(value):
    return [p for p in str(value or "").split("|") if p]


def build_summary_prompt(turns, profile, robot_name="Ember", extra_names=()):
    user = profile.get("user_name") or ""
    described = [f"{user} (the configured primary profile, not necessarily the current speaker)"] if user else []
    described += [f"{h['name']} ({h['relationship']})" if h.get("relationship") else h["name"]
                  for h in profile.get("household", [])]
    described += [n for n in extra_names if n.lower() not in
                  {k.lower() for k in known_names(profile)}]
    day = dt.datetime.fromtimestamp(turns[0]["t"])
    date = day.date().isoformat()
    return (
        f"You are the memory of {robot_name}, a companion robot that talks with people. "
        "Do not infer anyone's age, living arrangement or speaker identity from the "
        "configured primary profile. Preserve uncertainty when attribution is unclear. "
        f"Below is a conversation from {date}. The transcript doesn't label which person "
        "said each line; the bracketed notes say who was in front of the robot's camera "
        "at the time. When only one known person is in view, the 'Person' lines are "
        "almost certainly them. When several are, use names, context (\"my mom\", "
        "\"I'm Sarah\") and common sense.\n\n"
        + (f"People {robot_name} knows (use these spellings): {'; '.join(described)}.\n\n"
           if described else "")
        + "Write down what's worth remembering for future conversations: things that "
        "happened, personal facts, plans and upcoming events (with dates when given), "
        "worries, feelings and preferences. Each memory is ONE moment or fact, written "
        "once as a succinct note, retaining negation, uncertainty and skill-level qualifiers. "
        "Use ISO dates (YYYY-MM-DD) only when the year is known; never guess missing dates. "
        "Put ownership in people metadata; omit the subject's repeated name only when exactly "
        "one person is involved and attribution is clear. Keep names needed to distinguish people. "
        "never make separate copies of the same thing for different people.\n"
        "For each memory, list in \"people\" EVERYONE it involves: who said or did it, "
        "who it's about, who was there for it, and anyone mentioned (even people who "
        f"weren't there). Don't list {robot_name}. Use [] only if it involves nobody "
        "specific.\n"
        "Also add at most ONE brief topics-only conversation summary, without repeating "
        "the date or an introductory sentence, with everyone who took part in "
        "\"people\" and \"kind\": \"conversation\" (every other memory has "
        "\"kind\": \"fact\").\n\n"
        "Reply with JSON only: {\"memories\": [{\"memory\": \"<sentence>\", "
        "\"people\": [\"<name>\", ...], \"kind\": \"fact\"}]}. If nothing is worth "
        "remembering, reply {\"memories\": []}.\n\n"
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


def parse_memories(text, profile, extra_names=(), robot_name="Ember"):
    """Flash's reply → [{"memory", "people": [...]}]. Tolerates code fences and
    a single "person" string; if the reply isn't JSON at all, each line becomes
    a memory that involves nobody specific."""
    known = known_names(profile, extra_names)
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    parsed = True
    try:
        data = json.loads(body)
    except ValueError:
        try:
            data = _json_from_text(body)          # JSON with some text around it
        except ValueError:
            data, parsed = None, False
    if parsed and not isinstance(data, (list, dict)):
        return []                                 # null, "NOTHING_TO_REMEMBER", a number…
    if isinstance(data, list):                    # a bare list of memories
        items = data
    elif isinstance(data, dict):
        items = data.get("memories")
        if not isinstance(items, list):
            items = [data] if "memory" in data else []
    elif "{" in body or "[" in body:
        # Broken JSON (e.g. cut off): retry later rather than store fragments
        raise ValueError("unreadable reply from the summariser")
    else:
        lines = [ln.strip(" -*•\t") for ln in body.splitlines()]
        return [{"memory": ln, "people": [], "kind": "fact"} for ln in lines
                if ln and ln != "NOTHING_TO_REMEMBER"]
    out = []
    sentences = all(isinstance(i, str) for i in items)   # a plain list of sentences
    for item in items:
        if isinstance(item, str) and sentences:
            item = {"memory": item}
        if not isinstance(item, dict):
            continue
        memory = " ".join(str(item.get("memory") or "").split())
        if not memory:
            continue
        people = item.get("people", item.get("person", []))
        if isinstance(people, str):
            people = [people]
        if not isinstance(people, list):
            people = []
        people = [p.get("name") if isinstance(p, dict) else p for p in people]
        people = [p for p in people if isinstance(p, str)]
        kind = "conversation" if str(item.get("kind", "")).lower() == "conversation" else "fact"
        out.append({"memory": memory, "people": _people_list(people, known, robot_name),
                    "kind": kind})
    return out


# Not a first name on its own ("Aunt Mary", "the visitor", "Dr Lee")
_NOT_FIRST_NAMES = {"the", "a", "an", "my", "our", "his", "her", "their", "mr", "mr.", "mrs",
                    "mrs.", "ms", "ms.", "miss", "dr", "dr.", "doctor", "aunt", "auntie",
                    "uncle", "grandma", "grandpa", "granny", "nana", "papa", "cousin", "mom",
                    "dad", "mum", "mother", "father", "sister", "brother", "sir", "lady",
                    "saint", "st", "st.", "nurse", "pastor", "father", "neighbor", "friend"}


def name_pattern(name, others=()):
    """Matches someone's name as a word — case-sensitive, so forgetting "Will"
    or "May" doesn't touch "she will visit" or "she may call". Their first
    name alone ("Margaret" for "Margaret Lee") counts only when nobody else
    in `others` shares it and it isn't a title ("Aunt Mary")."""
    name = " ".join(str(name).split())
    parts = [re.escape(name)]
    words = name.split()
    if len(words) > 1 and words[0].lower() not in _NOT_FIRST_NAMES:
        first = words[0].lower()
        shared = any(o and o.lower() != name.lower() and o.split()[0].lower() == first
                     for o in others)
        if not shared:
            parts.append(re.escape(words[0]))
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


def summarise(turns, profile, generate, robot_name="Ember", extra_names=(), forgotten=()):
    """Turn a finished conversation into memories (may be []). Nothing about
    the `forgotten` people is kept."""
    if sum(t["speaker"] == "user" for t in turns) < MIN_USER_TURNS:
        return []
    prompt = build_summary_prompt(turns, profile, robot_name, extra_names)
    if forgotten:
        prompt += ("\n\nThese people asked to be forgotten — write nothing about them: "
                   + ", ".join(sorted(forgotten)) + ".")
    items = parse_memories(generate(prompt), profile, extra_names, robot_name)
    return drop_forgotten(items, forgotten, known_names(profile, extra_names))


def drop_forgotten(items, forgotten, known=()):
    """Leave out memories that involve or name anyone in `forgotten`."""
    for name in forgotten:
        pattern = name_pattern(name, known)
        items = [m for m in items if not pattern.search(m["memory"])
                 and name.lower() not in {p.lower() for p in m["people"]}]
    return items


def _metadata(people, when, mentions=1, kind="fact"):
    """kind "conversation" = a summary of a whole conversation ("On Wednesday,
    Sarah and Margaret talked with Ember about ..."): used for greetings and the
    prompt, but not recalled mid-conversation (it matches anything said to Ember)."""
    return {"source": "conversation_summary",
            "timestamp": time.strftime("%Y%m%d_%H%M%S", time.localtime(when)),
            "time": float(when), "people": _encode_people(people), "mentions": int(mentions),
            "kind": kind}


def save_memories(embedder, items, when=None):
    """Store memories. A memory that says (almost) the same thing as an existing
    one about the same people (cosine distance <= DUPLICATE_DISTANCE) replaces
    it — newest wording and date win — instead of piling up a duplicate.
    Returns (added, replaced)."""
    if not items:
        return 0, 0
    when = when or time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    collection = getattr(embedder, "_collection", None)
    added = refreshed = 0
    for item in items:
        duplicate = None
        if collection is not None and collection.count() > 0:
            hits = embedder.search(item["memory"], n_results=3)
            for h in hits:
                meta = h.get("metadata") or {}
                same_people = ({p.lower() for p in _decode_people(meta.get("people"))}
                               == {p.lower() for p in item["people"]})
                if h["distance"] <= DUPLICATE_DISTANCE and same_people:
                    duplicate = (h, meta)
                    break
        if duplicate:
            h, meta = duplicate
            embedder.save([item["memory"]], ids=[h["id"]], metadatas=[
                _metadata(item["people"], when, int(meta.get("mentions", 1)) + 1,
                          item.get("kind", "fact"))])
            refreshed += 1
        else:
            # Saved one at a time so a repeat later in the same batch is caught too
            embedder.save([item["memory"]], ids=[f"memory_{stamp}_{uuid.uuid4().hex[:8]}"],
                          metadatas=[_metadata(item["people"], when, kind=item.get("kind", "fact"))])
            added += 1
    return added, refreshed


def _all_memories(embedder):
    collection = getattr(embedder, "_collection", None)
    if collection is None:
        return []
    got = collection.get(include=["documents", "metadatas"])
    docs = got.get("documents") or []
    ids = got.get("ids") or [None] * len(docs)
    metas = got.get("metadatas") or [None] * len(docs)   # missing metadata → no people
    out = []
    for mid, doc, meta in zip(ids, docs, metas):
        meta = meta or {}
        when = meta.get("time")
        if when is None:                        # older memories only have "timestamp"
            try:
                when = time.mktime(time.strptime(meta.get("timestamp", ""), "%Y%m%d_%H%M%S"))
            except ValueError:
                when = 0.0
        if "people" in meta:
            people = _decode_people(meta["people"])
        else:                                   # earlier formats
            people = [meta["person"]] if meta.get("person") else []
        out.append({"id": mid, "memory": doc, "people": people, "time": float(when),
                    "legacy": "people" not in meta, "kind": meta.get("kind", "fact")})
    return out


def _involves(memory, name, primary):
    key = (name or "").lower()
    if any(p.lower() == key for p in memory["people"]):
        return True
    # Memories saved before people were tracked are about the person Ember cares for
    return bool(key) and memory["legacy"] and not memory["people"] and key == primary.lower()


def people_in_memories(embedder):
    """Everyone any stored memory is linked to."""
    return sorted({p for m in _all_memories(embedder) for p in m["people"]})


def memories_involving(embedder, name, profile, limit=None):
    """Newest memories that include `name` → [{"id", "memory", "people", "time"}]."""
    primary = profile.get("user_name") or ""
    picked = [m for m in _all_memories(embedder) if _involves(m, name, primary)]
    picked.sort(key=lambda m: m["time"], reverse=True)
    return picked[:limit or MEMORIES_PER_PERSON]


def _when_text(ts, now=None):
    if not ts:
        return ""
    d, today = dt.datetime.fromtimestamp(ts), dt.datetime.fromtimestamp(now or time.time())
    days = (today.date() - d.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return d.strftime("%A")
    return f"{d.strftime('%b')} {d.day}"


def memory_line(m, now=None, about=None):
    """'- Sarah has a job interview on Friday. (Wednesday; with Margaret, Sarah)'."""
    others = [p for p in m["people"] if not about or p.lower() != about.lower()]
    tags = [t for t in (_when_text(m["time"], now),
                        ("with " if about else "") + ", ".join(others) if others else "") if t]
    return f"- {m['memory']}" + (f" ({'; '.join(tags)})" if tags else "")


def memory_block(embedder, profile, limit=50, now=None, focus=(), per_person=10, included=None,
                 exclude=()):
    """System-instruction section: the newest memories plus the newest
    `per_person` involving each person in `focus` (who's in view), each listed
    once with when it was and who it involves. The texts listed are appended
    to `included` (a list) when given. Nothing about anyone in `exclude`
    (people being forgotten) is listed."""
    every = _all_memories(embedder)
    if exclude:
        every = drop_forgotten(every, exclude, known_names(profile))
    memories = sorted(every, key=lambda m: m["time"], reverse=True)[:limit]
    primary = profile.get("user_name") or ""
    for name in focus:
        theirs = sorted((m for m in every if _involves(m, name, primary)),
                        key=lambda m: m["time"], reverse=True)[:per_person]
        memories += [m for m in theirs if m not in memories]
    memories.sort(key=lambda m: m["time"], reverse=True)
    if included is not None:
        included.extend(m["memory"] for m in memories)
    if not memories:
        return ""
    return ("What you remember from earlier conversations, newest first (treat these as "
            "established facts — don't second-guess them):\n"
            + "\n".join(memory_line(m, now) for m in memories))


def build_redact_prompt(memory, name):
    return (f"Rewrite this memory so it contains nothing about {name}: no name, and no "
            f"facts, plans or feelings of theirs. Keep whatever is about other people. "
            f"If nothing meaningful remains, reply exactly DELETE. Reply with the "
            f"rewritten sentence only.\n\nMemory: {memory}")


def forget_person(embedder, name, profile, generate, extra_names=(), robot_name="Ember"):
    """Forget someone everywhere: memories only about them are deleted; shared
    memories are rewritten without them (via Gemini) and unlinked — or deleted
    if the rewrite still names them. Memories not linked to them are left
    alone (older, unlinked memories that name them are rewritten). If Gemini
    can't be reached, raises after applying what was done, so it can be
    retried. Returns {"deleted": n, "rewritten": n}."""
    collection = getattr(embedder, "_collection", None)
    if collection is None:
        return {"deleted": 0, "rewritten": 0}
    memories = _all_memories(embedder)
    # Everyone Ember knows of, including people only named in memories
    known = known_names(profile, list(extra_names) + sorted(
        {p for m in memories for p in m["people"]}))
    canon = canonical_name(name, known, robot_name, strict=True)
    if not canon:
        return {"deleted": 0, "rewritten": 0}
    pattern = name_pattern(canon, known)
    primary = profile.get("user_name") or ""
    deleted, rewritten = [], 0
    try:
        for m in memories:
            linked = _involves(m, canon, primary)
            # Newer memories list everyone they involve, so an unlinked one that
            # contains the word ("in May", "she will") isn't about them. Older
            # memories have no list: there the name in the text is all we have.
            if not linked and not (m["legacy"] and pattern.search(m["memory"])):
                continue
            others = [p for p in m["people"] if p.lower() != canon.lower()]
            if m["legacy"] and not m["people"] and primary and primary.lower() != canon.lower():
                others = [primary]   # older memories were implicitly about the primary user
            if linked and not others:
                deleted.append(m["id"])
                continue
            text = " ".join(str(generate(build_redact_prompt(m["memory"], canon)) or "").split())
            nothing_left = text.upper().strip(" .\"'") == "DELETE"
            if nothing_left or not text or pattern.search(text):
                # Linked to them: when in doubt, delete. Only a word match
                # ("in May", another Will): keep it unless nothing is left.
                if linked or nothing_left:
                    deleted.append(m["id"])
                continue
            embedder.save([text], ids=[m["id"]],
                          metadatas=[_metadata(others, m["time"], kind=m["kind"])])
            rewritten += 1
    finally:
        if deleted:
            collection.delete(ids=deleted)
    return {"deleted": len(deleted), "rewritten": rewritten}


# ─── Appearance ──────────────────────────────────────────────────────────────
def build_appearance_prompt(previous):
    base = ("This is a photo of a person from a companion robot's camera. Describe "
            "what a friend would notice, in two parts:\n"
            "- \"look\": their usual look — hair (length, colour, style), glasses, facial "
            "hair — in at most 20 words.\n"
            "- \"today\": what they're wearing or have on today that a friend might "
            "kindly compliment (e.g. \"a bright red cardigan\", \"a pearl necklace\"), in "
            "at most 15 words, or \"\" if nothing stands out.\n"
            "Never mention body shape, weight, skin, age, health or attractiveness.")
    if previous:
        base += ("\n\nTheir look last time: \"" + previous + "\". Has their look clearly "
                 "changed in a way a friend would notice and kindly mention — e.g. a "
                 "haircut or new colour, new glasses, a shaved or new beard? Ignore "
                 "lighting, camera angle and clothing. Only say changed if you're confident.")
    return base + ("\n\nReply with JSON only: {\"look\": \"...\", \"today\": \"...\", "
                   "\"changed\": true|false, \"change\": \"<short, e.g. 'shorter hair'> or empty\"}")


def check_appearance(generate, image_jpeg, previous=None):
    """→ {"description" (usual look), "today" (outfit), "changed", "change"},
    or None if it couldn't be judged."""
    try:
        data = _json_from_text(generate(build_appearance_prompt(previous), image_jpeg))
    except Exception as e:
        print(f"[APPEARANCE] Check failed: {e}")
        return None
    look = " ".join(str(data.get("look") or data.get("description") or "").split())[:200]
    if not look:
        return None
    today = " ".join(str(data.get("today") or "").split())[:120]
    change = " ".join(str(data.get("change") or "").split())[:100]
    changed = bool(previous) and data.get("changed") is True and bool(change)
    return {"description": look, "today": today, "changed": changed,
            "change": change if changed else ""}


# ─── Starting a conversation ────────────────────────────────────────────────
def describe_stay(seconds):
    if seconds is None:
        return ""
    mins = seconds / 60
    if mins < 10:
        return "just arrived"
    if mins < 60:
        return "here for a while"
    hours = int(mins // 60)
    return f"here for about {hours} hour{'s' if hours != 1 else ''}"


def initiation_prompt(people, memories, unknown=0, robot_name="Ember"):
    """The [INITIATE CONVERSATION] message, addressed to who's actually there.

    people: [{"name", "label", "here_for", "appearance": {"changed", "change",
    "today"} or None}]; memories: [{"memory", "people", "time"}] involving them."""
    if people:
        def with_stay(p):
            stay = describe_stay(p.get("here_for"))
            if not stay:
                return p["label"]
            if p["label"].endswith(")"):                 # "Sarah (Margaret's daughter, just arrived)"
                return p["label"][:-1] + f", {stay})"
            return f"{p['label']} ({stay})"
        who = [with_stay(p) for p in people]
        if unknown:
            who.append("someone you don't recognize" if unknown == 1
                       else f"{unknown} people you don't recognize")
        names = [p["name"] for p in people]
        whom = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        head = (f"[INITIATE CONVERSATION] With you now: {'; '.join(who)}. It's been quiet "
                f"for a while and you haven't spoken in some time. Gently start a "
                f"conversation with {whom} — warm, brief and natural, using their name.")
    else:
        head = ("[INITIATE CONVERSATION] " + ("Someone you don't recognize is" if unknown
                else "Someone is") + " here, it's been quiet for a while and you haven't "
                "spoken in some time. Gently start a conversation — warm, brief and natural.")
    looks = []
    for p in people:
        a = p.get("appearance") or {}
        if a.get("changed"):
            looks.append(f"{p['name']}'s look has changed since last time: {a['change']}")
        if a.get("today"):
            looks.append(f"today {p['name']} has on {a['today']}")
    ideas = ["notice something about how they look today" + (f" ({'; '.join(looks)})" if looks else ""),
             "follow up on something you remember", "or ask a light question about their day"]
    if not looks:
        ideas = ideas[1:]
    lines = [head, "Some ideas — pick ONE that fits: " + ", ".join(ideas) + "."]
    if memories:
        lines.append("Things you remember (use only if it feels natural):\n"
                     + "\n".join(memory_line(m) for m in memories))
    if people and unknown:
        lines.append("Someone you don't recognize is also here, so don't bring up anything "
                     "private — health, family matters, money, worries.")
    elif len(people) > 1:
        lines.append("Talk to them as the people they are — and keep private things "
                     "between you and the person they're about.")
    lines.append("Never comment on weight, body, skin or age. Don't mention that you "
                 "were prompted.")
    return "\n".join(lines)


# ─── Arrival greeting ────────────────────────────────────────────────────────
def describe_away(seconds, last_seen_ts=None, now_ts=None):
    if seconds is None:
        return None
    now_ts = now_ts or time.time()
    if seconds < 3600:
        return f"about {max(1, int(seconds // 60))} minutes ago"
    if seconds < 20 * 3600:
        hours = int(seconds // 3600)
        return f"about {hours} hour{'s' if hours != 1 else ''} ago"
    if last_seen_ts:
        then = dt.datetime.fromtimestamp(last_seen_ts)
        days = (dt.datetime.fromtimestamp(now_ts).date() - then.date()).days
        if days <= 0:
            return "earlier today"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days} days ago ({then.strftime('%A')})"
        return f"about {days} days ago"
    days = max(1, int(round(seconds / 86400)))
    return "about a day ago" if days == 1 else f"about {days} days ago"


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
        lines = [memory_line(m, about=name) if isinstance(m, dict) else f"- {m}"
                 for m in memories]
        parts.append(f"Things you remember that involve {name}, newest first:\n"
                     + "\n".join(lines))
    if appearance and appearance.get("changed"):
        parts.append(f"{name} looks a little different from last time: {appearance['change']}.")
    if appearance and appearance.get("today"):
        parts.append(f"Today {name} has on: {appearance['today']}.")
    ask = (f"Greet {name} warmly by name" if not is_primary
           else f"Welcome {name} back warmly by name")
    guidance = [
        f"{ask} — keep it short and natural.",
        "If something from last time is worth following up (a plan, an event, a worry), "
        "ask about one of them the way a friend would — don't list what you remember.",
    ]
    if appearance and (appearance.get("changed") or appearance.get("today")):
        guidance.append("You may also mention how they look kindly as a conversation starter "
                        "(e.g. \"Did you get a haircut? It looks nice!\" or \"What a lovely "
                        "sweater!\"). Never comment on weight, body, skin or age.")
    guidance.append("If they're busy or talking with someone else, a quick hello is enough.")
    return "\n".join(parts) + "\n" + " ".join(guidance)
