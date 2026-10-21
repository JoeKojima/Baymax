"""Conservative residual-echo subtraction and non-blocking text diagnostics."""
import re
import threading
import time

import numpy as np


class ResidualEcho:
    """Find a delayed linear speaker component; subtract that component only.

    Unexplained audio is retained. Correlation is evidence, not speaker identity.
    The render callback only appends PCM; analysis stays on the capture thread.
    """
    def __init__(self, rate=48000, clock=time.monotonic, enabled=True):
        self.rate, self.clock = rate, clock
        self.enabled = enabled
        self.step = max(1, rate // 8000)
        self.window = (rate // 25 // self.step) * self.step  # 40 ms
        self.maximum = int(rate * .6) * 2
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.history = bytearray()
            self.updated = float('-inf')
        self.capture = np.empty(0, dtype=np.float64)
        self.previous_delay = None
        self.matches = 0
        self.metrics = {'state': 'idle', 'correlation': 0.0}

    def rendered(self, pcm):
        if not self.enabled:
            return
        with self.lock:
            self.history.extend(pcm)
            if len(self.history) > self.maximum:
                del self.history[:-self.maximum]
            self.updated = self.clock()

    def process(self, samples, active):
        if not self.enabled:
            self.metrics = {'state': 'disabled; WebRTC retained', 'correlation': 0.0}
            return samples
        if not active or not len(samples):
            self.capture = np.empty(0, dtype=np.float64)
            self.previous_delay = None
            self.matches = 0
            self.metrics = {'state': 'idle', 'correlation': 0.0}
            return samples
        self.capture = np.concatenate((self.capture, samples.astype(np.float64)))[-self.window:]
        with self.lock:
            pcm = bytes(self.history)
            age = self.clock() - self.updated
        reference = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        reference = reference[len(reference) % self.step:]
        if len(self.capture) < self.window or len(reference) < self.window or age > .6:
            self.metrics = {'state': 'insufficient reference', 'correlation': 0.0}
            self.matches = 0
            return samples
        # Downsample only the delay search; refine and subtract at full rate.
        near = self.capture.reshape(-1, self.step).mean(axis=1)
        far = reference.reshape(-1, self.step).mean(axis=1)
        near = near - near.mean()
        power = float(near @ near)
        if power < len(near) * 4:  # Do not fit quantization-level silence.
            self.metrics = {'state': 'quiet', 'correlation': 0.0}
            self.matches = 0
            return samples
        size = 1 << (len(far) + len(near) - 2).bit_length()
        convolution = np.fft.irfft(np.fft.rfft(far, size) * np.fft.rfft(near[::-1], size), size)
        numerator = convolution[len(near)-1:len(far)]
        sums = np.concatenate(([0.0], np.cumsum(far)))
        squares = np.concatenate(([0.0], np.cumsum(far * far)))
        count = len(near)
        energy = squares[count:] - squares[:-count] - (sums[count:] - sums[:-count]) ** 2 / count
        scores = np.abs(numerator) / np.sqrt(np.maximum(power * energy, 1e-12))
        scores[energy < count * 4] = 0
        start = int(np.argmax(scores)) * self.step
        best = None
        centered = self.capture - self.capture.mean()
        near_power = float(centered @ centered)
        for offset in range(max(0, start-self.step), min(len(reference)-self.window, start+self.step)+1):
            segment = reference[offset:offset+self.window]
            segment = segment - segment.mean()
            far_power = float(segment @ segment)
            if far_power < self.window * 4:
                continue
            dot = float(centered @ segment)
            correlation = abs(dot) / np.sqrt(max(near_power * far_power, 1e-12))
            if best is None or correlation > best[0]:
                best = (correlation, dot / far_power, offset, segment)
        if best is None:
            self.metrics = {'state': 'uncertain; speech retained', 'correlation': 0.0}
            self.matches = 0
            return samples
        correlation, gain, start, segment = best
        delay_ms = (len(reference) - start - self.window) * 1000 / self.rate
        stable = self.previous_delay is not None and abs(delay_ms - self.previous_delay) <= 40
        self.matches = self.matches + 1 if correlation >= .85 and stable else int(correlation >= .85)
        self.previous_delay = delay_ms
        independent = float(np.sqrt(max(0.0, 1 - min(1.0, correlation) ** 2)))
        self.metrics = {'state': 'uncertain; speech retained', 'correlation': round(float(correlation), 3),
                        'estimated_delay_ms': round(delay_ms, 1),
                        'unexplained_fraction': round(independent, 3)}
        if self.matches < 2:
            return samples
        # No noise/speech gate: retain unexplained (including quiet user) audio.
        # Leave 10% of the fitted component to limit over-subtraction/model error.
        result = samples.astype(np.float64).copy()
        length = min(len(result), self.window)
        result[-length:] -= .9 * gain * segment[-length:]
        self.metrics['state'] = 'correlated echo reduced; unexplained audio retained'
        return np.clip(result, -32768, 32767).astype(np.int16)


def transcript_overlap(user_text, robot_texts):
    """Warn on a substantial repeated prefix; never discard or rewrite speech."""
    tokens = lambda text: re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.casefold().replace('’', "'"))
    user = tokens(user_text[:512])[:12]
    robot = tokens(' '.join(robot_texts)[-2048:])
    matched = 0
    if len(user) >= 3:
        for start in range(len(robot)):
            count = 0
            for first, second in zip(user, robot[start:]):
                if first != second:
                    break
                count += 1
            matched = max(matched, count)
    ratio = matched / len(user) if user else 0
    distinctive = sum(len(word) for word in user[:matched]) + max(0, matched-1) >= 20
    return {'suspected': matched >= 3 and distinctive and ratio >= .8,
            'matched_words': matched, 'prefix_overlap': round(ratio, 3)}
