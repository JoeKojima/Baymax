"""Virtual microphone, speaker and camera with the sounddevice / OpenCV interface the core uses.

`VirtualSoundDevice` answers the subset of `sounddevice` the runtime calls
(query_devices, default, check_*_settings, InputStream, OutputStream,
_terminate/_initialize), so the unchanged legacy core and the new core both run
on it. The microphone plays queued PCM (silence or quiet room tone between
lines); the speaker records what the runtime rendered. Streams either run on
their own clock (`realtime=True`, like hardware) or advance only when a test
calls `step()`, which makes two runs of the same script comparable sample for
sample.

Nothing here opens a real device or the network.
"""
import collections
import threading
import time
from types import SimpleNamespace

import numpy as np

RATE = 48000


class Timing(SimpleNamespace):
    """The PortAudio callback time_info fields the audio cleanup reads."""


class MicSource:
    """PCM the virtual microphone 'hears': queued lines, then background."""

    def __init__(self, room_noise=0, seed=0):
        self.lock = threading.Lock()
        self.pending = collections.deque()
        self.room_noise = room_noise
        self.rng = np.random.default_rng(seed)

    def say(self, samples):
        samples = np.asarray(samples, dtype=np.int16).reshape(-1)
        with self.lock:
            self.pending.extend(samples.tolist())

    def busy(self):
        with self.lock:
            return bool(self.pending)

    def read(self, frames):
        with self.lock:
            count = min(frames, len(self.pending))
            head = [self.pending.popleft() for _ in range(count)]
        block = (self.rng.standard_normal(frames) * self.room_noise).astype(np.int16) if self.room_noise else \
            np.zeros(frames, dtype=np.int16)
        if head:
            block[:count] = np.clip(block[:count].astype(np.int32) + np.array(head, dtype=np.int32),
                                    -32768, 32767).astype(np.int16)
        return block


class SpeakerSink:
    """Everything the runtime rendered, bounded, with a 'voice heard' counter."""

    def __init__(self, keep_seconds=120):
        self.lock = threading.Lock()
        self.audio = bytearray()
        self.maximum = keep_seconds * RATE * 2
        self.blocks = 0
        self.voiced_blocks = 0

    def write(self, outdata):
        data = np.asarray(outdata, dtype=np.int16)
        mono = data[:, 0] if data.ndim == 2 else data
        with self.lock:
            self.audio.extend(mono.tobytes())
            if len(self.audio) > self.maximum:
                del self.audio[:-self.maximum]
            self.blocks += 1
            self.voiced_blocks += bool(np.any(mono))

    def take(self):
        with self.lock:
            data = bytes(self.audio)
            self.audio.clear()
            return data


class _Default:
    def __init__(self):
        self.device = [0, 1]
        self.extra_settings = None


class _Stream:
    latency = 0.01

    def __init__(self, backend, kind, samplerate, channels, dtype, blocksize, callback):
        if dtype != "int16":
            raise ValueError("virtual streams are int16 only")
        self.backend, self.kind, self.rate, self.channels = backend, kind, samplerate, channels
        self.blocksize, self.callback = blocksize, callback
        self.active = False
        self.thread = None
        self.clock = 0.0

    # sounddevice API
    def start(self):
        if self.active:
            return
        self.active = True
        self.backend._opened(self)
        if self.backend.realtime:
            self.thread = threading.Thread(target=self._run, name=f"virtual-{self.kind}", daemon=True)
            self.thread.start()

    def stop(self):
        self.active = False
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def close(self):
        self.stop()
        self.backend._closed(self)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # one block
    def tick(self):
        if not self.active:
            return
        frames = self.blocksize
        self.clock += frames / self.rate
        if self.kind == "input":
            mono = self.backend.microphone.read(frames)
            indata = np.repeat(mono[:, None], self.channels, axis=1) if self.channels > 1 else mono[:, None]
            self.callback(indata, frames, Timing(currentTime=self.clock, inputBufferAdcTime=self.clock - 0.005), None)
        else:
            outdata = np.zeros((frames, self.channels), dtype=np.int16)
            self.callback(outdata, frames, Timing(currentTime=self.clock, outputBufferDacTime=self.clock + 0.005), None)
            self.backend.speaker.write(outdata)

    def _run(self):
        period = self.blocksize / self.rate / self.backend.speed
        deadline = time.monotonic()
        while self.active:
            try:
                self.tick()
            except Exception as error:  # A PortAudio callback error stops that stream.
                print(f"[VIRTUAL] {self.kind} callback failed: {error}", flush=True)
                self.active = False
                return
            deadline += period
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()


class VirtualSoundDevice:
    """Drop-in for the `sounddevice` module, backed by a MicSource and a SpeakerSink."""

    def __init__(self, microphone=None, speaker=None, input_channels=2, realtime=True, speed=1.0):
        self.microphone = microphone or MicSource()
        self.speaker = speaker or SpeakerSink()
        self.realtime, self.speed = realtime, speed
        self.default = _Default()
        self.devices = [
            {"name": "Ember virtual microphone", "hostapi": 0, "max_input_channels": input_channels,
             "max_output_channels": 0, "default_samplerate": RATE},
            {"name": "Ember virtual speaker", "hostapi": 0, "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": RATE},
        ]
        self.streams = []
        self.resets = 0
        self.lock = threading.Lock()

    def query_devices(self, device=None, kind=None):
        if device is None:
            return [dict(d) for d in self.devices]
        return dict(self.devices[int(device)])

    def query_hostapis(self):
        return [{"name": "Ember virtual audio", "devices": [0, 1]}]

    def check_input_settings(self, **settings):
        return None

    def check_output_settings(self, **settings):
        return None

    def InputStream(self, samplerate, channels, dtype, blocksize, callback, **_):
        return _Stream(self, "input", samplerate, channels, dtype, blocksize, callback)

    def OutputStream(self, samplerate, channels, dtype, blocksize, callback, **_):
        return _Stream(self, "output", samplerate, channels, dtype, blocksize, callback)

    def _terminate(self):
        self.resets += 1

    def _initialize(self):
        return None

    def _opened(self, stream):
        with self.lock:
            self.streams.append(stream)

    def _closed(self, stream):
        with self.lock:
            if stream in self.streams:
                self.streams.remove(stream)

    def open_streams(self, kind=None):
        with self.lock:
            return [s for s in self.streams if s.active and (kind is None or s.kind == kind)]

    def step(self, blocks=1):
        """Manual clock: run each open output stream, then each input stream, `blocks` times."""
        for _ in range(blocks):
            for stream in self.open_streams("output") + self.open_streams("input"):
                stream.tick()


class VirtualCamera:
    """cv2.VideoCapture stand-in that returns the given BGR frames in a loop."""

    def __init__(self, frames=None, size=(640, 480)):
        width, height = size
        self.frames = list(frames) if frames else [np.full((height, width, 3), 128, dtype=np.uint8)]
        self.index = 0
        self.opened = True
        self.settings = {}
        self.reads = 0

    def isOpened(self):
        return self.opened

    def read(self):
        if not self.opened:
            return False, None
        frame = self.frames[self.index % len(self.frames)]
        self.index += 1
        self.reads += 1
        return True, frame.copy()

    def set(self, prop, value):
        self.settings[prop] = value
        return True

    def release(self):
        self.opened = False

    def show(self, frame):
        """Replace what the camera sees (e.g. a label card for a look-closely test)."""
        self.frames, self.index = [frame], 0
