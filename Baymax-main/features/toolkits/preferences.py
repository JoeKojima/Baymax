"""Explicit, locally stored named feed preferences; no speaker inference."""
import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path

CATEGORIES = ("world", "technology", "business", "markets", "science", "health",
              "sports", "entertainment", "space")


def profile_name(profile):
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", profile):
        raise ValueError("Use an explicit profile ID: 1-40 lowercase letters, digits, _ or -")
    return profile


def terms(value):
    if (not isinstance(value, list) or len(value) > 20 or
            any(not isinstance(x, str) or not x.strip() or len(x) > 60 for x in value)):
        raise ValueError("Use at most 20 nonempty terms of up to 60 characters")
    return list(dict.fromkeys(" ".join(x.casefold().split()) for x in value))


class Preferences:
    def __init__(self, path=None):
        root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[2]))
        self.path = Path(path) if path else root / "toolkit_feed_preferences.json"
        self.lock = threading.RLock()

    def _load(self):
        try:
            if self.path.stat().st_size > 200_000:
                raise ValueError("Preferences file is too large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or len(data) > 32:
            raise ValueError("Preferences file is invalid; repair it before updating")
        for name, value in data.items():
            profile_name(name)
            data[name] = self._validate(value)
        return data

    @staticmethod
    def _validate(value):
        if not isinstance(value, dict):
            raise ValueError("Invalid feed preferences")
        categories = terms(value.get("categories", []))
        if any(x not in CATEGORIES for x in categories):
            raise ValueError("Unsupported category; use list_public_capabilities")
        if not isinstance(value.get("strict", False), bool):
            raise ValueError("strict must be true or false")
        return {"categories": categories, "interests": terms(value.get("interests", [])),
                "avoid": terms(value.get("avoid", [])), "strict": value.get("strict", False)}

    def get(self, profile):
        profile_name(profile)
        with self.lock:
            data = self._load()
            return {"profile": profile, "saved": profile in data,
                    **copy.deepcopy(data.get(profile, self._validate({})))}

    def update(self, profile, categories=None, interests=None, avoid=None, strict=None, forget=False):
        profile_name(profile)
        if not isinstance(forget, bool):
            raise ValueError("forget must be true or false")
        if forget and any(x is not None for x in (categories, interests, avoid, strict)):
            raise ValueError("Forgetting cannot be combined with an update")
        with self.lock:
            data = self._load()
            if forget:
                data.pop(profile, None)
            else:
                value = dict(data.get(profile, self._validate({})))
                for k, v in (("categories", categories), ("interests", interests), ("avoid", avoid), ("strict", strict)):
                    if v is not None:
                        value[k] = v
                data[profile] = self._validate(value)
                if len(data) > 32:
                    raise ValueError("At most 32 feed profiles are supported")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".toolkit-prefs-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return {"profile": profile, "forgotten": forget, "saved": not forget,
                    **copy.deepcopy(data.get(profile, self._validate({})))}
