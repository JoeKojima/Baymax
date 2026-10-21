"""Single-threaded expressions, delayed thinking and priority fall-alert eyes."""
from contextlib import contextmanager
import os
from pathlib import Path
import random
import sys
import threading
import time

from .driver import LedControl
from .patterns import EXPRESSIONS

PORT = "/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00"
OPEN = (0x3C, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x3C)
BLINK = (0, 0, 0, 0, 0x3C, 0, 0, 0)
BLANK = (0,) * 8
DOTS = tuple((0, 0, 0, 0, value, value, 0, 0) for value in (0xC0, 0x18, 0x03))
THINKING = tuple((dot, BLANK) for dot in DOTS) + tuple((BLANK, dot) for dot in DOTS)
# Consecutive MVP starts that never reached Gemini; X eyes ("unplug and replug") after 3.
FAILURES = Path(__file__).resolve().parents[2] / ".eyes-failures"
FAILED_STARTS_FOR_X = 3


def failure_face(connected, path=FAILURES):
    """Face left on the matrices after a failed run: confused while systemd retries, then X."""
    try:
        count = int(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        count = 0
    count = 1 if connected else count + 1
    try:
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return 'dead' if count >= FAILED_STARTS_FOR_X else 'confused'


def clear_failures(path=FAILURES):
    try:
        path.unlink()
    except OSError:
        pass


class Eyes:
    def __init__(self, port=PORT, board_factory=None, clock=time.monotonic, booting=False, dead_after=0):
        self.port, self.board_factory, self.clock = port, board_factory, clock
        # Confused spinner from launch until Gemini first connects ('listening').
        self.booting = booting
        # X eyes once Gemini has been unreachable this long (0 disables).
        self.dead_after = dead_after
        self.unready_since = clock() if booting else None
        self.connected_once = False
        self.exit_failed = False
        self.final_face = None  # Left on the matrices at exit instead of blanking them.
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.waiting_since = None
        self.responding = False
        self.thread = None
        self.expression = 'normal'
        self.expression_until = 0
        self.fall_until = 0
        self.fall_speaking = False
        self.reply_complete = False

    def express(self, name):
        if name not in ('normal', *EXPRESSIONS):
            return False
        if self.stopping.is_set() or (self.thread is not None and not self.thread.is_alive()):
            return False
        with self.lock:
            self.expression = name
            self.expression_until = self.clock() + 30
        self.wake.set()
        return True

    def event(self, event):
        """Request state only; no serial writes on the Gemini/audio thread."""
        with self.lock:
            if event == "listening":
                self.booting = False
                self.connected_once = True
                self.unready_since = None
            elif event in ("connecting", "reconnecting") and self.unready_since is None:
                self.unready_since = self.clock()
            if event == "user_speech":
                if not self.responding:
                    if self.waiting_since is None:
                        self.expression = 'normal'
                    self.waiting_since = self.clock()
            elif event == "thinking":
                if not self.responding and self.waiting_since is None:
                    self.waiting_since = self.clock()
            elif event == "speaking":
                self.responding = True
                self.waiting_since = None
                self.reply_complete = False
                if self.clock() < self.fall_until:
                    self.fall_speaking = True
            elif event == 'fall_alert':
                self.fall_until = self.clock() + 15
                self.fall_speaking = False
                self.waiting_since = None
            elif event == 'reply_complete':
                self.responding = False
                self.waiting_since = None
                self.reply_complete = True
            elif event == 'reply_playback_done':
                if self.reply_complete:
                    self.expression = 'normal'
                    if self.fall_speaking:
                        self.fall_until = 0
                    self.reply_complete = False
            elif event == 'calling_tool':
                pass
            else:
                self.responding = False
                self.waiting_since = None
                self.expression = 'normal'
                self.fall_until = 0
                self.reply_complete = False
        self.wake.set()

    def mode(self):
        with self.lock:
            now = self.clock()
            if now < self.fall_until:
                return 'surprised'
            if self.dead_after and self.unready_since is not None and now - self.unready_since > self.dead_after:
                return 'dead'
            if self.booting:
                return 'confused'
            if self.waiting_since is not None and now - self.waiting_since > 2.0:
                return 'thinking'
            return self.expression if now < self.expression_until else 'normal'

    def thinking(self):
        with self.lock:
            return self.waiting_since is not None and self.clock() - self.waiting_since > 2.0

    def hold(self, seconds, thinking):
        end = self.clock() + seconds
        expected = 'thinking' if thinking is True else 'normal' if thinking is False else thinking
        while not self.stopping.is_set() and self.mode() == expected:
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
        previous = 'normal'
        while not self.stopping.is_set():
            mode = self.mode()
            if mode != previous:
                frame = 0
            if mode == 'thinking':
                leds.draw_pair(*THINKING[frame % len(THINKING)])
                frame += 1
                self.hold(0.75, True)
                was_thinking = True
            elif mode != 'normal':
                frames, seconds = EXPRESSIONS[mode]
                pair = frames[frame % len(frames)]
                leds.draw_pair(*pair)
                frame += 1
                if mode in ('surprised', 'curious', 'love'):
                    if mode != previous:
                        next_blink = self.clock() + random.uniform(0.7, 1.4)
                    if self.clock() >= next_blink:
                        if random.random() < (0.50 if mode == 'surprised' else 0.55):
                            leds.draw_pair(BLINK)
                            self.hold(0.015, mode)
                            if self.mode() == mode:
                                leds.draw_pair(*pair)
                                if random.random() < 0.15 and self.hold(0.055, mode):
                                    leds.draw_pair(BLINK)
                                    self.hold(0.010, mode)
                                    if self.mode() == mode:
                                        leds.draw_pair(*pair)
                        next_blink = self.clock() + random.uniform(0.7, 1.4)
                    self.hold(max(0, next_blink - self.clock()), mode)
                else:
                    self.hold(seconds, mode)
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
            previous = mode

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
            print("[EYES] Ready: expressive eyes; thinking after 2 seconds; fall surprise override.", flush=True)
            self.render(leds)
        except Exception as error:
            print(f"[EYES] Unavailable (conversation continues): {error}", flush=True)
        finally:
            try:
                if leds is not None and self.final_face in EXPRESSIONS:
                    # The MAX7219s keep showing this after the process exits.
                    leds.draw_pair(*EXPRESSIONS[self.final_face][0][0])
                elif leds is not None:
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
    try:
        dead_after = float(os.getenv("BAYMAX_EYES_DEAD_AFTER_S", "120"))
    except ValueError:
        dead_after = 120.0
    eyes = Eyes(port=os.getenv("BAYMAX_EYES_PORT") or PORT, booting=True, dead_after=dead_after)
    previous = monitor.event_observer
    previous_expression = monitor.eye_expression_handler
    monitor.event_observer = eyes.event
    monitor.eye_expression_handler = eyes.express
    eyes.start()
    failed = False
    try:
        yield eyes
    except KeyboardInterrupt:
        raise  # Deliberate stop (Ctrl+C / SIGTERM): blank eyes as before.
    except SystemExit as error:
        failed = error.code not in (0, None)
        raise
    except BaseException:
        failed = True
        raise
    finally:
        monitor.event_observer = previous
        monitor.eye_expression_handler = previous_expression
        if failed or eyes.exit_failed:
            eyes.final_face = failure_face(eyes.connected_once)
        elif eyes.connected_once:
            clear_failures()
        eyes.stop()
