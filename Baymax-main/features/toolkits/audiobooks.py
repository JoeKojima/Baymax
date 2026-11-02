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

The same `player` plays radio stations and open recordings (`listen.py`): one
program at a time.
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


class Book:
    """A LibriVox book: chapters downloaded, then decoded; pauses after each chapter."""
    kind, label, live, limit_seconds = "audiobook", "AUDIOBOOK", False, None
    no_next = "Already on the last chapter"

    def __init__(self, book, source, decode):
        self.book, self.source, self.decode = book, source, decode
        self.sections = book["sections"]

    def frames(self, index, rate, cancelled):
        return self.decode(self.source.download(self.book, index, cancelled=cancelled), rate)

    def _chapter(self, index):
        return f"{index + 1} ('{self.sections[index]['title']}') of {self.book['title']} by {self.book['author']}"

    def checkpoint(self, index):
        """Note for the end of section `index` that pauses playback, or None to keep going."""
        if index + 1 < len(self.sections):
            # Checkpoint: nobody is ever stuck in a book for more than a chapter.
            return (f"[AUDIOBOOK] Chapter {self._chapter(index)} just finished and the recording is "
                    "paused. Briefly ask whether they would like to keep going or chat about it.")
        return None

    def paused_at(self, index):
        return f"the recording is paused partway through chapter {self._chapter(index)}"

    def finished(self, error=None):
        if error is not None:
            return (f"[AUDIOBOOK] {self.book['title']} stopped because the recording could not be downloaded "
                    "or played. Tell them briefly and offer to try again later.")
        return f"[AUDIOBOOK] {self.book['title']} is finished. Ask what they thought of it."

    def still_listening(self, minutes):
        return None

    def status(self, index):
        return {"kind": self.kind, "title": self.book["title"], "author": self.book["author"],
                "chapter": index + 1, "chapters": len(self.sections),
                "chapter_title": self.sections[index]["title"]}


