"""Blink normally; animate thinking only after a response wait exceeds 2s."""
from contextlib import contextmanager
import os
import random
import sys
import threading
import time

from .driver import LedControl

PORT = "/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00"
OPEN = (0x3C, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x3C)
BLINK = (0, 0, 0, 0, 0x3C, 0, 0, 0)
BLANK = (0,) * 8
DOTS = tuple((0, 0, 0, 0, value, value, 0, 0) for value in (0xC0, 0x18, 0x03))
THINKING = tuple((dot, BLANK) for dot in DOTS) + tuple((BLANK, dot) for dot in DOTS)


class Eyes:
    def __init__(self, port=PORT, board_factory=None, clock=time.monotonic):
        self.port, self.board_factory, self.clock = port, board_factory, clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.waiting_since = None
        self.responding = False
        self.thread = None

    def event(self, event):
        """Request state only; no serial writes on the Gemini/audio thread."""
        with self.lock:
            if event == "user_speech":
                if not self.responding:
                    self.waiting_since = self.clock()
            elif event == "thinking":
                if not self.responding and self.waiting_since is None:
                    self.waiting_since = self.clock()
            elif event == "speaking":
                self.responding = True
                self.waiting_since = None
            else:
                self.responding = False
                self.waiting_since = None
        self.wake.set()

    def thinking(self):
        with self.lock:
            return self.waiting_since is not None and self.clock() - self.waiting_since > 2.0

    def hold(self, seconds, thinking):
        end = self.clock() + seconds
        while not self.stopping.is_set() and self.thinking() == thinking:
            remaining = end - self.clock()
            if remaining <= 0:
                return True
            self.wake.wait(min(remaining, 0.02))
            self.wake.clear()
        return False

    def render(self, leds):
        leds.draw_pair(OPEN)
        next_blink = self.clock() + random.uniform(0.7, 1.4)
        frame = 0
        was_thinking = False
        while not self.stopping.is_set():
            if self.thinking():
                leds.draw_pair(*THINKING[frame % len(THINKING)])
                frame += 1
                self.hold(0.75, True)
                was_thinking = True
            else:
                if was_thinking:
                    leds.draw_pair(OPEN)
                    frame = 0
                    was_thinking = False
                    next_blink = self.clock() + random.uniform(0.7, 1.4)
                if self.clock() >= next_blink:
                    if random.random() < 0.75:
                        leds.draw_pair(BLINK)
                        self.hold(0.015, False)
                        leds.draw_pair(OPEN)
                        if not self.thinking() and random.random() < 0.15:
                            if self.hold(0.055, False):
                                leds.draw_pair(BLINK)
                                self.hold(0.010, False)
                                leds.draw_pair(OPEN)
                    next_blink = self.clock() + random.uniform(0.7, 1.4)
                self.hold(max(0, next_blink - self.clock()), False)

    def run(self):
        board = leds = None
        try:
            factory = self.board_factory
            if factory is None:
                from pyfirmata2 import Arduino
                factory = Arduino
            board = factory(self.port)
            leds = LedControl(board)
            for device in (0, 1):
                leds.set_intensity(device, 3)
                leds.shutdown(device, False)
            print("[EYES] Ready: normal blink; thinking after 2 seconds.", flush=True)
            self.render(leds)
        except Exception as error:
            print(f"[EYES] Unavailable (conversation continues): {error}", flush=True)
        finally:
            try:
                if leds is not None:
                    for device in (0, 1):
                        leds.shutdown(device, True)
            except Exception:
                pass
            finally:
                if board is not None:
                    try:
                        board.exit()
                    except Exception:
                        pass

    def start(self):
        self.thread = threading.Thread(target=self.run, name="ember-eyes", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3)


@contextmanager
def eyes_session():
    if os.getenv("BAYMAX_EYES_ENABLED", "1" if sys.platform.startswith("linux") else "0") != "1":
        yield
        return
    import runtime_monitor as monitor
    eyes = Eyes(port=os.getenv("BAYMAX_EYES_PORT") or PORT)
    previous = monitor.event_observer
    monitor.event_observer = eyes.event
    eyes.start()
    try:
        yield
    finally:
        monitor.event_observer = previous
        eyes.stop()
