"""Public RSS headlines with local, explainable preference filtering."""
import html
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .preferences import CATEGORIES, profile_name, terms
from .transport import utc_now

SOURCES = {
    "world": ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    "technology": ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    "business": ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    "markets": ("CNBC Markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    "science": ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    "health": ("BBC Health", "https://feeds.bbci.co.uk/news/health/rss.xml"),
    "sports": ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    "entertainment": ("BBC Entertainment", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    "space": ("NASA", "https://www.nasa.gov/news-release/feed/"),
}
ALIASES = {"ai": ("ai", "artificial intelligence"),
           "semiconductors": ("semiconductor", "semiconductors", "chip", "chips", "nvidia"),
           "crypto": ("crypto", "cryptocurrency", "bitcoin", "ethereum")}


def text(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())[:350]


def matches(term, value):
    return any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", value, re.I)
               for word in ALIASES.get(term, (term,)))


def parse_feed(raw, category):
    decoded = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in decoded.upper() or "<!ENTITY" in decoded.upper():
        raise ValueError("Unsupported XML declaration")
    try:
        root = ET.fromstring(decoded)
    except ET.ParseError as exc:
        raise ValueError("Malformed RSS response") from exc
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Expected RSS channel")
    result = []
    for item in root.findall("./channel/item")[:80]:
        title = text(item.findtext("title"))
        parts = urlsplit((item.findtext("link") or "").strip())
        if not title or parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                           if not k.casefold().startswith("utm_") and k.casefold() not in ("ref", "fbclid", "gclid")])
        result.append({"title": title, "url": urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")),
                       "source": SOURCES[category][0], "category": category,
                       "published_at": published.astimezone(timezone.utc).isoformat()})
    return result


class Headlines:
    def __init__(self, client, preferences, now=lambda: datetime.now(timezone.utc)):
        self.client, self.preferences, self.now = client, preferences, now

    def get(self, profile=None, categories=None, limit=5, hours=48):
        if type(limit) is not int or not 1 <= limit <= 10 or type(hours) is not int or not 1 <= hours <= 168:
            raise ValueError("Use limit 1-10 and hours 1-168")
        if profile is not None:
            profile_name(profile)
        prefs = self.preferences.get(profile) if profile is not None else {"categories": [], "interests": [], "avoid": [], "strict": False}
        if profile is not None and not prefs["saved"]:
            return {"error": "No saved preferences for this profile. Ask what they want to follow before saving a personal feed.",
                    "profile": profile, "needs_preferences": True, "headlines": []}
        selected = terms(categories) if categories is not None else prefs["categories"] or ["world", "technology", "business", "markets"]
        if not selected or any(x not in CATEGORIES for x in selected):
            raise ValueError("Choose supported categories using list_public_capabilities")

        def fetch(category):
            try:
                data = self.client.get(SOURCES[category][1], xml=True)
                return category, parse_feed(data["data"], category), data["fetched_at"], None
            except Exception:
                return category, [], None, "Source temporarily unavailable"

        items, failed, fetched = [], [], {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for category, articles, stamp, error in pool.map(fetch, selected):
                if error:
                    failed.append({"category": category, "source": SOURCES[category][0], "error": error})
                else:
                    fetched[category] = stamp
                items.extend(articles)
        ranked, seen = [], set()
        now = self.now()
        for article in items:
            age = (now - datetime.fromisoformat(article["published_at"])).total_seconds()
            if not 0 <= age <= hours * 3600:
                continue
            title = article["title"]
            if any(matches(x, title) for x in prefs["avoid"]):
                continue
            hits = [x for x in prefs["interests"] if matches(x, title)]
            if prefs["strict"] and prefs["interests"] and not hits:
                continue
            normalized = re.sub(r"\W+", " ", title.casefold()).strip()
            if article["url"] in seen or normalized in seen:
                continue
            seen.update((article["url"], normalized))
            ranked.append({**article, "matched_interests": hits,
                           "selection_reason": "Interest match" if hits else "Selected category"})
        ranked.sort(key=lambda x: (len(x["matched_interests"]), x["published_at"]), reverse=True)
        return {"profile": profile, "headlines": ranked[:limit], "requested_categories": selected,
                "source_fetched_at": fetched, "unavailable_sources": failed, "checked_at": utc_now(),
                "partial": bool(failed), "error": "All requested sources are unavailable" if len(failed) == len(selected) else None,
                "notice": "Headlines only; not full-article analysis or live market prices. No matches means no matching recent headlines in these feeds."}
