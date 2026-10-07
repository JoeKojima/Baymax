"""
Baymax LED eyes + neck servo, driven from the LattePanda.

Python port of the sketch that used to run on a separate Arduino UNO.  The
LattePanda's onboard Arduino (ATmega32U4 / Leonardo) runs StandardFirmata and
this module drives it over USB serial with pyfirmata2, so the eyes can be
controlled from a thread inside realtime_gemini_8.py / realtime_gemini_9.py.

Expressions:
    idle      open eyes with a random double blink every 1.5–4 s
    thinking  pupils glance up and slowly squint while Gemini is working on a
              reply (driven by ThinkingIndicator)

Wiring (LattePanda onboard Arduino header — same pins as the old UNO):

    Pin 6   -> Eye 1 DIN
    Pin 13  -> CLK  (shared)
    Pin 10  -> CS   (shared)
    Pin 9   -> Neck servo signal
    5V/GND  -> VCC/GND (shared)

    Eye 1 DOUT -> Eye 2 DIN  (two daisy-chained MAX7219 8x8 matrices)

The MAX7219 protocol is bit-banged exactly like the LedControl library does
(shiftOut MSB first), so no hardware SPI pins are required.

Usage:
    eyes = BaymaxEyes()
    eyes.start(shutdown_event)   # render thread (idle blinking)
    eyes.set_thinking(True)      # non-blocking, callable from any thread
    eyes.blink()                 # non-blocking request
    eyes.move_neck_smooth(70)
    eyes.stop()

    thinking = ThinkingIndicator(eyes)
    thinking.user_spoke()        # user transcription arrived
    thinking.update()            # call every ~50 ms
    thinking.model_audio()       # first reply audio → back to idle eyes
"""
import os
import random
import threading
import time

# ─── Pins ────────────────────────────────────────────────────────────────────
DIN_PIN = 6
CLK_PIN = 13
CS_PIN = 10
SERVO_PIN = 9

NUM_DEVICES = 2
BRIGHTNESS = 5  # 0–15

# Neck movement limits
SERVO_LEFT = 68
SERVO_RIGHT = 112
SERVO_CENTER = 90

# Serial port of the onboard Arduino (None = autodetect)
EYES_PORT = os.getenv("BAYMAX_EYES_PORT") or None
EYES_ENABLED = os.getenv("BAYMAX_EYES_ENABLED", "1") != "0"

# ─── Eye patterns ────────────────────────────────────────────────────────────
# Open eye
EYE_OPEN = [
    0b00111100,
    0b01111110,
    0b11111111,
    0b11100111,
    0b11100111,
    0b11111111,
    0b01111110,
    0b00111100,
]

# Thin horizontal line for closed eye
EYE_CLOSED = [
    0b00000000,
    0b00000000,
    0b00000000,
    0b00000000,
    0b01111110,
    0b00000000,
    0b00000000,
    0b00000000,
]


def eye_with_pupil(top_row):
    """Open-eye outline with the 2x2 pupil starting at `top_row` (EYE_OPEN = 3)."""
    outline = [0b00111100, 0b01111110] + [0b11111111] * 4 + [0b01111110, 0b00111100]
    pupil = 0b00011000
    return [row & ~pupil if top_row <= i <= top_row + 1 else row
            for i, row in enumerate(outline)]


# Thinking: pupils glide up, then alternate between "looking up" and a slight
# squint (top lid lowered).  Every frame is left/right symmetric, so it looks
# the same on both eyes whichever way eye 2 is mounted.
EYE_LOOK_UP = eye_with_pupil(1)
EYE_LOOK_UP_SQUINT = [0b00000000] + EYE_LOOK_UP[1:]
THINKING_ENTER_FRAMES = [eye_with_pupil(2), EYE_LOOK_UP]
THINKING_LOOP_FRAMES = [EYE_LOOK_UP, EYE_LOOK_UP_SQUINT]
THINKING_ENTER_FRAME_SECONDS = 0.08
THINKING_LOOP_FRAME_SECONDS = 0.6

