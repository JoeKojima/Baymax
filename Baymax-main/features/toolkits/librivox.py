"""LibriVox search and chapter download through the Internet Archive.

Search/ranking/metadata logic adapted from the audiobook PR (JoeKojima/Baymax#2,
audiobook.py), which was verified live against archive.org.
"""
import difflib
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.parse

import requests

HTTP_TIMEOUT = 20
SEARCH_LANGUAGE = "eng"
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_METADATA_URL = "https://archive.org/metadata/{}"
IA_DOWNLOAD_URL = "https://archive.org/download/{}/{}"
MP3_FORMATS = ["64Kbps MP3", "VBR MP3", "128Kbps MP3"]
CACHE = Path(tempfile.gettempdir()) / "ember-audiobooks"
_STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "by", "book", "story"}


def _clean_query(text):
    """Strip characters that are special in the archive.org Lucene syntax."""
    return re.sub(r"[^\w\s]", " ", text or "").strip()


def _lucene_terms(text):
    """'alice in wonderland' -> 'alice* AND wonderland*' so possessives still match."""
    words = [w for w in _clean_query(text).lower().split() if w not in _STOPWORDS]
    return " AND ".join(f"{w}*" if len(w) > 2 else w for w in words)


def _normalise_title(title):
    title = re.sub(r"\(.*?\)", "", title.lower())
    title = re.sub(r"^(the|a|an)\s+", "", title.strip())
    return re.sub(r"[^\w\s]", "", title).strip()


def _parse_length(value):
    """'65:06', '1:05:06' or '1520.5' -> seconds."""
    try:
        if ":" in str(value):
            seconds = 0.0
            for part in str(value).split(":"):
                seconds = seconds * 60 + float(part)
            return seconds
        return float(value) if value else 0.0
    except ValueError:
        return 0.0


def _ia_search(query, rows):
    params = {"q": query, "fl[]": ["identifier", "title", "creator", "downloads"],
              "sort[]": "downloads desc", "rows": rows, "output": "json"}
    resp = requests.get(IA_SEARCH_URL, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    results = []
    for doc in resp.json().get("response", {}).get("docs", []):
        creator = doc.get("creator", "")
        if isinstance(creator, list):
            creator = ", ".join(creator)
        results.append({"id": doc["identifier"], "title": doc.get("title", doc["identifier"]),
                        "author": creator, "downloads": doc.get("downloads", 0)})
    return results


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOPWORDS}


def _covers_query(result, title, author):
    """Reject loose matches (e.g. 'harry potter' -> an author named Potter)."""
    wanted = _words(title) | _words(author)
    if not wanted:
        return True
    have = _words(result["title"]) | _words(result["author"])
    found = sum(1 for w in wanted if any(h.startswith(w) or w.startswith(h) for h in have))
    return found / len(wanted) >= 0.75


def _rank(results, title):
    """Title similarity plus popularity; dramatic/multi-cast versions rank lower."""
    wanted = _normalise_title(title) if title else ""

    def score(r):
        name = r["title"].lower()
        similarity = difflib.SequenceMatcher(None, wanted, _normalise_title(name)).ratio() if wanted else 0.0
        penalty = 0.5 if ("drama" in name and "drama" not in wanted) else 0.0
        return similarity + 0.15 * math.log10(r["downloads"] + 1) - penalty

    return sorted(results, key=score, reverse=True)


def search(title=None, author=None, limit=5):
    """LibriVox recordings matching a title and/or author, best first."""
    t, a = _lucene_terms(title), _lucene_terms(author)
    if not t and not a:
        return []
    base = f"collection:librivoxaudio AND language:{SEARCH_LANGUAGE}"
    queries = []
    if t and a:
        queries.append(f"{base} AND title:({t}) AND creator:({a})")
    if t:
        queries += [f"{base} AND title:({t})", f"{base} AND ({t})"]
    if a:
        queries.append(f"{base} AND creator:({a})")
    for query in queries:
        results = [r for r in _ia_search(query, rows=max(limit * 3, 15)) if _covers_query(r, title, author)]
        if results:
            return _rank(results, title)[:limit]
    return []


def fetch_book(identifier):
    """A LibriVox item's title, author and ordered MP3 chapters."""
    resp = requests.get(IA_METADATA_URL.format(identifier), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    meta, files = data.get("metadata", {}), data.get("files", [])
    sections = []
    for fmt in MP3_FORMATS:
        sections = [f for f in files if f.get("format") == fmt]
        if sections:
            break
    if not sections:
        raise ValueError(f"No MP3 files found for {identifier}")

    def order(f):
        track = re.match(r"\d+", str(f.get("track", "")))
        return (int(track.group()) if track else 0, f["name"])

    sections.sort(key=order)
    creator = meta.get("creator", "")
    if isinstance(creator, list):
        creator = ", ".join(creator)
    return {"id": identifier, "title": meta.get("title", identifier), "author": creator,
            "sections": [{"title": f.get("title") or os.path.splitext(f["name"])[0], "file": f["name"],
                          "length": _parse_length(f.get("length"))} for f in sections]}


def download(book, index, cancelled=lambda: False):
    """Download one chapter to the local cache (once) and return its path."""
    name = book["sections"][index]["file"]
    folder = CACHE / re.sub(r"[^\w.-]", "_", book["id"])
    path = folder / re.sub(r"[^\w.-]", "_", name)
    if path.exists():
        return path
    folder.mkdir(parents=True, exist_ok=True)
    url = IA_DOWNLOAD_URL.format(book["id"], urllib.parse.quote(name))
    partial = path.with_suffix(path.suffix + ".part")
    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        with open(partial, "wb") as out:
            for block in resp.iter_content(chunk_size=1 << 16):
                if cancelled():
                    raise InterruptedError("Download cancelled")
                out.write(block)
    os.replace(partial, path)
    return path
