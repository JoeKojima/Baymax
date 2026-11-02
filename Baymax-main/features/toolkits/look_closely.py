"""'Look closely': one sharper still of the camera view, on request.

Gemini normally sees a 320x240, quality-50 JPEG every 3 s: enough to see a
person, too blurry to read a letter or a label. When the person asks Ember to
look at or read something, the look_closely tool sends the newest camera frame
at full capture size (640x480 on the MVP) and quality 90 on the same realtime
video input the 3 s frames use, then returns hints (blurry, dark) so Ember can
ask them to hold it closer or steadier. Frames come from the existing camera
loop through `frame_observers`; no camera is opened and nothing is stored.
"""
import os
import threading
import time

import runtime_monitor as monitor

QUALITY = 90
MAX_SIDE = 1280        # Larger captures are scaled down; the MVP's 640x480 is sent as is.
STALE_SECONDS = 2.0    # An older frame means the camera stopped; never describe it as "now".
BLURRY = 60.0          # Variance of the Laplacian below this: too soft to read text.
DARK = 40              # Mean brightness (0-255) below this: too dark.

DECLARATION = {
    "name": "look_closely",
    "description": ("Send yourself one sharper photo of what the camera sees right now. Use it when the person "
                    "asks you to look at, read or identify something they are showing you."),
    "parameters": {"type": "OBJECT", "properties": {}},
}
INSTRUCTION = """
[LOOKING CLOSELY]
Your normal camera view is small and blurry. When someone asks you to look at,
read or identify something ("what am I holding?", "read this letter", "is this
expired?"), call look_closely first: a sharper photo arrives on your video input
right before the tool result. Describe or read only what you can actually see in
it. If the result says it is blurry or dark, or you cannot make out the text,
say so honestly and ask them to hold it closer to the camera, keep it still, or
turn on a light, then call look_closely again. Never guess at words you cannot
read. For medicine labels, read the label text aloud but never give dosing advice
or say whether to take it: suggest checking with their pharmacist or doctor.
"""


def enabled():
    return os.getenv("BAYMAX_LOOK_CLOSELY", "1") != "0"


class Camera:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.frame = None
        self.seen = None

    def observe(self, frame):
        """Camera thread: keep a reference to the newest frame (no copy, no work)."""
        with self.lock:
            self.frame, self.seen = frame, self.clock()

    def latest(self):
        with self.lock:
            return self.frame, self.seen

    def photo(self):
        """(jpeg bytes, result dict) or (None, error dict). Runs off the event loop."""
        frame, seen = self.latest()
        if frame is None:
            return None, {"error": "No camera picture is available on this robot. Tell them you can't see right now."}
        if self.clock() - seen > STALE_SECONDS:
            return None, {"error": "The camera stopped sending pictures. Tell them you can't see right now."}
        import cv2
        height, width = frame.shape[:2]
        if max(height, width) > MAX_SIDE:
            scale = MAX_SIDE / max(height, width)
            frame = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
            height, width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), QUALITY])
        if not ok:
            return None, {"error": "The photo could not be prepared. Tell them you can't see right now."}
        hints = []
        if float(cv2.Laplacian(gray, cv2.CV_64F).var()) < BLURRY:
            hints.append("blurry: ask them to hold it still, a little closer")
        if float(gray.mean()) < DARK:
            hints.append("dark: ask them to turn on a light")
        result = {"status": "sent", "photo": f"{width}x{height}, on your video input just before this result"}
        if hints:
            result["hints"] = hints
        return buffer.tobytes(), result


camera = Camera()


async def run(session):
    """Send one sharp frame to the live session, then return the tool result."""
    import asyncio
    from google.genai import types
    jpeg, result = await asyncio.to_thread(camera.photo)
    if jpeg is None:
        monitor.record("look_closely", "No photo: " + result["error"].split(".")[0])
        return result
    try:
        await session.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))
    except Exception:
        return {"error": "The photo could not be sent. Tell them you can't see it right now."}
    monitor.record("look_closely", "Sent a sharper photo " + result["photo"].split(",")[0])
    return result


def register(observers=None):
    if observers is None:
        from .fall_detection import frame_observers as observers
    if camera.observe not in observers:
        observers.append(camera.observe)


register()