# ThinkingIndicator timing
THINKING_ONSET_SECONDS = 0.35      # user quiet this long → show thinking
THINKING_MAX_ONSET_SECONDS = 1.5   # show it by now even if the mic stays noisy
THINKING_TIMEOUT_SECONDS = 8.0     # no reply after this → give up, back to idle

# ─── MAX7219 register opcodes (from LedControl) ──────────────────────────────
OP_NOOP = 0
OP_DIGIT0 = 1
OP_DECODEMODE = 9
OP_INTENSITY = 10
OP_SCANLIMIT = 11
OP_SHUTDOWN = 12
OP_DISPLAYTEST = 15


class LedControl:
    """Port of the Arduino LedControl library for daisy-chained MAX7219s,
    bit-banged over Firmata digital pins."""

    def __init__(self, board, din, clk, cs, num_devices):
        self._din = board.get_pin(f"d:{din}:o")
        self._clk = board.get_pin(f"d:{clk}:o")
        self._cs = board.get_pin(f"d:{cs}:o")
        self._num_devices = num_devices
        self._status = [None] * (8 * num_devices)   # None = unknown, always write
        self._din_state = None

        self._clk.write(0)
        self._cs.write(1)
        for device in range(num_devices):
            self._spi_transfer(device, OP_DISPLAYTEST, 0)
            self._spi_transfer(device, OP_SCANLIMIT, 7)
            self._spi_transfer(device, OP_DECODEMODE, 0)
            self.clear_display(device)
            self.shutdown(device, True)

    def shutdown(self, device, off):
        self._spi_transfer(device, OP_SHUTDOWN, 0 if off else 1)

    def set_intensity(self, device, intensity):
        self._spi_transfer(device, OP_INTENSITY, max(0, min(15, intensity)))

    def clear_display(self, device):
        for row in range(8):
            self.set_row(device, row, 0)

    def set_row(self, device, row, value):
        # Only rows that actually change are sent — each row costs ~70 Firmata writes
        if self._status[device * 8 + row] == value:
            return
        self._status[device * 8 + row] = value
        self._spi_transfer(device, OP_DIGIT0 + row, value)

    def _write_din(self, value):
        # Skip redundant writes — every Firmata message costs a USB round of I/O
        if value != self._din_state:
            self._din.write(value)
            self._din_state = value

    def _shift_out(self, byte):
        for bit in range(7, -1, -1):
            self._write_din((byte >> bit) & 1)
            self._clk.write(1)
            self._clk.write(0)

    def _spi_transfer(self, device, opcode, data):
        # One 16-bit word per device in the chain; every other device gets a NOOP.
        # The last device in the chain is shifted out first.
        spidata = [0] * (self._num_devices * 2)
        offset = device * 2
        spidata[offset + 1] = opcode
        spidata[offset] = data
        self._cs.write(0)
        for i in range(len(spidata), 0, -1):
            self._shift_out(spidata[i - 1])
        self._cs.write(1)