class Player:
    """One thing plays at a time: an audiobook, a radio station or a recording (`program`)."""

    def __init__(self, rate=RATE, source=librivox, decode=_stream, volume=VOLUME,
                 listener_factory=WakeListener, notify=None):
        self.rate, self.source, self.decode, self.volume = rate, source, decode, volume
        self.listener_factory, self.listener = listener_factory, None
        self.notify = notify or (lambda text: tell_gemini(text))
        self.capacity = int(rate * BUFFER_SECONDS) * 2
        self.lock = threading.Lock()
        self.space = threading.Condition(self.lock)
        self.buffer = bytearray()
        self.marks = deque()   # [bytes left in buffer, section] in buffer order.
        self.program = None
        self.index = 0         # Section being heard (the feeder may be ahead).
        self.playing = False   # Something is loaded and not stopped.
        self.paused = False
        self.feeding = False   # Feeder thread still has sections to deliver.
        self.error = None      # Why the feeder stopped early, if it failed.
        self.heard = 0         # Bytes played since start/resume, for long-listening check-ins.
        self.generation = 0    # Bumped by play/next/stop; stale feeders exit.

    # ── Speaker callback side: never blocks on I/O ────────────────────────────
    def render(self, n_bytes, voice_active):
        """Program PCM for this callback, or None. Holds while Ember is speaking."""
        note = None
        with self.lock:
            if not self.playing or self.paused or voice_active:
                return None
            program = self.program
            # Never play past a section boundary within one callback.
            size = min(n_bytes, self.marks[0][0]) if self.marks else n_bytes
            chunk = bytes(self.buffer[:size])
            del self.buffer[:size]
            if chunk and self.marks:
                section = self.index = self.marks[0][1]
                self.marks[0][0] -= len(chunk)
                if not self.marks[0][0]:
                    self.marks.popleft()
                    note = program.checkpoint(section)
                    if note:
                        self._pause()
            if chunk and not note and program.limit_seconds:
                self.heard += len(chunk)
                if self.heard >= program.limit_seconds * self.rate * 2:
                    note = program.still_listening(round(program.limit_seconds / 60))
                    self._pause()
            if not chunk and not self.feeding:
                self.playing = False
                note = program.finished(self.error)
            self.space.notify_all()
        if note:
            monitor.record(program.kind, note[len(program.label) + 3:80])
            self.notify(note)
        if not chunk:
            return None
        chunk += b"\0" * (n_bytes - len(chunk))
        return (np.frombuffer(chunk, dtype=np.int16) * self.volume).astype(np.int16).tobytes()

    def _pause(self):
        # Caller holds self.lock. Live radio is not buffered while paused: resume reconnects.
        self.paused = True
        if self.program is not None and self.program.live:
            self.generation += 1
            self.buffer.clear()
            self.marks.clear()
            self.feeding = False
            self.space.notify_all()

    # ── Capture thread side: listening mode ───────────────────────────────────
    def gate(self, samples):
        """True while something plays: withhold the mic from Gemini, feed wake words."""
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
            program = self.program
            where = program.paused_at(self.index)
            self._pause()
        monitor.record(program.kind, f"Paused by '{phrase}'")
        self.notify(f"[{program.label}] The person said '{phrase}', so {where}. "
                    "Answer briefly, for example 'Yes?', and help them.")

    # ── Feeder thread ─────────────────────────────────────────────────────────
    def _current(self, generation):
        return self.generation == generation and self.playing

    def _feed(self, generation, program, index):
        try:
            while index < len(program.sections):
                with self.lock:
                    if not self._current(generation):
                        return
                for frames in program.frames(index, self.rate, lambda: not self._current(generation)):
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
            print(f"[{program.label}] Playback failed: {error}", flush=True)
            monitor.record(program.kind, "Playback failed: " + type(error).__name__)
            with self.lock:
                if self.generation == generation:
                    self.error = error
        finally:
            with self.lock:
                if self.generation == generation:
                    self.feeding = False

    def _start(self, program, index):
        # Caller holds self.lock.
        self.generation += 1
        self.program, self.index = program, index
        self.buffer.clear()
        self.marks.clear()
        self.playing, self.paused, self.feeding, self.error, self.heard = True, False, True, None, 0
        self.space.notify_all()
        threading.Thread(target=self._feed, args=(self.generation, program, index),
                         name="ember-" + program.kind, daemon=True).start()

    def play(self, program):
        """Start a program, replacing whatever was playing."""
        if self.listener is None:
            self.listener = self.listener_factory(self.wake)
            self.listener.start()  # Loads (and first time downloads) the wake-word model.
        with self.lock:
            self._start(program, 0)
        return "unavailable" if self.listener.failed else "ready" if self.listener.ready else "loading"

    def decoder_missing(self):
        if self.decode is not _stream:
            return False
        try:
            import miniaudio  # noqa: F401
        except ImportError:
            return True
        return False

    # ── Tool actions ──────────────────────────────────────────────────────────
    def status(self):
        with self.lock:
            if self.program is None or not self.playing:
                return {"status": "stopped"}
            return {"status": "paused" if self.paused else "playing", "volume": round(self.volume * 100),
                    **self.program.status(self.index)}

    def control(self, action, args):
        """Actions shared by every program: pause, resume, stop, next, status, volume."""
        if action == "volume":
            try:
                level = float(args["level"])
            except (KeyError, TypeError, ValueError):
                return {"error": "Give a level from 0 to 100"}
            if level != level:
                return {"error": "Give a level from 0 to 100"}
            self.volume = min(1.0, max(0.0, level / 100))
            return {**self.status(), "volume": round(self.volume * 100)}
        with self.lock:
            if self.program is None or not self.playing:
                return {"status": "stopped"} if action in ("status", "stop") else {"error": "Nothing is playing"}
            if action == "pause":
                self._pause()
            elif action == "resume":
                if self.listener is not None:
                    self.listener.clear()  # Don't wake on audio heard before resuming.
                if self.program.live:
                    self._start(self.program, 0)  # Reconnect: live radio, not where it paused.
                else:
                    self.paused, self.heard = False, 0
            elif action == "stop":
                self.playing = False
                self.generation += 1
                self.buffer.clear()
                self.marks.clear()
                self.space.notify_all()
                return {"status": "stopped"}
            elif action == "next":
                if self.index + 1 >= len(self.program.sections):
                    return {"error": self.program.no_next}
                self._start(self.program, self.index + 1)
        return self.status()

    def execute(self, args):
        """The audiobook tool."""
        if not isinstance(args, dict) or args.get("action") not in ACTIONS:
            return {"error": "Expected an action: " + ", ".join(ACTIONS)}
        action = args["action"]
        if action != "play":
            return self.control(action, args)
        title, author = args.get("title"), args.get("author")
        if not (title or author):
            return {"error": "Give a title or an author"}
        if self.decoder_missing():
            return {"error": "Audiobooks are not installed on this robot"}
        results = self.source.search(title=title, author=author, limit=1)
        if not results:
            return {"error": "No LibriVox recording found", "title": title, "author": author}
        book = self.source.fetch_book(results[0]["id"])
        wake_words = self.play(Book(book, self.source, self.decode))
        monitor.record("audiobook", "Playing " + book["title"])
        return {"status": "starting", "title": book["title"], "author": book["author"],
                "chapters": len(book["sections"]), "recording": "LibriVox volunteer narrator",
                "how_to_pause": "say 'Hey Ember' or 'stop'; it also pauses after each chapter",
                "wake_words": wake_words}


player = Player()


def execute(args):
    return player.execute(args)


def register(sources=None, gates=None):
    """Feed playback into Ember's speaker stream and gate the mic while it plays."""
    if sources is None or gates is None:
        from features.conversational_ability import processing
        sources = processing.output_sources if sources is None else sources
        gates = processing.input_gates if gates is None else gates
    if player.render not in sources:
        sources.append(player.render)
    if player.gate not in gates:
        gates.append(player.gate)


register()
