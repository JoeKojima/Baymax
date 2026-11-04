"""Run a camera model (MediaPipe pose or face landmarks) in its own process.

In Ember's main process MediaPipe inference held Python's GIL, so the microphone
callback and the Gemini connection waited behind every frame: mic overflows and
replies up to a minute late on the Orange Pi. A child process has its own GIL and
cores. Only frames go in and plain landmark tuples come back over its pipes.

    model = Remote(FaceLandmarks)      # any picklable factory with .detect(frame)
    landmarks = model.detect(frame)    # list[Point] or None, as the local model

Run as `python -m features.vision_process`; Remote starts it, nothing else should.
"""
import collections
import pickle
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
Point = collections.namedtuple("Point", "x y z visibility presence")


def points(landmarks):
    """MediaPipe landmarks as plain tuples (x/y/z/visibility read like the originals)."""
    if landmarks is None:
        return None
    return [Point(p.x, p.y, p.z, p.visibility or 0.0, p.presence or 0.0) for p in landmarks]


class Remote:
    """`factory()` built and run in a child process, with the same detect(frame) call."""

    def __init__(self, factory):
        self.lock = threading.Lock()
        self.child = subprocess.Popen([sys.executable, "-m", "features.vision_process"], cwd=ROOT,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self._call(factory)
        except Exception:
            self.close()
            raise

    def _call(self, item):
        with self.lock:
            try:
                pickle.dump(item, self.child.stdin, protocol=pickle.HIGHEST_PROTOCOL)
                self.child.stdin.flush()
                kind, value = pickle.load(self.child.stdout)
            except (EOFError, OSError, pickle.UnpicklingError) as error:
                raise RuntimeError(f"vision process stopped ({error or 'exited'})") from error
        if kind == "error":
            raise RuntimeError(value)
        return value

    def detect(self, frame):
        return self._call(frame)

    def close(self):
        try:
            self.child.stdin.close()  # The child exits at end of input.
        except OSError:
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    requests, replies = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # Model or library prints must not corrupt the reply pipe.

    def reply(kind, value):
        pickle.dump((kind, value), replies, protocol=pickle.HIGHEST_PROTOCOL)
        replies.flush()

    try:
        model = pickle.load(requests)()
    except Exception as error:
        reply("error", f"{type(error).__name__}: {error}")
        return
    reply("ok", None)
    while True:
        try:
            frame = pickle.load(requests)
        except EOFError:
            return
        try:
            reply("ok", points(model.detect(frame)))
        except Exception as error:
            reply("error", f"{type(error).__name__}: {error}")


if __name__ == "__main__":
    # Run the imported module, not __main__, so Point pickles as features.vision_process.Point.
    from features.vision_process import main as serve
    serve()
