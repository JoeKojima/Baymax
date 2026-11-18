"""Bounded, local topic cues. No models, hardware, network, or disk at import."""
import json
import re
import unicodedata

WORDS = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
SEEDS = {
    "family": "family", "daughter": "family", "son": "family",
    "grandchild": "family", "grandchildren": "family", "friend": "friends",
    "friends": "friends", "visit": "visits", "visits": "visits",
    "visiting": "visits", "appointment": "appointments",
    "appointments": "appointments", "birthday": "birthdays",
    "birthdays": "birthdays", "garden": "gardening", "gardening": "gardening",
    "music": "music", "book": "reading", "books": "reading",
    "reading": "reading", "pet": "pets", "pets": "pets", "dog": "pets",
    "cat": "pets", "travel": "travel", "holiday": "travel",
    "food": "food", "cooking": "food", "painting": "painting",
    "sister": "family", "brother": "family", "husband": "family", "wife": "family",
    "granddaughter": "family", "grandson": "family", "hobby": "hobbies",
    "knitting": "knitting", "sewing": "sewing", "doctor": "appointments",
    "clinic": "appointments", "checkup": "appointments",
}


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return [m.group().removesuffix("'s") for m in WORDS.finditer(text)]


def topic_vocabulary(aliases=None):
    return frozenset(SEEDS.values()) | frozenset(
        value.strip().casefold() for value in (aliases or {}).values() if value.strip())


def parse_summaries(text, aliases=None):
    """Validate the structured response; unknown tags never discard a fact."""
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError("memory summary must be a JSON array")
    allowed = topic_vocabulary(aliases)
    summaries = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("summary"), str):
            raise ValueError("memory summary must contain summary text")
        summary = record["summary"].strip()
        tags = record.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = sorted({tag.strip().casefold() for tag in tags
                       if isinstance(tag, str)} & allowed)
        if summary:
            summaries.append((summary, tags))
    return summaries


class TopicMatcher:
    """Emit complete seeded/custom topics once per transcript utterance."""

    def __init__(self, aliases=None, *, debounce_seconds=0.2):
        self.debounce_seconds = debounce_seconds
        self._aliases = {**{topic: topic for topic in SEEDS.values()}, **SEEDS}
        self._phrases = {}
        for alias, canonical in (aliases or {}).items():
            parts = tokens(alias)
            if not parts or not isinstance(canonical, str):
                continue
            if len(parts) == 1:
                self._aliases[parts[0]] = canonical.strip().casefold()
            else:
                self._phrases[tuple(parts)] = canonical.strip().casefold()
        self.reset()

    def reset(self):
        self.text = ""
        self.last_change = 0.0
        self.seen = set()

    def feed(self, fragment: str, now: float) -> tuple[str, ...]:
        if fragment:
            self.text = (self.text + fragment)[-4000:]
            self.last_change = now
        return self.poll(now)

    def poll(self, now: float) -> tuple[str, ...]:
        text = self.text
        matches = list(WORDS.finditer(text))
        if (matches and matches[-1].end() == len(text)
                and now - self.last_change < self.debounce_seconds):
            text = text[:matches[-1].start()]
        words = tokens(text)
        found = set()
        for word in words:
            if word in self._aliases:
                found.add(self._aliases[word])
        for phrase, canonical in self._phrases.items():
            n = len(phrase)
            if any(tuple(words[i:i+n]) == phrase for i in range(len(words)-n+1)):
                found.add(canonical)
        new = found - self.seen
        self.seen.update(found)
        return tuple(sorted(new - {""}))
