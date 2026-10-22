"""Bounded, local topic cues. No models, hardware, network, or disk at import."""
from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict

WORDS = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
STOP_WORDS = frozenset("""
a an the and or but if then as at by for from in into of on to with without
i me my mine we us our you your yours he him his she her hers it its they them
their this that these those who what where when why how which someone something
am is are was were be been being do does did have has had can could will would
shall should may might must not no yes so very just really quite also still
about again all any both each few more most much many only other same some such
than too up down out over under here there now today yesterday tomorrow
wasn't isn't aren't don't doesn't didn't can't couldn't won't wouldn't
hello hi hey please thanks thank tell saying said say wonder wondering know
think thinking want wanted remember remembered feel feeling like likes enjoys
enjoy loves love talk talking asked ask worth important user patient person
""".split())

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


class TopicMatcher:
    """Recognize seeded/custom terms and words from already-read memories.

    ``feed`` receives transcription DELTAS, preserving their spacing. A token
    at the buffer tail remains provisional until a following boundary or a
    quiet debounce. Cues are emitted once per utterance, not once per fragment.
    """

    def __init__(self, aliases=None, *, debounce_seconds=0.2, max_terms=4096):
        self.debounce_seconds = debounce_seconds
        self.max_terms = max_terms
        self._learned = OrderedDict()
        self._aliases = dict(SEEDS)
        self._phrases = {}
        for alias, canonical in (aliases or {}).items():
            parts = tokens(alias)
            if not parts or all(p in STOP_WORDS for p in parts) or not isinstance(canonical, str):
                continue
            if len(parts) == 1:
                self._aliases[parts[0]] = canonical.casefold()
            else:
                self._phrases[tuple(parts)] = canonical.casefold()
        self.reset()

    def reset(self):
        self.text = ""
        self.last_change = 0.0
        self.seen = set()

    def learn(self, documents):
        for document in documents:
            if not isinstance(document, str):
                continue
            for word in tokens(document):
                if not 3 <= len(word) <= 64 or word in STOP_WORDS or word in self._aliases:
                    continue
                self._learned[word] = None
                self._learned.move_to_end(word)
                while len(self._learned) > self.max_terms:
                    self._learned.popitem(last=False)

    @property
    def learned_count(self):
        return len(self._learned)

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
            elif word in self._learned:
                found.add(word)
        for phrase, canonical in self._phrases.items():
            n = len(phrase)
            if any(tuple(words[i:i+n]) == phrase for i in range(len(words)-n+1)):
                found.add(canonical)
        new = found - self.seen
        self.seen.update(found)
        return tuple(sorted(new))


def prediction_context(current: str, messages) -> str:
    """Keep negation/current speech, dropping oldest messages before truncation."""
    preceding = [(str(role), str(text)) for role, text in list(messages)[-5:]]
    current_line = "patient: " + current[-1991:]
    while preceding:
        combined = "\n".join(f"{r}: {t}" for r, t in preceding) + "\n" + current_line
        if len(combined) <= 2000:
            return combined
        preceding.pop(0)
    return current_line[:2000]
