"""
Audiobook player — reads free public-domain LibriVox audiobooks aloud.

Gemini Live triggers it through function calling (see AUDIOBOOK_TOOLS); the
actual audio never goes through Gemini.  Books are found with the Internet
Archive search API (LibriVox hosts every recording in the `librivoxaudio`
collection), downloaded one section at a time into `audiobook_cache/`, decoded
with miniaudio and played on a dedicated output stream in a background thread,
so playback survives Gemini reconnects.

- Playback pauses automatically while the assistant is speaking.
- Positions are saved to `audiobook_bookmarks.json` so "keep reading" resumes
  where the user left off.

Usage:
    player = AudiobookPlayer(is_assistant_speaking=lambda: ...)
    result = player.handle_tool_call("play_audiobook", {"title": "Dracula"})
"""
import difflib
import json
import math
import os
import re
import shutil
import threading
import time
import urllib.parse

import miniaudio
import numpy as np
import requests
import sounddevice as sd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "audiobook_cache")
BOOKMARKS_PATH = os.path.join(SCRIPT_DIR, "audiobook_bookmarks.json")

# ─── Config ──────────────────────────────────────────────────────────────────
SAMPLE_RATE = 48000           # matches the Gemini output stream
CHUNK_FRAMES = 4800           # 0.1 s per write — keeps pause/stop responsive
VOLUME = 0.6                  # lower volume = less book audio leaking into the mic
RESUME_REWIND_SECONDS = 3.0   # replay a little context when resuming
BOOKMARK_SAVE_INTERVAL = 15.0
CACHE_MAX_BOOKS = 3           # keep downloaded sections for this many books
SEARCH_LANGUAGE = "eng"
HTTP_TIMEOUT = 20

IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_METADATA_URL = "https://archive.org/metadata/{}"
IA_DOWNLOAD_URL = "https://archive.org/download/{}/{}"
MP3_FORMATS = ["64Kbps MP3", "VBR MP3", "128Kbps MP3"]

# ─── Gemini function declarations ───────────────────────────────────────────
AUDIOBOOK_TOOLS = [
    {
        "name": "search_audiobooks",
        "description": (
            "Search free public-domain LibriVox audiobooks (mostly classics "
            "published before ~1930). Also returns books the user is partway "
            "through."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Book title or title keywords"},
                "author": {"type": "STRING", "description": "Author name, if known"},
            },
        },
    },
    {
        "name": "play_audiobook",
        "description": (
            "Start reading an audiobook aloud. Resumes from the saved position "
            "if the user has started this book before."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Book title"},
                "author": {"type": "STRING", "description": "Author name, if known"},
                "from_beginning": {
                    "type": "BOOLEAN",
                    "description": "Restart from the first section instead of resuming",
                },
            },
            "required": ["title"],
        },
    },
    {"name": "pause_audiobook", "description": "Pause the audiobook that is playing."},
    {"name": "resume_audiobook", "description": "Resume the paused audiobook."},
    {"name": "stop_audiobook", "description": "Stop the audiobook (position is saved)."},
    {"name": "next_audiobook_section", "description": "Skip to the next chapter/section."},
    {"name": "previous_audiobook_section", "description": "Go back to the previous chapter/section."},
    {"name": "audiobook_status", "description": "What book is playing and where we are in it."},
]


# ─── LibriVox / Internet Archive helpers ─────────────────────────────────────
_STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "by", "book", "story"}


def _clean_query(text):
    """Strip characters that are special in the archive.org Lucene syntax."""
    return re.sub(r"[^\w\s]", " ", text or "").strip()


def _lucene_terms(text):
    """'alice in wonderland' -> 'alice* AND wonderland*' so possessives and
    plurals ("Alice's") still match."""
    words = [w for w in _clean_query(text).lower().split() if w not in _STOPWORDS]
    return " AND ".join(f"{w}*" if len(w) > 2 else w for w in words)


def _normalise_title(title):
    title = title.lower()
    title = re.sub(r"\(.*?\)", "", title)               # "(version 2)", "(abridged)"
    title = re.sub(r"^(the|a|an)\s+", "", title.strip())
    return re.sub(r"[^\w\s]", "", title).strip()


