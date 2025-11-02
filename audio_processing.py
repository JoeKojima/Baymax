"""Realtime audio I/O utilities for streaming with OpenAI Realtime."""
import pyaudio
import array
from typing import Iterator, Optional

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_FORMAT = pyaudio.paInt16
FRAME_MS = 30
CHUNK = int(SAMPLE_RATE * FRAME_MS / 1000)

class MicrophoneStream:
    """yields raw 16-bit mono PCM frames from the default input device."""
    def __init__(self, sample_rate: int = SAMPLE_RATE, chunk: int = CHUNK):
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=SAMPLE_FORMAT,
            channels=CHANNELS,
            rate=sample_rate,
            input=True,
            frames_per_buffer=chunk,
        )
        self.chunk = chunk

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        return self.stream.read(self.chunk, exception_on_overflow=False)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

class SpeakerStream:
    """plays raw 16-bit mono PCM frames to the default output device."""
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=SAMPLE_FORMAT,
            channels=CHANNELS,
            rate=sample_rate,
            output=True,
        )

    def play(self, pcm16_bytes: bytes):
        self.stream.write(pcm16_bytes)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

def mean_abs_amp_int16(frame_bytes: bytes) -> float:
    a = array.array('h', frame_bytes)
    if not a:
        return 0.0
    return sum(abs(x) for x in a) / len(a)
