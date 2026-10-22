"""Opt-in desktop WebRTC audio cleanup, using Ember's existing playback reference."""
import copy
import os
import threading

import numpy as np


def enabled():
    return os.getenv('BAYMAX_AUDIO_ENHANCEMENT', '0') == '1'


def extend_config(config):
    if not enabled():
        return config
    result = copy.deepcopy(config)
    realtime = result.setdefault('realtime_input_config', {})
    realtime['automatic_activity_detection'] = {
        'disabled': False, 'start_of_speech_sensitivity': 'START_SENSITIVITY_HIGH',
        'end_of_speech_sensitivity': 'END_SENSITIVITY_LOW',
        'prefix_padding_ms': 0, 'silence_duration_ms': 500}
    realtime['activity_handling'] = 'START_OF_ACTIVITY_INTERRUPTS'
    return result


class InputEnhancer:
    """DSP runs only on the capture thread; render callback only queues PCM.

    Process complete 10ms frames, retaining leftovers across 1024-sample callbacks.
    A fixed 10ms output delay avoids padding/discarding tails on every callback.
    """
    def __init__(self, rate=48000, processor=None, ns_level=None):
        if processor is None:
            from pywebrtc_audio import AudioProcessor
            level = int(os.getenv('BAYMAX_NOISE_SUPPRESSION_LEVEL', '1')) if ns_level is None else ns_level
            if level not in range(4):
                raise ValueError('Noise suppression level must be between 0 and 3.')
            processor = AudioProcessor(sample_rate=rate, echo_cancellation=True,
                                       noise_suppression=True, high_pass_filter=True,
                                       auto_gain_control=True, ns_level=level,
                                       agc_max_gain_db=18, stream_delay_ms=50)
        self.processor = processor
        self.rate = rate
        self.frame = rate // 100
        self.lock = threading.Lock()
        self.render_buffer = bytearray()
        self.input_latency_ms = 0
        self.output_latency_ms = 0
        self.failed = False
        self.metrics = {}
        self.reset()

    @property
    def full_duplex(self):
        return not self.failed

    def reset(self):
        self.processor.reset()
        self.pending = np.empty(0, dtype=np.int16)
        self.far_pending = np.empty(0, dtype=np.int16)
        self.output = np.zeros(self.frame, dtype=np.int16)
        with self.lock:
            self.render_buffer.clear()
        self.failed = False

    def rendered(self, pcm):
        # Feed exactly what reached the speaker, including silence. Never capture
        # a second microphone/loopback stream or pass unplayed queued TTS as AEC reference.
        with self.lock:
            self.render_buffer.extend(pcm)
            maximum = self.rate * 2 // 4  # 250ms bound: discard stale reference on a stall.
            if len(self.render_buffer) > maximum:
                del self.render_buffer[:-maximum]

    def _reference(self, samples):
        wanted = samples * 2
        with self.lock:
            pcm = bytes(self.render_buffer[:wanted])
            del self.render_buffer[:wanted]
        return np.frombuffer(pcm + b'\0' * (wanted - len(pcm)), dtype=np.int16)

    @staticmethod
    def levels(samples):
        values = samples.astype(np.float32)
        rms = float(np.sqrt(np.mean(values * values))) if values.size else 0
        peak = float(np.max(np.abs(values))) if values.size else 0
        return {'rms_dbfs': round(20 * np.log10(max(rms, 1e-3) / 32768), 1),
                'peak_dbfs': round(20 * np.log10(max(peak, 1e-3) / 32768), 1),
                'clipped_percent': round(float(np.mean(np.abs(values) >= 32760)) * 100, 2) if values.size else 0}

    def process(self, samples):
        if self.failed:
            return samples
        try:
            self.pending = np.concatenate((self.pending, samples))
            self.far_pending = np.concatenate((self.far_pending, self._reference(len(samples))))
            self.processor.stream_delay_ms = int(min(500, max(0, self.input_latency_ms + self.output_latency_ms)))
            chunks = [self.output]
            count = (len(self.pending) // self.frame) * self.frame
            for offset in range(0, count, self.frame):
                chunks.append(self.processor.process(self.pending[offset:offset+self.frame],
                                                     self.far_pending[offset:offset+self.frame]))
            self.pending = self.pending[count:]
            self.far_pending = self.far_pending[count:]
            ready = np.concatenate(chunks)
            result, self.output = ready[:len(samples)].copy(), ready[len(samples):]
            self.metrics = {'state': 'WebRTC echo cancellation + noise suppression + gain control',
                            'raw': self.levels(samples), 'clean': self.levels(result),
                            'speech_probability': round(float(self.processor.speech_probability), 3),
                            'gain_db': round(float(self.processor.gain_db), 1), 'full_duplex': True}
            return result
        except Exception:
            # Preserve microphone capture on DSP failure; restore half-duplex to
            # avoid feeding speaker echo back into Gemini. Expose the failure.
            self.failed = True
            self.metrics = {'state': 'Audio cleanup failed; raw input / half-duplex fallback', 'full_duplex': False}
            return samples


def create(rate=48000):
    return InputEnhancer(rate) if enabled() else None
