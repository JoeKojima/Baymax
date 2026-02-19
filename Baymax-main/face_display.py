import threading
import time
import random
import tkinter as tk


class FaceDisplay:
    """
    Simple animated face:
      - runs in its own thread using tkinter
      - states: "idle", "listening", "speaking"
      - blinks randomly in all states
    """

    def __init__(self, width: int = 480, height: int = 320):
        self.width = width
        self.height = height

        self._state = "idle"
        self._lock = threading.Lock()
        self._running = True

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---------- public API ----------

    def set_state(self, state: str):
        """Set the face state: 'idle', 'listening', or 'speaking'."""
        if state not in ("idle", "listening", "speaking"):
            state = "idle"
        with self._lock:
            self._state = state

    def stop(self):
        """Stop the face thread and close the window."""
        self._running = False
        self._thread.join(timeout=1.0)

    # ---------- internals ----------

    def _loop(self):
        root = tk.Tk()
        root.title("Robot Face")

        # --- FULLSCREEN, BORDERLESS ---
        root.attributes("-fullscreen", True)
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        self.width = screen_w
        self.height = screen_h
        # --------------------------------

        # canvas will stretch to fill the whole window
        canvas = tk.Canvas(root, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        # blinking logic
        next_blink = time.time() + random.uniform(2.0, 5.0)
        blink_duration = 0.12
        blinking = False
        blink_end = None

        def on_close():
            self._running = False
            try:
                root.destroy()
            except tk.TclError:
                pass

        # allow ESC to exit fullscreen
        root.bind("<Escape>", lambda e: on_close())
        root.protocol("WM_DELETE_WINDOW", on_close)

        while self._running:
            try:
                root.update_idletasks()
                root.update()

                now = time.time()

                # blink timing
                if not blinking and now >= next_blink:
                    blinking = True
                    blink_end = now + blink_duration
                if blinking and now >= blink_end:
                    blinking = False
                    next_blink = now + random.uniform(2.0, 5.0)

                with self._lock:
                    state = self._state

                self._draw_face(canvas, state, blinking)
                time.sleep(0.03)  # ~30 FPS

            except tk.TclError:
                self._running = False
                break

        try:
            root.destroy()
        except tk.TclError:
            pass
    def _draw_face(self, canvas: tk.Canvas, state: str, blinking: bool):
        # use ACTUAL canvas size, not the original 480x320
        canvas_width = canvas.winfo_width()
        canvas_height = canvas.winfo_height()
        if canvas_width <= 1 or canvas_height <= 1:
            return  # not laid out yet

        # keep these in sync so any other code using self.width/height matches
        self.width = canvas_width
        self.height = canvas_height

        canvas.delete("all")

        # background color by state (unchanged)
        if state == "idle":
            bg = "#0a0a28"  # dark blue
        elif state == "listening":
            bg = "#1c4c7a"  # bluish
        elif state == "speaking":
            bg = "#1c6432"  # greenish
        else:
            bg = "#000000"

        # now this rectangle covers the WHOLE screen
        canvas.create_rectangle(0, 0, self.width, self.height, fill=bg, outline=bg)

        cx = self.width // 2
        cy = self.height // 2

        # keep same variable names; just tuned for big pixel eyes
        eye_offset_x = int(self.width * 0.18)
        eye_offset_y = int(self.height * -0.02)
        eye_radius = int(min(self.width, self.height) * 0.10)  # used as a scale only

        left_eye_center = (cx - eye_offset_x, cy + eye_offset_y)
        right_eye_center = (cx + eye_offset_x, cy + eye_offset_y)

        # same variable names, now LED-style colors
        eye_white_color = "#26f0ff"   # bright cyan
        eye_pupil_color = "#14c8ff"   # slightly dimmer cyan

        pixel_size = max(10, int(min(self.width, self.height) * 0.06))
        pixel_gap = max(3, pixel_size // 5)

        # state-based brightness tweak
        if state == "idle":
            on_color = eye_white_color
        elif state == "listening":
            on_color = "#5ff7ff"   # brighter
        elif state == "speaking":
            on_color = "#34ffd0"   # a bit greener
        else:
            on_color = eye_white_color

        # both eyes use the SAME pattern so they face the same way
        # 1 = bright pixel, 2 = slightly dimmer "pupil" pixels
        open_pattern = [
            "01110",
            "22222",
            "21112",
        ]

        blink_pattern = [
            "22222",
        ]

        if blinking:
            pattern = blink_pattern
        else:
            pattern = open_pattern

        # small vertical offset for eye direction
        dy = 0
        if state == "listening":
            dy = -pixel_size // 3
        elif state == "speaking":
            dy = pixel_size // 3

        def draw_pixel_eye(center, pattern_local):
            rows = len(pattern_local)
            cols = len(pattern_local[0])

            total_w = cols * pixel_size + (cols - 1) * pixel_gap
            total_h = rows * pixel_size + (rows - 1) * pixel_gap

            origin_x = center[0] - total_w // 2
            origin_y = center[1] - total_h // 2 + dy

            for r, row in enumerate(pattern_local):
                for c, ch in enumerate(row):
                    if ch == "0":
                        continue
                    color = on_color if ch == "1" else eye_pupil_color
                    x0 = origin_x + c * (pixel_size + pixel_gap)
                    y0 = origin_y + r * (pixel_size + pixel_gap)
                    x1 = x0 + pixel_size
                    y1 = y0 + pixel_size
                    canvas.create_rectangle(
                        x0, y0, x1, y1,
                        fill=color,
                        outline=color,
                    )

        # draw only the two eyes
        draw_pixel_eye(left_eye_center, pattern)
        draw_pixel_eye(right_eye_center, pattern)
