"""One local 'hmm' per slow response, consumed by the existing speaker callback."""
import os
from pathlib import Path
import threading
import time
import wave

import numpy as np

ASSET = Path(__file__).with_name('assets') / 'hmm.wav'


class WaitingCue:
    def __init__(self, pcm, clock=time.monotonic):
        self.pcm, self.clock = pcm, clock
        self.lock = threading.Lock()
        self.waiting_since = None
        self.responding = False
        self.played = False
        self.offset = 0

    def event(self, event):
        with self.lock:
            if event in ('thinking', 'user_speech'):
                if not self.responding:
                    if self.waiting_since is None:
                        self.played = False
                    if event == 'user_speech' or self.waiting_since is None:
                        self.waiting_since = self.clock()
                    if event == 'user_speech':
                        self.offset = len(self.pcm) if self.played else 0
            else:
                self.responding = event == 'speaking'
                self.waiting_since = None
                self.offset = 0
                self.played = False

    def cancel(self):
        with self.lock:
            self.waiting_since = None
            self.offset = 0
            self.played = True

    def microphone(self, pcm, threshold):
        # Use cleaned near-end audio so the cue's own echo is not an interruption.
        values = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if len(values) and float(np.sqrt(np.mean(values * values))) > threshold:
            self.cancel()

    def render(self, reply, size):
        with self.lock:
            if reply:
                self.waiting_since = None
                self.responding = True
                self.offset = 0
                return reply  # Real reply bytes always win, without mixing/delay.
            if self.waiting_since is None or self.clock() - self.waiting_since <= 2:
                return b''
            if self.played and self.offset >= len(self.pcm):
                return b''
            self.played = True
            chunk = self.pcm[self.offset:self.offset + size]
            self.offset += len(chunk)
            return chunk


def create(echo_cancellation_ready):
    if os.getenv('BAYMAX_WAIT_CUE_ENABLED', '0') != '1':
        return None
    if not echo_cancellation_ready:
        print('[WAIT CUE] Disabled: working echo cancellation is required.', flush=True)
        return None
    try:
        with wave.open(str(ASSET), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (48000, 1, 2, 'NONE'):
                raise ValueError('expected 48 kHz mono 16-bit PCM')
            if not 0 < audio.getnframes() <= 48000 * 2:
                raise ValueError('cue must be between 0 and 2 seconds')
            pcm = audio.readframes(audio.getnframes())
            if len(pcm) != audio.getnframes() * 2:
                raise ValueError('truncated cue')
        return WaitingCue(pcm)
    except (OSError, ValueError, EOFError, wave.Error) as error:
        print(f'[WAIT CUE] Unavailable (conversation continues): {error}', flush=True)
        return None
