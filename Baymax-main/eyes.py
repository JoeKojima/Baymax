"""
Baymax LED eyes + neck servo, driven from the LattePanda.

Python port of the sketch that used to run on a separate Arduino UNO.  The
LattePanda's onboard Arduino (ATmega32U4 / Leonardo) runs StandardFirmata and
this module drives it over USB serial with pyfirmata2, so the eyes can be
controlled from a thread inside realtime_gemini_8.py.

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
    eyes.start(shutdown_event)   # idle blink loop in a daemon thread
    eyes.blink()                 # callable from any thread
    eyes.move_neck_smooth(70)
    eyes.stop()
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
        self._status = [0] * (8 * num_devices)
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
    """Owns the Firmata connection and runs the idle blink loop in a thread.
    All hardware access is serialised with a lock so other threads can call
    blink()/show()/move_neck_smooth() safely."""

    def __init__(self, port=EYES_PORT):
        self._port = port
        self._board = None
        self._lc = None
        self._servo = None
        self._servo_angle = SERVO_CENTER
        self._lock = threading.RLock()
        self._thread = None
        self._stop_event = threading.Event()

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

    # ── Display ──────────────────────────────────────────────────────────────
    def show(self, pattern):
        """Update both eyes.  Eye 2 is mounted upside down relative to eye 1,
        so it is drawn vertically flipped."""
        with self._lock:
            for row in range(8):
                self._lc.set_row(0, row, pattern[row])
            for row in range(8):
                self._lc.set_row(1, 7 - row, pattern[row])

    def blink(self):
        with self._lock:
            # First blink
            self.show(EYE_CLOSED)
            time.sleep(0.120)
            self.show(EYE_OPEN)
            time.sleep(0.050)

            # Tiny second blink gives the animation a more natural feel
            self.show(EYE_CLOSED)
            time.sleep(0.120)
            self.show(EYE_OPEN)

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

    # ── Thread ───────────────────────────────────────────────────────────────
    def _run(self, shutdown_event):
        try:
            self.connect()
        except Exception as e:
            print(f"[EYES] Could not connect to eyes hardware (non-fatal): {e}")
            return

        try:
            while not (self._stop_event.is_set()
                       or (shutdown_event and shutdown_event.is_set())):
                # Wait a random amount of time before blinking
                if self._stop_event.wait(random.uniform(1.5, 4.0)):
                    break
                if shutdown_event and shutdown_event.is_set():
                    break
                self.blink()
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
        if self._thread is not None:
            self._thread.join(timeout=timeout)


if __name__ == "__main__":
    # Standalone test: blink until Ctrl+C
    eyes = BaymaxEyes()
    eyes.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        eyes.stop()
