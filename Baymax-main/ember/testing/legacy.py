"""Load the unchanged legacy runtime (realtime_gemini_8.py) on virtual devices.

Its import selects audio devices, queries sounddevice and builds the audio
cleanup, so it cannot normally be imported without hardware. Here
`sounddevice` is a VirtualSoundDevice and the PC hardware profile is used (so
device selection goes through pc_hardware, as on desktops). Each call returns a
fresh module with fresh globals. The file itself is never modified: it is the
reference the new core is compared against, sample for sample.
"""
import contextlib
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "realtime_gemini_8.py"


class Posts:
    """Records _notify_webapp posts instead of contacting the dashboard."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))


@contextlib.contextmanager
def _sounddevice(sd):
    previous = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = sd
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = previous


def load(sd, path=LEGACY, env=None, name="legacy_realtime_gemini_8"):
    """Execute the legacy file as a module bound to `sd`. Returns the module."""
    settings = {"BAYMAX_HARDWARE_PROFILE": "pc", **(env or {})}
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with _sounddevice(sd), patch.dict(os.environ, settings), patch("dotenv.load_dotenv", lambda *a, **k: False):
        spec.loader.exec_module(module)
    module.http_requests = Posts()
    return module


def use_client(module, client):
    """Make the module's genai.Client(...) return `client` (a FakeClient)."""
    module.genai = SimpleNamespace(Client=lambda **kwargs: client)


class RealCameraBlocked(RuntimeError):
    pass


def _no_real_camera(*args, **kwargs):
    raise RealCameraBlocked("a test tried to open a real camera")


@contextlib.contextmanager
def camera(capture):
    """While active, the camera lookup yields `capture` (a VirtualCamera) or nothing.

    The desktop profile is forced (v8 reads it on the camera thread) and
    cv2.VideoCapture raises, so no real camera can ever be probed.
    """
    import cv2
    import pc_hardware
    with patch.dict(os.environ, {"BAYMAX_HARDWARE_PROFILE": "pc"}), \
            patch.object(cv2, "VideoCapture", _no_real_camera), \
            patch.object(pc_hardware, "camera_candidates", lambda: [0] if capture is not None else []), \
            patch.object(pc_hardware, "open_camera", lambda cv2, index: capture):
        yield
