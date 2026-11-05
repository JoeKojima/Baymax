"""Run a camera model (MediaPipe pose or face landmarks) in its own process.

In Ember's main process MediaPipe inference held Python's GIL, so the microphone
callback and the Gemini connection waited behind every frame: mic overflows and
replies up to a minute late on the Orange Pi. A child process has its own GIL and
cores. Only frames go in and plain landmark tuples come back over its pipes.

    model = Remote(FaceLandmarks)      # any picklable factory with .detect(frame)
    landmarks = model.detect(frame)    # list[Point] or None, as the local model

Run as `python -m features.pose_detection.vision_process`; Remote starts it, nothing else should.
"""
import collections
import pickle
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
Point = collections.namedtuple("Point", "x y z visibility presence")
Sample = collections.namedtuple("Sample", "frame landmarks captured_at finished_at")


class LatestVision:
    """One inference in flight and one replaceable pending frame; submit never waits.

    Frames must not be modified after submission. The camera owns capture; this
    worker owns model startup, inference and cleanup, including startup failures.
    """

    def __init__(self, factory):
        self.factory = factory
        self.condition = threading.Condition()
        self.pending = None
        self.result = None
        self.stopped = False
        self.error = None
        self.dropped = 0
        self.inference_ms = None
        self.model = None
        self.thread = threading.Thread(target=self._run, name="ember-pose", daemon=True)
        self.thread.start()

    def submit(self, frame, captured_at):
        with self.condition:
            if self.stopped or self.error is not None:
                return
            if self.pending is not None:
                self.dropped += 1
            self.pending = (frame, captured_at)
            self.condition.notify()

    def poll(self):
        """Consume a completed sample once, so old poses cannot confirm a fall."""
        with self.condition:
            result, self.result = self.result, None
            return result

    def _run(self):
        model = None
        try:
            model = self.factory()
            with self.condition:
                self.model = model
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or self.pending is not None)
                    if self.stopped:
                        return
                    (frame, captured_at), self.pending = self.pending, None
                started = time.monotonic()
                landmarks = model.detect(frame)
                finished = time.monotonic()
                with self.condition:
                    if self.stopped:
                        return
                    self.inference_ms = (finished - started) * 1000
                    self.result = Sample(frame, landmarks, captured_at, finished)
                    self.condition.notify_all()
        except Exception as error:
            with self.condition:
                self.error = f"{type(error).__name__}: {error}"
                self.pending = None
        finally:
            if model is not None:
                try:
                    model.close()
                except Exception as error:
                    with self.condition:
                        self.error = self.error or f"{type(error).__name__}: {error}"

    def close(self):
        with self.condition:
            self.stopped = True
            self.pending = None
            self.result = None
            self.condition.notify()
            model = self.model
        # Interrupt pipe I/O if a native model is stuck during shutdown.
        if model is not None and hasattr(model, "cancel"):
            model.cancel()
        self.thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def points(landmarks):
    """MediaPipe landmarks as plain tuples (x/y/z/visibility read like the originals)."""
    if landmarks is None:
        return None
    return [Point(p.x, p.y, p.z, p.visibility or 0.0, p.presence or 0.0) for p in landmarks]


class Remote:
    """`factory()` built and run in a child process, with the same detect(frame) call."""

    def __init__(self, factory, timeout=5.0):
        self.lock = threading.Lock()
        self.timeout = timeout
        self.child = subprocess.Popen([sys.executable, "-m", "features.pose_detection.vision_process"], cwd=ROOT,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self._call(factory, timeout=60.0)
        except Exception:
            self.close()
            raise

    def _call(self, item, timeout=None):
        with self.lock:
            expired = threading.Event()

            def expire():
                expired.set()
                self.cancel()

            timer = threading.Timer(self.timeout if timeout is None else timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                pickle.dump(item, self.child.stdin, protocol=pickle.HIGHEST_PROTOCOL)
                self.child.stdin.flush()
                kind, value = pickle.load(self.child.stdout)
            except (EOFError, OSError, pickle.UnpicklingError) as error:
                if expired.is_set():
                    raise RuntimeError("vision inference timed out") from error
                raise RuntimeError(f"vision process stopped ({error or 'exited'})") from error
            finally:
                timer.cancel()
        if kind == "error":
            raise RuntimeError(value)
        return value

    def detect(self, frame):
        return self._call(frame)

    def cancel(self):
        if self.child.poll() is None:
            self.child.kill()

    def close(self):
        try:
            self.child.stdin.close()  # The child exits at end of input.
        except OSError:
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=5)
        self.child.stdout.close()

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
    # Run the imported module, not __main__, so Point pickles as features.pose_detection.vision_process.Point.
    from features.pose_detection.vision_process import main as serve
    serve()
