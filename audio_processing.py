# audio_processing.py
import pyaudio

# Use 24 kHz for BOTH input and output so we match what the Realtime API is sending
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 24000 * 0.03 = 720 samples


class MicrophoneStream:
    """Yields 24 kHz mono PCM16 chunks of ~30 ms."""
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=FRAME_SIZE,
        )

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return self.stream.read(FRAME_SIZE, exception_on_overflow=False)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()


class SpeakerStream:
    """Plays 24 kHz mono PCM16 audio that we get from Realtime."""
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,  # 24k → correct pitch
            output=True,
        )

    def play(self, pcm_bytes: bytes):
        if pcm_bytes:
            self.stream.write(pcm_bytes)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()