class BaymaxEyes:
    """Owns the Firmata connection.  A single render thread draws everything;
    other threads only request changes (set_thinking / blink), so callers such
    as the asyncio Gemini loop never block on the hardware."""

    def __init__(self, port=EYES_PORT):
        self._port = port
        self._board = None
        self._lc = None
        self._servo = None
        self._servo_angle = SERVO_CENTER
        self._lock = threading.RLock()       # hardware access
        self._thread = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()       # interrupts the render thread's waits
        self._thinking = False
        self._blink_requested = False

    # ── Setup ────────────────────────────────────────────────────────────────
    def connect(self):
        from pyfirmata2 import Arduino

        port = self._port or Arduino.AUTODETECT
        print(f"[EYES] Connecting to onboard Arduino ({port})...")
        self._board = Arduino(port)

        # Initialize both eyes
        self._lc = LedControl(self._board, DIN_PIN, CLK_PIN, CS_PIN, NUM_DEVICES)
        for device in range(NUM_DEVICES):
            self._lc.shutdown(device, False)        # Wake MAX7219
            self._lc.set_intensity(device, BRIGHTNESS)
            self._lc.clear_display(device)
        self.show(EYE_OPEN)

        # Servo
        self._servo = self._board.get_pin(f"d:{SERVO_PIN}:s")
        self._servo.write(SERVO_CENTER)
        self._servo_angle = SERVO_CENTER
        time.sleep(0.5)
        print("[EYES] Eyes and neck servo ready.")

    def close(self):
        with self._lock:
            if self._board is not None:
                try:
                    for device in range(NUM_DEVICES):
                        self._lc.clear_display(device)
                    self._board.exit()
                except Exception:
                    pass
                self._board = None

    # ── Requests (any thread, non-blocking) ──────────────────────────────────
    def set_thinking(self, thinking):
        thinking = bool(thinking)
        if thinking != self._thinking:
            self._thinking = thinking
            self._wake.set()

    @property
    def thinking(self):
        return self._thinking

    def blink(self):
        self._blink_requested = True
        self._wake.set()

    # ── Display ──────────────────────────────────────────────────────────────
    def show(self, pattern):
        """Update both eyes.  Eye 2 is mounted upside down relative to eye 1,
        so it is drawn vertically flipped."""
        with self._lock:
            for row in range(8):
                self._lc.set_row(0, row, pattern[row])
            for row in range(8):
                self._lc.set_row(1, 7 - row, pattern[row])

    def _hold(self, seconds, while_thinking):
        """Wait up to `seconds`; returns False early if the expression changed,
        a blink was requested while idle, or we are stopping — so the render
        loop can react immediately."""
        deadline = time.monotonic() + seconds
        while True:
            if self._stop_event.is_set() or self._thinking != while_thinking:
                return False
            if not while_thinking and self._blink_requested:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self._wake.wait(remaining)
            self._wake.clear()

    def _blink_once(self):
        # First blink, then a tiny second blink for a more natural feel.
        # Aborts as soon as thinking is requested.
        for pattern, seconds in ((EYE_CLOSED, 0.120), (EYE_OPEN, 0.050),
                                 (EYE_CLOSED, 0.120)):
            self.show(pattern)
            if not self._hold(seconds, while_thinking=False):
                break
        self.show(EYE_OPEN)

    def _animate_thinking(self):
        for frame in THINKING_ENTER_FRAMES:
            self.show(frame)
            if not self._hold(THINKING_ENTER_FRAME_SECONDS, while_thinking=True):
                return
        i = 0
        while True:
            self.show(THINKING_LOOP_FRAMES[i % len(THINKING_LOOP_FRAMES)])
            i += 1
            if not self._hold(THINKING_LOOP_FRAME_SECONDS, while_thinking=True):
                return

    # ── Servo ────────────────────────────────────────────────────────────────
    def move_neck_smooth(self, end_angle, step_delay=0.035):
        end_angle = max(SERVO_LEFT, min(SERVO_RIGHT, int(end_angle)))
        with self._lock:
            start = self._servo_angle
            step = 1 if end_angle >= start else -1
            for angle in range(start, end_angle + step, step):
                self._servo.write(angle)
                time.sleep(step_delay)
            self._servo_angle = end_angle

    # ── Render thread ────────────────────────────────────────────────────────
    def _render_loop(self):
        next_blink = time.monotonic() + random.uniform(1.5, 4.0)
        while not self._stop_event.is_set():
            if self._thinking:
                self._animate_thinking()
                self.show(EYE_OPEN)
                next_blink = time.monotonic() + random.uniform(1.5, 4.0)
                continue

            if self._blink_requested or time.monotonic() >= next_blink:
                self._blink_requested = False
                self._blink_once()
                # Wait a random amount of time before the next blink
                next_blink = time.monotonic() + random.uniform(1.5, 4.0)
                continue

            self._hold(next_blink - time.monotonic(), while_thinking=False)

    def _run(self, shutdown_event):
        try:
            self.connect()
        except Exception as e:
            print(f"[EYES] Could not connect to eyes hardware (non-fatal): {e}")
            return

        # Let the main loop's shutdown event stop the render loop too
        if shutdown_event is not None:
            def _watch():
                shutdown_event.wait()
                self._stop_event.set()
                self._wake.set()
            threading.Thread(target=_watch, daemon=True).start()

        try:
            self._render_loop()
        except Exception as e:
            print(f"[EYES] Eyes thread error: {e}")
        finally:
            self.close()
            print("[EYES] Eyes thread stopped.")

    def start(self, shutdown_event=None):
        if not EYES_ENABLED:
            print("[EYES] Disabled via BAYMAX_EYES_ENABLED=0.")
            return
        self._thread = threading.Thread(
            target=self._run, args=(shutdown_event,), daemon=True
        )
        self._thread.start()
        print("[EYES] Eyes thread started.")

    def stop(self, timeout=2):
        self._stop_event.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class ThinkingIndicator:
    """Decides when to show thinking eyes: after the user has finished speaking
    and before Gemini's reply audio starts.

    Pure timing logic (no hardware), fed events from the Gemini loop:
      user_spoke()   — an input transcription fragment arrived
      model_audio()  — reply audio arrived (thinking is over)
      turn_ended()   — turn_complete / interrupted / reconnect
      update()       — call every ~50 ms; pass the last time the mic was loud
                       (if known) so thinking waits until the user goes quiet
    """

    def __init__(self, eyes, clock=time.monotonic,
                 onset=THINKING_ONSET_SECONDS,
                 max_onset=THINKING_MAX_ONSET_SECONDS,
                 timeout=THINKING_TIMEOUT_SECONDS):
        self._eyes = eyes
        self._clock = clock
        self._onset = onset
        self._max_onset = max_onset
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending = False        # user spoke, no reply yet
        self._responding = False     # reply audio is streaming for this turn
        self._last_fragment = 0.0
        self._thinking_since = None

    def user_spoke(self):
        with self._lock:
            if self._responding:
                return  # late transcription of a turn Gemini already answered
            if not self._pending:
                self._pending = True
            self._last_fragment = self._clock()

    def model_audio(self):
        with self._lock:
            self._responding = True
            self._pending = False
            if self._thinking_since is not None:
                print(f"[EYES] Thought for "
                      f"{(self._clock() - self._thinking_since) * 1000:.0f} ms", flush=True)
            self._set(False)

    def turn_ended(self):
        with self._lock:
            self._responding = False
            self._pending = False
            self._set(False)

    def update(self, last_loud_mic_ts=None):
        with self._lock:
            if not self._pending or self._responding:
                return
            now = self._clock()
            if self._thinking_since is not None:
                if now - self._thinking_since > self._timeout:
                    print("[EYES] No reply — leaving thinking mode.", flush=True)
                    self._pending = False
                    self._set(False)
                return
            last_user = self._last_fragment
            if last_loud_mic_ts is not None:
                last_user = max(last_user, last_loud_mic_ts)
            quiet = now - last_user >= self._onset
            overdue = now - self._last_fragment >= self._max_onset
            if quiet or overdue:
                self._set(True)

    @property
    def thinking(self):
        return self._thinking_since is not None

    def _set(self, thinking):
        if thinking and self._thinking_since is None:
            self._thinking_since = self._clock()
        elif not thinking:
            self._thinking_since = None
        self._eyes.set_thinking(thinking)


if __name__ == "__main__":
    # Standalone test: idle blinking, or alternate idle/thinking with --thinking
    import sys

    eyes = BaymaxEyes()
    eyes.start()
    try:
        while True:
            if "--thinking" in sys.argv:
                time.sleep(4)
                eyes.set_thinking(not eyes.thinking)
                print(f"[EYES] thinking={eyes.thinking}")
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        eyes.stop()