def _parse_length(value):
    """'65:06', '1:05:06' or '1520.5' -> seconds."""
    if not value:
        return 0.0
    try:
        if ":" in str(value):
            seconds = 0.0
            for part in str(value).split(":"):
                seconds = seconds * 60 + float(part)
            return seconds
        return float(value)
    except ValueError:
        return 0.0


def _ia_search(query, rows):
    params = {
        "q": query,
        "fl[]": ["identifier", "title", "creator", "downloads"],
        "sort[]": "downloads desc",
        "rows": rows,
        "output": "json",
    }
    resp = requests.get(IA_SEARCH_URL, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    docs = resp.json().get("response", {}).get("docs", [])
    results = []
    for doc in docs:
        creator = doc.get("creator", "")
        if isinstance(creator, list):
            creator = ", ".join(creator)
        results.append({
            "id": doc["identifier"],
            "title": doc.get("title", doc["identifier"]),
            "author": creator,
            "downloads": doc.get("downloads", 0),
        })
    return results


def search_librivox(title=None, author=None, limit=5):
    """Search LibriVox recordings, most-downloaded first."""
    t, a = _lucene_terms(title), _lucene_terms(author)
    if not t and not a:
        return []
    base = f"collection:librivoxaudio AND language:{SEARCH_LANGUAGE}"
    queries = []
    if t and a:
        queries.append(f"{base} AND title:({t}) AND creator:({a})")
    if t:
        queries.append(f"{base} AND title:({t})")
        # Covers "read me some Sherlock Holmes" style requests
        queries.append(f"{base} AND ({t})")
    if a:
        queries.append(f"{base} AND creator:({a})")
    for query in queries:
        results = _ia_search(query, rows=max(limit * 3, 15))
        results = [r for r in results if _covers_query(r, title, author)]
        if results:
            return _rank(results, title)[:limit]
    return []


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOPWORDS}


def _covers_query(result, title, author):
    """Reject loose matches (e.g. 'harry potter' -> an author named Potter)."""
    wanted = _words(title) | _words(author)
    if not wanted:
        return True
    have = _words(result["title"]) | _words(result["author"])
    # Allow simple plurals/possessives: "alice" matches "alices"
    found = sum(1 for w in wanted if any(h.startswith(w) or w.startswith(h) for h in have))
    return found / len(wanted) >= 0.75


def _rank(results, title):
    """Balance title similarity against popularity (a proxy for a good, complete
    solo reading); dramatic/multi-cast versions rank lower."""
    wanted = _normalise_title(title) if title else ""

    def score(r):
        name = r["title"].lower()
        similarity = (difflib.SequenceMatcher(None, wanted, _normalise_title(name)).ratio()
                      if wanted else 0.0)
        popularity = 0.15 * math.log10(r["downloads"] + 1)
        penalty = 0.5 if ("drama" in name and "drama" not in wanted) else 0.0
        return similarity + popularity - penalty

    return sorted(results, key=score, reverse=True)


