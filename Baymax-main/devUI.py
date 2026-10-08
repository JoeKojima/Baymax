"""Local devUI: configure key and PC devices, without starting Gemini."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
import urllib.request

import sounddevice as sd
from dev_settings import public_configuration, save_configuration
from pc_hardware import open_camera
from runtime_monitor import read_status

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        # Windows otherwise permits multiple development servers on one port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def audio_catalog():
    # PortAudio caches devices at initialization. A fresh process sees hot-plugged
    # hardware without resetting audio streams in another running application.
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--audio-catalog"],
                            capture_output=True, text=True, check=True, timeout=20)
    return json.loads(result.stdout)


def _audio_catalog_snapshot():
    hosts = sd.query_hostapis()
    devices = sd.query_devices()
    result = {"microphones": [], "speakers": []}
    for index, device in enumerate(devices):
        # WASAPI exposes the full Windows endpoint name without duplicate driver entries.
        if sys.platform == "win32" and hosts[device["hostapi"]]["name"] != "Windows WASAPI":
            continue
        entry = {"index": index, "name": device["name"], "hostapi": hosts[device["hostapi"]]["name"]}
        if device["max_input_channels"] > 0:
            result["microphones"].append(entry)
        if device["max_output_channels"] > 0:
            result["speakers"].append(entry)
    return result


def camera_catalog():
    os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
    import cv2
    cameras = []
    names = []
    if sys.platform == "win32":
        # COM enumeration must be initialized in this worker thread.
        import comtypes
        from pygrabber.dshow_graph import FilterGraph
        comtypes.CoInitialize()
        try:
            names = FilterGraph().get_input_devices()
        finally:
            comtypes.CoUninitialize()
    for index in range(len(names) if sys.platform == "win32" else 10):
        camera = open_camera(cv2, index)
        try:
            if camera.isOpened():
                ready, frame = camera.read()
                if ready and frame is not None:
                    height, width = frame.shape[:2]
                    cameras.append({"index": index, "name": names[index] if names else f"Camera {index}",
                                    "resolution": f"{width} × {height}"})
        finally:
            camera.release()
    return cameras


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, code, data, content_type="application/json"):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}",
                                             f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.valid_host():
            self.respond(403, {"error": "Local host required."})
        elif self.path in ("/", "/monitor", "/emberdevUI"):
            page = (ROOT / ("static/devUI.html" if self.path == "/" else "static/emberdevUI.html")).read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            self.respond(200, page.encode(), "text/html; charset=utf-8")
        else:
            self.respond(404, {"error": "Not found."})

    def do_POST(self):
        origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if (not self.valid_host() or self.headers.get("Origin") not in origins or
                not secrets.compare_digest(self.headers.get("X-devUI-Token", ""), self.server.token)):
            self.respond(403, {"error": "Refresh the local devUI page and try again."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16384:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/status":
                self.respond(200, public_configuration(ENV))
            elif self.path == "/api/runtime":
                self.respond(200, read_status())
            elif self.path == "/api/devices":
                # Only one hardware scan at a time, including requests from multiple tabs.
                with self.server.settings_lock:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        audio = pool.submit(audio_catalog)
                        camera = pool.submit(camera_catalog)
                        catalog = audio.result()
                        catalog["cameras"] = camera.result()
                    self.server.catalog = catalog
                self.respond(200, catalog)
            elif self.path == "/api/save":
                with self.server.settings_lock:
                    if self.server.catalog is None:
                        raise ValueError("Wait for device discovery before saving.")
                    current_audio = audio_catalog()
                    current_audio["cameras"] = self.server.catalog["cameras"]
                    self.respond(200, save_configuration(ENV, body, current_audio))
            elif self.path == "/api/test":
                kind, device = body.get("kind"), body.get("device", "")
                if kind not in ("microphone", "speaker", "camera") or not isinstance(device, str):
                    raise ValueError("Invalid test request.")
                with self.server.settings_lock:
                    result = subprocess.run([sys.executable, str(ROOT / "device_test.py"), kind, device],
                                            capture_output=True, text=True, timeout=20)
                    data = json.loads(result.stdout)
                self.respond(400 if result.returncode else 200, data)
            else:
                self.respond(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})
        except Exception:
            self.respond(500, {"error": "Could not read devices or save settings. Close apps using the camera, check file permissions, and refresh."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    try:
        server = LocalServer(("127.0.0.1", args.port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                existing = response.read(65536)
            if b"<title>Ember devUI</title>" in existing:
                if not args.no_browser:
                    webbrowser.open(url + ("/monitor" if args.monitor else ""))
                parser.exit(0, f"devUI is already running at {url}\n")
        except OSError:
            pass
        parser.exit(1, "devUI's port is busy. Open the existing devUI or use --port 8767.\n")
    server.token = secrets.token_urlsafe(32)
    server.catalog = None
    server.settings_lock = threading.Lock()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"devUI: {url}\nSave settings here, then start/restart Gemini. Ctrl+C closes devUI.", flush=True)
    if not args.no_browser:
        webbrowser.open(url + ("/monitor" if args.monitor else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--audio-catalog"]:
        print(json.dumps(_audio_catalog_snapshot()))
    else:
        main()
