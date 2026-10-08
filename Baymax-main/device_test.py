"""Short local hardware tests. No configuration writes or cloud requests."""
import base64
import json
import sys
import time

import numpy as np
import sounddevice as sd
from pc_hardware import open_camera, configure_audio_backend


def test_device(kind, raw):
    if kind not in ("microphone", "speaker", "camera"):
        raise ValueError("Unknown device test.")
    if raw == "none":
        raise ValueError("Choose a camera first.")
    index = int(raw) if raw else None
    if index is not None and index < 0:
        raise ValueError("Invalid device.")
    if kind == "camera":
        import cv2
        candidates = [index] if index is not None else [0, 1, 2]
        for candidate in candidates:
            camera = open_camera(cv2, candidate)
            try:
                if not camera.isOpened():
                    continue
                frame = None
                for _ in range(5):
                    ready, frame = camera.read()
                if ready and frame is not None:
                    height, width = frame.shape[:2]
                    frame = cv2.resize(frame, (640, max(1, round(height * 640 / width))))
                    ok, jpg = cv2.imencode(".jpg", frame)
                    if ok:
                        return {"image": "data:image/jpeg;base64," + base64.b64encode(jpg).decode(),
                                "message": f"Camera {candidate} preview captured. Test again to refresh."}
            finally:
                camera.release()
        raise ValueError("Camera could not be opened. Close other apps using it and refresh devices.")
    info = sd.query_devices(index, "input" if kind == "microphone" else "output")
    configure_audio_backend(sd, microphone=index if kind == "microphone" else None,
                            speaker=index if kind == "speaker" else None)
    rate = 48000
    if kind == "speaker":
        t = np.arange(rate) / rate
        tone = (0.08 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, t * 20) * np.minimum(1, (1-t)*20)).astype("float32")
        sd.play(tone, samplerate=rate, device=index, blocking=True)
        return {"message": "Test tone sent to " + info["name"] + ". Did you hear it?"}
    levels = []
    # Callback keeps the meter responsive while the UI polls this subprocess's result.
    def capture(data, frames, timing, status):
        levels.append(round(float(np.max(np.abs(data))), 4))
    with sd.InputStream(device=index, channels=1, samplerate=rate, blocksize=4800,
                        dtype="float32", callback=capture):
        time.sleep(3)
    return {"levels": levels, "peak": max(levels, default=0),
            "message": "Captured 3 seconds from " + info["name"] + ". Levels show sound reaching this microphone."}


if __name__ == "__main__":
    try:
        print(json.dumps(test_device(*sys.argv[1:3])))
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
