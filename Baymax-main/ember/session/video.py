"""Camera capture, the 3-second frame sender and the local MJPEG preview (version-8 behaviour).

One camera thread captures 640x480 frames at 15 fps, shares the latest frame
with the Gemini sender, hands each frame to the fall-detection worker (which
calls the registered frame observers) and keeps an FPS-annotated copy for the
MJPEG stream on port 8080. Gemini gets a 320x240 JPEG (quality 50) every 3 s.
"""
import asyncio
import collections
import threading
import time

import numpy as np

FALL_DETECTION_FPS = 15          # pose inference rate (frames per second)
FALL_ANGLE_THRESHOLD = 45.0
FALL_ANG_VEL_THRESHOLD = 25.0
FALL_HIP_VEL_THRESHOLD = 0.12
FALL_CONFIRMATION_FRAMES = 2
FALL_COOLDOWN_SECONDS = 3.0

_WHITE = (255, 255, 255)
_CYAN = (255, 220, 0)


def _text(cv2, img, text, pos, scale=0.65, color=_WHITE, thickness=2):
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


class Video:
    def __init__(self, devices, fall_worker, shutdown_event, tracker, cv2=None, types=None):
        if cv2 is None:
            import cv2
        self.cv2, self.devices, self.fall_worker = cv2, devices, fall_worker
        self.shutdown_event, self.tracker, self.types = shutdown_event, tracker, types
        self.latest_frame = None
        self.latest_lock = threading.Lock()
        self.annotated_frame = None
        self.annotated_lock = threading.Lock()

    # ── Camera thread (v8 _fall_detection_thread; pose inference lives in the fall toolkit) ──
    def camera_thread(self):
        cv2 = self.cv2
        cap = self.devices.open_camera()

        if cap is None:
            print("[CAM] Camera not available — video feed disabled.")
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        fall_worker = self.fall_worker
        fall_worker.start(fps=FALL_DETECTION_FPS, angle_threshold=FALL_ANGLE_THRESHOLD,
                          ang_vel_threshold=FALL_ANG_VEL_THRESHOLD, hip_vel_threshold=FALL_HIP_VEL_THRESHOLD,
                          history_window=8, confirmation_frames=FALL_CONFIRMATION_FRAMES,
                          cooldown_seconds=FALL_COOLDOWN_SECONDS)
        print(f"[CAM] Camera capture active (640x480, {FALL_DETECTION_FPS} fps) "
              f"— fall detection: {fall_worker.status()['state']}.")

        frame_interval = 1.0 / FALL_DETECTION_FPS
        prev_t = time.monotonic()
        fps_history = collections.deque(maxlen=30)

        try:
            while not self.shutdown_event.is_set():
                t0 = time.monotonic()

                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.1)
                    continue

                # Share raw frame for Gemini video sender
                with self.latest_lock:
                    self.latest_frame = frame
                fall_worker.submit(frame)

                # FPS
                now = time.monotonic()
                fps_history.append(1.0 / max(now - prev_t, 1e-6))
                prev_t = now
                fps = float(np.mean(fps_history))

                # Overlay: FPS only — skeleton/HUD/graph all need pose landmarks
                viz = frame.copy()
                _text(cv2, viz, f"FPS: {fps:5.1f}", (10, 24), color=_CYAN)

                # Share annotated frame for MJPEG stream
                with self.annotated_lock:
                    self.annotated_frame = viz

                # Throttle to target FPS
                elapsed = time.monotonic() - t0
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
        except Exception as e:
            print(f"[CAM] Camera thread error: {e}")
        finally:
            cap.release()
            fall_worker.stop()
            print("[CAM] Camera thread stopped.")

    # ── Gemini sender (v8 send_video_realtime) ──
    async def send(self, session):
        """Reads shared frames from the camera thread and sends to Gemini every 3s."""
        cv2, types = self.cv2, self.types
        # Wait for camera thread to start capturing
        for _ in range(50):
            with self.latest_lock:
                if self.latest_frame is not None:
                    break
            await asyncio.sleep(0.1)

        with self.latest_lock:
            if self.latest_frame is None:
                print("[VIDEO] No camera frames available — video disabled.")
                return

        print("[VIDEO] Sending shared camera frames to Gemini (1 frame / 3s).")

        while True:
            await asyncio.sleep(3.0)
            t0 = time.perf_counter()

            with self.latest_lock:
                frame = self.latest_frame
            if frame is None:
                continue

            # Resize to 320x240 for Gemini (fall detection runs at 640x480)
            small = cv2.resize(frame, (320, 240))
            _, buffer = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
            jpg_bytes = buffer.tobytes()
            encode_ms = (time.perf_counter() - t0) * 1000

            t1 = time.perf_counter()
            try:
                await session.send_realtime_input(video=types.Blob(data=jpg_bytes, mime_type="image/jpeg"))
            except Exception as e:
                print(f"[VIDEO] Error sending frame: {e}")
                continue
            send_ms = (time.perf_counter() - t1) * 1000
            self.tracker.record(encode_ms + send_ms)

    # ── MJPEG preview (v8 _start_mjpeg_server) ──
    def mjpeg_server(self, port=8080):
        """Lightweight MJPEG server in a daemon thread — serves annotated frames."""
        from http.server import HTTPServer, BaseHTTPRequestHandler
        video = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    try:
                        while not video.shutdown_event.is_set():
                            with video.annotated_lock:
                                frame = video.annotated_frame
                            if frame is None:
                                time.sleep(0.05)
                                continue
                            _, jpg = video.cv2.imencode(".jpg", frame, [int(video.cv2.IMWRITE_JPEG_QUALITY), 70])
                            data = jpg.tobytes()
                            self.wfile.write(b"--frame\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                            self.wfile.write(data)
                            self.wfile.write(b"\r\n")
                            time.sleep(0.066)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self.send_response(301)
                    self.send_header("Location", "/stream")
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("0.0.0.0", port), Handler)
        server.daemon_threads = True
        print(f"[MJPEG] Video stream available at http://0.0.0.0:{port}/stream")
        server.serve_forever()
