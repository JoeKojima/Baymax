"""LibriVox audiobooks mixed into Ember's own speaker stream.

The speaker callback pulls book audio through `render`, so echo cancellation
receives the book as part of its reference, and Ember's voice always wins: the
book holds while Ember is speaking. Chapters are downloaded and decoded
(miniaudio) on a feeder thread into a ~2 s buffer; the audio callback never
downloads or decodes.

Book mode: echo cancellation cannot remove enough narration for Gemini, which
then answers the narrator as if the person spoke. While a book plays, `gate`
withholds the mic from Gemini and feeds it to an offline wake-word listener
("Hey Ember", "Ember stop/pause", "stop", "pause"). A wake word, or the end of a chapter,
pauses the book and tells Gemini where it stopped so it can talk about it.
"""
import asyncio
import os
from collections import deque
import threading

import numpy as np

import runtime_monitor as monitor
from . import librivox
from .wakeword import WakeListener

RATE = 48000
BUFFER_SECONDS = 2.0


def _volume():
    """BAYMAX_BOOK_VOLUME (0-1, default full); wake words are judged against the narration level."""
    try:
        return min(1.0, max(0.0, float(os.getenv("BAYMAX_BOOK_VOLUME", "1"))))
    except ValueError:
        return 1.0


VOLUME = _volume()
ACTIONS = ["play", "pause", "resume", "stop", "next", "status", "volume"]

DECLARATION = {
    "name": "audiobook",
    "description": "Read free public-domain LibriVox audiobooks aloud and control playback.",
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "enum": ACTIONS},
        "title": {"type": "STRING", "description": "Book title, for play."},
        "author": {"type": "STRING", "description": "Author, for play (optional)."},
        "level": {"type": "NUMBER", "description": "Book volume 0-100 percent, for volume."}},
        "required": ["action"]},
}
INSTRUCTION = """
[AUDIOBOOKS]
You can play free public-domain LibriVox audiobooks with the audiobook tool:
action play with a title and/or author, or pause, resume, stop, next (chapter)
and status, or volume with a level from 0 to 100 percent (for "louder" or
"quieter", status tells you the current volume; step by about 20). They are recordings read by volunteer narrators, not by you. When
starting one, say briefly that you will play a LibriVox recording of it and
that they can say "Hey Ember" or "stop" any time to pause it. Only classics
(mostly published before about 1930) are available; if a book is not found,
say so kindly and suggest a classic.
While a book plays you only hear those pause words. A message starting with
'[AUDIOBOOK]' tells you that it paused and where. Then talk with them about the
story if they like, using what you know about that book and chapter; if you do
not know a chapter's details well, ask what they enjoyed instead of inventing
them. Use the audiobook tool with resume when they want to continue.
"""

_gemini = {"session": None, "loop": None}


def attach(session, loop):
    """Remember the live session that started the book, for pause notes."""
    _gemini["session"], _gemini["loop"] = session, loop


def tell_gemini(text):
    """Send an [AUDIOBOOK] note into the live session from any thread."""
    session, loop = _gemini["session"], _gemini["loop"]
    print(f"[AUDIOBOOK] {text}", flush=True)
    if session is None or loop is None or loop.is_closed():
        return

    async def send():
        try:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]},
                                              turn_complete=True)
        except Exception as error:
            print(f"[AUDIOBOOK] Could not tell Gemini: {error}", flush=True)

    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(send()))


def _stream(path, rate):
    import miniaudio
    return miniaudio.stream_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16,
                                 nchannels=1, sample_rate=rate, frames_to_read=4800)


