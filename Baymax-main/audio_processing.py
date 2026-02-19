import numpy as np
import sounddevice as sd

# Use 24 kHz for BOTH input and output so we match what the Realtime API is sending
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 24000 * 0.03 = 720 samples


class MicrophoneStream:
    """Yields 24 kHz mono PCM16 chunks of ~30 ms (as bytes)."""

    def __init__(self):
        # dtype=int16 ensures PCM16 data; blocksize enforces ~30 ms frames
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAME_SIZE,
        )
        self.frame_size = FRAME_SIZE
        self.stream.start()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        # Read exactly FRAME_SIZE frames; overflow flag ignored like PyAudio's exception_on_overflow=False
        frames, _overflowed = self.stream.read(FRAME_SIZE)
        # frames is a (FRAME_SIZE, CHANNELS) int16 ndarray → bytes (PCM little-endian)
        return frames.tobytes()

    # Optional context-manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        try:
            self.stream.stop()
        finally:
            self.stream.close()


class SpeakerStream:
    """Plays 24 kHz mono PCM16 audio (bytes) that we get from Realtime."""

    def __init__(self):
        self.stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=FRAME_SIZE,
        )
        self.stream.start()

    def play(self, pcm_bytes: bytes):
        if not pcm_bytes:
            return
        # Convert bytes → int16 ndarray shaped (N, 1) and write
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        if arr.size == 0:
            return
        arr = arr.reshape(-1, CHANNELS)
        self.stream.write(arr)

    # Optional context-manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        try:
            self.stream.stop()
        finally:
            self.stream.close()