def fetch_book(identifier):
    """Fetch a LibriVox item's metadata and its ordered list of MP3 sections."""
    resp = requests.get(IA_METADATA_URL.format(identifier), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    meta = data.get("metadata", {})
    files = data.get("files", [])

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
    return {
        "id": identifier,
        "title": meta.get("title", identifier),
        "author": creator,
        "sections": [
            {
                "title": f.get("title") or os.path.splitext(f["name"])[0],
                "file": f["name"],
                "length": _parse_length(f.get("length")),
            }
            for f in sections
        ],
    }


# ─── Player ──────────────────────────────────────────────────────────────────
class AudiobookPlayer:
    """Thread-safe audiobook player.  States: idle, loading, playing, paused."""

    def __init__(self, is_assistant_speaking=lambda: False):
        self._is_assistant_speaking = is_assistant_speaking
        self._cond = threading.Condition()
        self._state = "idle"
        self._book = None
        self._section = 0
        self._position = 0.0
        self._generation = 0          # bumped on every seek/track change
        self._last_error = None
        self._bookmarks = self._load_bookmarks()
        self._bookmarks_lock = threading.Lock()
        self._download_locks = {}
        self._download_locks_lock = threading.Lock()
        self._stream = None
        self._thread = None

    # ── Tool dispatch ────────────────────────────────────────────────────────
    def handle_tool_call(self, name, args):
        """Run a Gemini function call.  Blocking (network) — call from a thread."""
        args = dict(args or {})
        handlers = {
            "search_audiobooks": lambda: self.search(args.get("title"), args.get("author")),
            "play_audiobook": lambda: self.play(
                args.get("title"), args.get("author"), bool(args.get("from_beginning"))
            ),
            "pause_audiobook": self.pause,
            "resume_audiobook": self.resume,
            "stop_audiobook": self.stop,
            "next_audiobook_section": lambda: self.skip(1),
            "previous_audiobook_section": lambda: self.skip(-1),
            "audiobook_status": self.status,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return handler()
        except requests.RequestException as e:
            print(f"[AUDIOBOOK] Network error in {name}: {e}")
            return {"error": "Couldn't reach the LibriVox library right now."}
        except Exception as e:
            print(f"[AUDIOBOOK] {name} failed: {e}")
            return {"error": str(e)}

    # ── Commands ─────────────────────────────────────────────────────────────
    def search(self, title=None, author=None):
        results = search_librivox(title, author, limit=10) if (title or author) else []
        # Several recordings often share a title — list each book once
        unique = {}
        for r in results:
            unique.setdefault((_normalise_title(r["title"]), r["author"]), r)
        return {
            "results": [
                {"title": r["title"], "author": r["author"]} for r in list(unique.values())[:5]
            ],
            "in_progress": self._in_progress(),
        }

    def play(self, title, author=None, from_beginning=False):
        bookmark_id = None if from_beginning else self._find_bookmark(title)
        if bookmark_id:
            book = fetch_book(bookmark_id)
        else:
            results = search_librivox(title, author, limit=5)
            if not results:
                return {
                    "status": "not_found",
                    "message": f"No LibriVox recording found for '{title}'. "
                               "LibriVox only has public-domain books (mostly pre-1930).",
                }
            book = fetch_book(results[0]["id"])

        mark = self._bookmarks.get(book["id"]) if not from_beginning else None
        section = min(mark["section"], len(book["sections"]) - 1) if mark else 0
        position = max(0.0, mark["position"] - RESUME_REWIND_SECONDS) if mark else 0.0

        self._save_current_bookmark()
        with self._cond:
            self._book = book
            self._section = section
            self._position = position
            self._state = "loading"
            self._last_error = None
            self._generation += 1
            self._cond.notify_all()
        self._ensure_thread()
        self._prune_cache(keep=book["id"])
        print(f"[AUDIOBOOK] Playing '{book['title']}' section {section + 1} at {position:.0f}s")
        return {
            "status": "starting",
            "title": book["title"],
            "author": book["author"],
            "resumed": bool(mark),
            "section": section + 1,
            "section_title": book["sections"][section]["title"],
            "total_sections": len(book["sections"]),
            "note": "The first section may take a few seconds to download.",
        }

    def pause(self):
        with self._cond:
            if self._state not in ("loading", "playing"):
                return {"status": self._state, "message": "Nothing is playing."}
            self._state = "paused"
            self._generation += 1
            self._cond.notify_all()
        self._save_current_bookmark()
        return {"status": "paused", **self._where()}

    def resume(self):
        with self._cond:
            if self._book is None:
                return {"status": "idle", "message": "No audiobook to resume.",
                        "in_progress": self._in_progress()}
            if self._state == "paused":
                self._position = max(0.0, self._position - RESUME_REWIND_SECONDS)
                self._state = "loading"
                self._generation += 1
                self._cond.notify_all()
        return {"status": "resumed", **self._where()}

    def stop(self):
        self._save_current_bookmark()
        with self._cond:
            was = self._book["title"] if self._book else None
            self._book = None
            self._state = "idle"
            self._generation += 1
            self._cond.notify_all()
        return {"status": "stopped", "title": was, "message": "Position saved."}

    def skip(self, delta):
        with self._cond:
            if self._book is None:
                return {"status": "idle", "message": "No audiobook is playing."}
            new = self._section + delta
            if not 0 <= new < len(self._book["sections"]):
                return {"status": self._state, "message": "No more sections in that direction.",
                        **self._where()}
            self._section = new
            self._position = 0.0
            self._state = "loading"
            self._generation += 1
            self._cond.notify_all()
        self._save_current_bookmark()
        return {"status": "playing", **self._where()}

    def status(self):
        with self._cond:
            state = self._state
        result = {"status": state, "in_progress": self._in_progress()}
        if self._book:
            result.update(self._where())
        if self._last_error:
            result["error"] = self._last_error
        return result

    def is_playing(self):
        with self._cond:
            return self._state in ("loading", "playing")

    def context_note(self):
        """Describes the current state for Gemini after a reconnect, or None."""
        with self._cond:
            if self._book is None:
                return None
            state = self._state
        where = self._where()
        return (f"[AUDIOBOOK] '{where['title']}' by {where['author']} is {state} "
                f"(section {where['section']} of {where['total_sections']}: "
                f"{where['section_title']}).")

    def shutdown(self):
        """Save position and release the audio device (call on exit)."""
        self._save_current_bookmark()
        with self._cond:
            self._state = "idle"
            self._generation += 1
            self._cond.notify_all()

    # ── Worker thread ────────────────────────────────────────────────────────
    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def _worker(self):
        while True:
            with self._cond:
                while self._state not in ("loading", "playing"):
                    self._close_stream()
                    self._cond.wait()
                gen = self._generation
                book, section, position = self._book, self._section, self._position

            try:
                path = self._download(book, section)
            except Exception as e:
                print(f"[AUDIOBOOK] Download failed: {e}")
                with self._cond:
                    if gen == self._generation:
                        self._state = "paused"
                        self._last_error = "Couldn't download the next section."
                continue

            with self._cond:
                if gen != self._generation or self._state not in ("loading", "playing"):
                    continue
                self._state = "playing"

            if section + 1 < len(book["sections"]):
                threading.Thread(target=self._prefetch, args=(book, section + 1),
                                 daemon=True).start()

            finished = self._play_file(path, gen, position)

            if finished:
                with self._cond:
                    if gen != self._generation:
                        continue
                    if self._section + 1 < len(book["sections"]):
                        self._section += 1
                        self._position = 0.0
                        self._generation += 1
                    else:
                        print(f"[AUDIOBOOK] Finished '{book['title']}'")
                        self._book = None
                        self._state = "idle"
                        self._generation += 1
                        with self._bookmarks_lock:
                            self._bookmarks.pop(book["id"], None)
                if self._book is not None:
                    self._save_current_bookmark()
                else:
                    self._write_bookmarks()

    def _play_file(self, path, gen, start_seconds):
        """Stream one section to the speaker.  Returns True at end of file,
        False if interrupted (pause/stop/skip) or on an audio error."""
        try:
            stream = self._open_stream()
            decoder = miniaudio.stream_file(
                path,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=SAMPLE_RATE,
                frames_to_read=CHUNK_FRAMES,
                seek_frame=int(start_seconds * SAMPLE_RATE),
            )
            position = start_seconds
            last_save = time.monotonic()
            for chunk in decoder:
                # Hold the book while the assistant is talking
                while True:
                    with self._cond:
                        if gen != self._generation or self._state != "playing":
                            return False
                    if not self._is_assistant_speaking():
                        break
                    time.sleep(0.05)

                samples = np.frombuffer(chunk, dtype=np.int16)
                samples = (samples.astype(np.float32) * VOLUME).astype(np.int16)
                stream.write(samples.reshape(-1, 1))
                position += len(samples) / SAMPLE_RATE

                with self._cond:
                    if gen == self._generation:
                        self._position = position
                if time.monotonic() - last_save > BOOKMARK_SAVE_INTERVAL:
                    self._save_current_bookmark()
                    last_save = time.monotonic()
            return True
        except Exception as e:
            # e.g. PortAudio was reset by the main loop — reopen and retry
            print(f"[AUDIOBOOK] Playback error: {e}")
            self._close_stream()
            time.sleep(2)
            return False

    def _open_stream(self):
        if self._stream is None:
            self._stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16")
            self._stream.start()
        return self._stream

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ── Downloads ────────────────────────────────────────────────────────────
    def _download(self, book, section):
        file_name = book["sections"][section]["file"]
        book_dir = os.path.join(CACHE_DIR, book["id"])
        path = os.path.join(book_dir, file_name)
        with self._download_locks_lock:
            lock = self._download_locks.setdefault(path, threading.Lock())
        with lock:
            if os.path.exists(path):
                return path
            os.makedirs(book_dir, exist_ok=True)
            url = IA_DOWNLOAD_URL.format(book["id"], urllib.parse.quote(file_name))
            print(f"[AUDIOBOOK] Downloading {file_name}...")
            tmp = path + ".part"
            with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for block in resp.iter_content(chunk_size=1 << 16):
                        f.write(block)
            os.replace(tmp, path)
            return path

    def _prefetch(self, book, section):
        try:
            self._download(book, section)
        except Exception as e:
            print(f"[AUDIOBOOK] Prefetch failed (will retry when needed): {e}")

    def _prune_cache(self, keep):
        if not os.path.isdir(CACHE_DIR):
            return
        dirs = [d for d in os.listdir(CACHE_DIR) if d != keep
                and os.path.isdir(os.path.join(CACHE_DIR, d))]
        dirs.sort(key=lambda d: os.path.getmtime(os.path.join(CACHE_DIR, d)), reverse=True)
        for d in dirs[CACHE_MAX_BOOKS - 1:]:
            shutil.rmtree(os.path.join(CACHE_DIR, d), ignore_errors=True)

    # ── Bookmarks ────────────────────────────────────────────────────────────
    def _where(self):
        with self._cond:
            book, section, position = self._book, self._section, self._position
        if book is None:
            return {}
        return {
            "title": book["title"],
            "author": book["author"],
            "section": section + 1,
            "section_title": book["sections"][section]["title"],
            "total_sections": len(book["sections"]),
            "position_minutes": round(position / 60, 1),
        }

    def _in_progress(self):
        with self._bookmarks_lock:
            marks = sorted(self._bookmarks.values(), key=lambda m: m["updated"], reverse=True)
        return [{"title": m["title"], "author": m["author"],
                 "section": m["section"] + 1} for m in marks[:5]]

    def _find_bookmark(self, title):
        if not title:
            return None
        wanted = _normalise_title(title)
        best_id, best = None, 0.0
        with self._bookmarks_lock:
            for book_id, mark in self._bookmarks.items():
                ratio = difflib.SequenceMatcher(None, wanted, _normalise_title(mark["title"])).ratio()
                if ratio > best:
                    best_id, best = book_id, ratio
        return best_id if best >= 0.75 else None

    def _save_current_bookmark(self):
        with self._cond:
            book, section, position = self._book, self._section, self._position
        if book is None:
            return
        with self._bookmarks_lock:
            self._bookmarks[book["id"]] = {
                "title": book["title"],
                "author": book["author"],
                "section": section,
                "position": round(position, 1),
                "updated": time.time(),
            }
        self._write_bookmarks()

    def _write_bookmarks(self):
        with self._bookmarks_lock:
            data = dict(self._bookmarks)
        try:
            tmp = BOOKMARKS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, BOOKMARKS_PATH)
        except OSError as e:
            print(f"[AUDIOBOOK] Could not save bookmarks: {e}")

    @staticmethod
    def _load_bookmarks():
        try:
            with open(BOOKMARKS_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


if __name__ == "__main__":
    # Standalone test: python3 audiobook.py "treasure island"
    import sys

    player = AudiobookPlayer()
    print(player.play(" ".join(sys.argv[1:]) or "Treasure Island"))
    try:
        while True:
            time.sleep(5)
            print(player.status())
    except KeyboardInterrupt:
        player.shutdown()
