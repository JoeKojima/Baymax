"""LibriVox audiobooks mixed into Ember's own speaker stream.

The speaker callback pulls book audio through `render`, so echo cancellation
receives the book as part of its reference and removes it from the mic, and
Ember's voice always wins: the book holds while Ember is speaking. Chapters are
downloaded and decoded (miniaudio) on a feeder thread into a ~2 s buffer; the
audio callback never downloads or decodes.
"""
from collections import deque
import threading

import numpy as np

import runtime_monitor as monitor
from . import librivox

RATE = 48000
BUFFER_SECONDS = 2.0
VOLUME = 0.6  # Quieter than Ember's voice; also less leakage into the mic.
ACTIONS = ["play", "pause", "resume", "stop", "next", "status"]

DECLARATION = {
    "name": "audiobook",
    "description": "Read free public-domain LibriVox audiobooks aloud and control playback.",
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "enum": ACTIONS},
        "title": {"type": "STRING", "description": "Book title, for play."},
        "author": {"type": "STRING", "description": "Author, for play (optional)."}},
        "required": ["action"]},
}
INSTRUCTION = """
[AUDIOBOOKS]
You can read free public-domain LibriVox audiobooks aloud with the audiobook
tool: action play with a title and/or author, or pause, resume, stop, next
(chapter) and status. Only classics (mostly published before about 1930) are
available; if a book is not found, say so kindly and suggest a classic. Give a
short acknowledgment when starting. While a book is playing, your microphone
also hears the narration: stay silent and ignore anything that is not clearly
addressed to you (for example by name, or asking to pause, stop or skip); never
ask the person to repeat during a book. The book pauses itself while you speak.
"""


def _stream(path, rate):
    import miniaudio
    return miniaudio.stream_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16,
                                 nchannels=1, sample_rate=rate, frames_to_read=4800)


class Player:
    def __init__(self, rate=RATE, source=librivox, decode=_stream, volume=VOLUME):
        self.rate, self.source, self.decode, self.volume = rate, source, decode, volume
        self.capacity = int(rate * BUFFER_SECONDS) * 2
        self.lock = threading.Lock()
        self.space = threading.Condition(self.lock)
        self.buffer = bytearray()
        self.marks = deque()   # [bytes left in buffer, chapter] in buffer order.
        self.book = None
        self.index = 0         # Chapter being heard (the feeder may be ahead).
        self.playing = False   # A book is loaded and not stopped.
        self.paused = False
        self.feeding = False   # Feeder thread still has chapters to deliver.
        self.generation = 0    # Bumped by play/next/stop; stale feeders exit.

    # ── Speaker callback side: never blocks on I/O ────────────────────────────
    def render(self, n_bytes, voice_active):
        """Book PCM for this callback, or None. Holds while Ember is speaking."""
        with self.lock:
            if not self.playing or self.paused or voice_active:
                return None
            chunk = bytes(self.buffer[:n_bytes])
            del self.buffer[:n_bytes]
            used = len(chunk)
            while used and self.marks:
                self.index = self.marks[0][1]
                take = min(used, self.marks[0][0])
                self.marks[0][0] -= take
                used -= take
                if not self.marks[0][0]:
                    self.marks.popleft()
            if not chunk and not self.feeding:
                self.playing = False  # Book finished.
            self.space.notify_all()
        if not chunk:
            return None
        chunk += b"\0" * (n_bytes - len(chunk))
        return (np.frombuffer(chunk, dtype=np.int16) * self.volume).astype(np.int16).tobytes()

    # ── Feeder thread ─────────────────────────────────────────────────────────
    def _current(self, generation):
        return self.generation == generation and self.playing

    def _feed(self, generation, book, index):
        try:
            while index < len(book["sections"]):
                with self.lock:
                    if not self._current(generation):
                        return
                path = self.source.download(book, index, cancelled=lambda: not self._current(generation))
                for frames in self.decode(path, self.rate):
                    data = frames.tobytes() if hasattr(frames, "tobytes") else bytes(frames)
                    with self.space:
                        while self._current(generation) and len(self.buffer) >= self.capacity:
                            self.space.wait(0.2)
                        if not self._current(generation):
                            return
                        self.buffer += data
                        if self.marks and self.marks[-1][1] == index:
                            self.marks[-1][0] += len(data)
                        else:
                            self.marks.append([len(data), index])
                index += 1
        except InterruptedError:
            return
        except Exception as error:
            print(f"[AUDIOBOOK] Playback failed: {error}", flush=True)
            monitor.record("audiobook", "Playback failed: " + type(error).__name__)
        finally:
            with self.lock:
                if self.generation == generation:
                    self.feeding = False

    def _start(self, book, index):
        # Caller holds self.lock.
        self.generation += 1
        self.book, self.index = book, index
        self.buffer.clear()
        self.marks.clear()
        self.playing, self.paused, self.feeding = True, False, True
        self.space.notify_all()
        threading.Thread(target=self._feed, args=(self.generation, book, index),
                         name="ember-audiobook", daemon=True).start()

    # ── Tool actions ──────────────────────────────────────────────────────────
    def status(self):
        with self.lock:
            if self.book is None or not self.playing:
                return {"status": "stopped"}
            return {"status": "paused" if self.paused else "playing", "title": self.book["title"],
                    "author": self.book["author"], "chapter": self.index + 1,
                    "chapters": len(self.book["sections"]),
                    "chapter_title": self.book["sections"][self.index]["title"]}

    def execute(self, args):
        if not isinstance(args, dict) or args.get("action") not in ACTIONS:
            return {"error": "Expected an action: " + ", ".join(ACTIONS)}
        action = args["action"]
        if action == "play":
            title, author = args.get("title"), args.get("author")
            if not (title or author):
                return {"error": "Give a title or an author"}
            if self.decode is _stream:
                try:
                    import miniaudio  # noqa: F401
                except ImportError:
                    return {"error": "Audiobooks are not installed on this robot"}
            results = self.source.search(title=title, author=author, limit=1)
            if not results:
                return {"error": "No LibriVox recording found", "title": title, "author": author}
            book = self.source.fetch_book(results[0]["id"])
            with self.lock:
                self._start(book, 0)
            monitor.record("audiobook", "Playing " + book["title"])
            return {"status": "starting", "title": book["title"], "author": book["author"],
                    "chapters": len(book["sections"])}
        with self.lock:
            if self.book is None or not self.playing:
                return {"status": "stopped"} if action in ("status", "stop") else {"error": "No audiobook is playing"}
            if action == "pause":
                self.paused = True
            elif action == "resume":
                self.paused = False
            elif action == "stop":
                self.playing = False
                self.generation += 1
                self.buffer.clear()
                self.marks.clear()
                self.space.notify_all()
                return {"status": "stopped"}
            elif action == "next":
                if self.index + 1 >= len(self.book["sections"]):
                    return {"error": "Already on the last chapter"}
                self._start(self.book, self.index + 1)
        return self.status()


player = Player()


def execute(args):
    return player.execute(args)


def register(sources=None):
    """Feed the book into Ember's speaker stream (via audio cleanup's mixer)."""
    if sources is None:
        from features.conversational_ability.processing import output_sources as sources
    if player.render not in sources:
        sources.append(player.render)


register()