class Player:
    def __init__(self, rate=RATE, source=librivox, decode=_stream, volume=VOLUME,
                 listener_factory=WakeListener, notify=None):
        self.rate, self.source, self.decode, self.volume = rate, source, decode, volume
        self.listener_factory, self.listener = listener_factory, None
        self.notify = notify or (lambda text: tell_gemini(text))
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
        note = None
        with self.lock:
            if not self.playing or self.paused or voice_active:
                return None
            # Never play past a chapter boundary within one callback.
            size = min(n_bytes, self.marks[0][0]) if self.marks else n_bytes
            chunk = bytes(self.buffer[:size])
            del self.buffer[:size]
            if chunk and self.marks:
                chapter = self.index = self.marks[0][1]
                self.marks[0][0] -= len(chunk)
                if not self.marks[0][0]:
                    self.marks.popleft()
                    if chapter + 1 < len(self.book["sections"]):
                        # Checkpoint: nobody is ever stuck in a book for more than a chapter.
                        self.paused = True
                        note = (f"[AUDIOBOOK] Chapter {chapter + 1} ('{self.book['sections'][chapter]['title']}') of "
                                f"{self.book['title']} by {self.book['author']} just finished and the recording is "
                                "paused. Briefly ask whether they would like to keep going or chat about it.")
            if not chunk and not self.feeding:
                self.playing = False
                note = f"[AUDIOBOOK] {self.book['title']} is finished. Ask what they thought of it."
            self.space.notify_all()
        if note:
            monitor.record("audiobook", note[12:80])
            self.notify(note)
        if not chunk:
            return None
        chunk += b"\0" * (n_bytes - len(chunk))
        return (np.frombuffer(chunk, dtype=np.int16) * self.volume).astype(np.int16).tobytes()

    # ── Capture thread side: book mode ────────────────────────────────────────
    def gate(self, samples):
        """True while a book plays: withhold the mic from Gemini, feed wake words."""
        with self.lock:
            active = self.playing and not self.paused
        listener = self.listener
        if not active or listener is None or not listener.ready:
            return False  # No wake-word model: fall back to the open mic.
        listener.feed(samples)
        return True

    def wake(self, phrase):
        with self.lock:
            if not self.playing or self.paused:
                return
            self.paused = True
            where = (f"partway through chapter {self.index + 1} ('{self.book['sections'][self.index]['title']}') "
                     f"of {self.book['title']} by {self.book['author']}")
        monitor.record("audiobook", f"Paused by '{phrase}'")
        self.notify(f"[AUDIOBOOK] The person said '{phrase}', so the recording is paused {where}. "
                    "Answer briefly, for example 'Yes?', and help them.")

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
            return {"status": "paused" if self.paused else "playing", "volume": round(self.volume * 100),
                    "title": self.book["title"],
                    "author": self.book["author"], "chapter": self.index + 1,
                    "chapters": len(self.book["sections"]),
                    "chapter_title": self.book["sections"][self.index]["title"]}

    def execute(self, args):
        if not isinstance(args, dict) or args.get("action") not in ACTIONS:
            return {"error": "Expected an action: " + ", ".join(ACTIONS)}
        action = args["action"]
        if action == "volume":
            try:
                level = float(args["level"])
            except (KeyError, TypeError, ValueError):
                return {"error": "Give a level from 0 to 100"}
            if level != level:
                return {"error": "Give a level from 0 to 100"}
            self.volume = min(1.0, max(0.0, level / 100))
            return {**self.status(), "volume": round(self.volume * 100)}
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
            if self.listener is None:
                self.listener = self.listener_factory(self.wake)
                self.listener.start()  # Loads (and first time downloads) the wake-word model.
            with self.lock:
                self._start(book, 0)
            monitor.record("audiobook", "Playing " + book["title"])
            return {"status": "starting", "title": book["title"], "author": book["author"],
                    "chapters": len(book["sections"]), "recording": "LibriVox volunteer narrator",
                    "how_to_pause": "say 'Hey Ember' or 'stop'; it also pauses after each chapter",
                    "wake_words": "unavailable" if self.listener.failed else "ready" if self.listener.ready else "loading"}
        with self.lock:
            if self.book is None or not self.playing:
                return {"status": "stopped"} if action in ("status", "stop") else {"error": "No audiobook is playing"}
            if action == "pause":
                self.paused = True
            elif action == "resume":
                self.paused = False
                if self.listener is not None:
                    self.listener.clear()  # Don't wake on audio heard before resuming.
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


def register(sources=None, gates=None):
    """Feed the book into Ember's speaker stream and gate the mic during books."""
    if sources is None or gates is None:
        from features.conversational_ability import processing
        sources = processing.output_sources if sources is None else sources
        gates = processing.input_gates if gates is None else gates
    if player.render not in sources:
        sources.append(player.render)
    if player.gate not in gates:
        gates.append(player.gate)


register()
