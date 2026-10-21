"""Opt-in desktop WebRTC audio cleanup, using Ember's existing playback reference."""
import copy
import os
import threading
import time

import numpy as np
import runtime_monitor as monitor
from .echo import ResidualEcho


def enabled():
    return os.getenv('BAYMAX_AUDIO_ENHANCEMENT', '0') == '1'


def mute_during_reply():
    # Without interruptions, mic audio heard during a reply can only become a
    # later echo "user" turn, so default to silence while the speaker is audible.
    interruptions = os.getenv('BAYMAX_INTERRUPTION_ENABLED', '1') != '0'
    return os.getenv('BAYMAX_MUTE_MIC_DURING_REPLY', '0' if interruptions else '1') == '1'


def extend_config(config):
    interruptions = os.getenv('BAYMAX_INTERRUPTION_ENABLED', '1') != '0'
    if not enabled():
        if interruptions:
            return config
        result = copy.deepcopy(config)
        result.setdefault('realtime_input_config', {})['activity_handling'] = 'NO_INTERRUPTION'
        return result
    sensitivity = os.getenv('BAYMAX_SPEECH_START_SENSITIVITY', 'low').strip().lower()
    if sensitivity not in ('low', 'high'):
        print('[AUDIO] Invalid speech-start sensitivity; using low.', flush=True)
        sensitivity = 'low'
    result = copy.deepcopy(config)
    realtime = result.setdefault('realtime_input_config', {})
    realtime['automatic_activity_detection'] = {
        'disabled': False, 'start_of_speech_sensitivity': 'START_SENSITIVITY_' + sensitivity.upper(),
        'end_of_speech_sensitivity': 'END_SENSITIVITY_LOW',
        'prefix_padding_ms': 0, 'silence_duration_ms': 500}
    realtime['activity_handling'] = 'START_OF_ACTIVITY_INTERRUPTS' if interruptions else 'NO_INTERRUPTION'
    return result


class FanFilter:
    """4th-order high-pass below the voice band; filter state carries across callbacks.

    The LattePanda fan measured 29 Hz (52% of noise power) with 88/193 Hz tones;
    95% of its noise is under 211 Hz while speech intelligibility sits at 300-3400 Hz.
    """
    def __init__(self, rate, cutoff_hz):
        from scipy.signal import butter, sosfilt
        self.sosfilt = sosfilt
        self.cutoff = cutoff_hz
        self.sos = butter(4, cutoff_hz, btype='highpass', fs=rate, output='sos')
        self.reset()

    def reset(self):
        self.state = np.zeros((self.sos.shape[0], 2))

    def __call__(self, samples):
        out, self.state = self.sosfilt(self.sos, samples.astype(np.float64), zi=self.state)
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def fan_filter_hz():
    try:
        return float(os.getenv('BAYMAX_FAN_FILTER_HZ', '200'))
    except ValueError:
        print('[AUDIO] Invalid BAYMAX_FAN_FILTER_HZ; using 200.', flush=True)
        return 200.0


class InputEnhancer:
    """DSP runs only on the capture thread; render callback only queues PCM.

    Process complete 10ms frames, retaining leftovers across 1024-sample callbacks.
    A fixed 10ms output delay avoids padding/discarding tails on every callback.
    """
    def __init__(self, rate=48000, processor=None, ns_level=None, clock=time.monotonic, fan_filter_hz=0):
        self.fan_filter = None
        if fan_filter_hz > 0:
            try:
                self.fan_filter = FanFilter(rate, fan_filter_hz)
            except Exception as error:
                print(f'[AUDIO] Fan filter unavailable ({error}); continuing without it.', flush=True)
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
        self.residual = ResidualEcho(rate, clock, enabled=os.getenv('BAYMAX_RESIDUAL_ECHO_ENABLED', '1') == '1')
        self.mute_during_reply = mute_during_reply()
        self.clock = clock
        self.rate = rate
        self.frame = rate // 100
        self.lock = threading.Lock()
        self.render_buffer = bytearray()
        self.input_latency_ms = 0
        self.output_latency_ms = 0
        self.failed = False
        self.metrics = {}
        self.speaker_until = float('-inf')
        self.reset()

    @property
    def full_duplex(self):
        return not self.failed

    def reset(self):
        self.processor.reset()
        self.residual.reset()
        self.pending = np.empty(0, dtype=np.int16)
        self.far_pending = np.empty(0, dtype=np.int16)
        self.output = np.zeros(self.frame, dtype=np.int16)
        with self.lock:
            self.render_buffer.clear()
            self.speaker_until = float('-inf')
        self.failed = False
        if self.fan_filter is not None:
            self.fan_filter.reset()

    def rendered(self, pcm, timing=None):
        # Feed exactly what reached the speaker, including silence. Never capture
        # a second microphone/loopback stream or pass unplayed queued TTS as AEC reference.
        current = getattr(timing, 'currentTime', 0)
        dac = getattr(timing, 'outputBufferDacTime', 0)
        if current > 0 and dac > 0:
            self.output_latency_ms = min(500, max(0, (dac - current) * 1000))
        active = bool(np.any(np.frombuffer(pcm, dtype=np.int16)))
        monitor.sensor_alive('speaker')  # Output callbacks run continuously, silence included.
        with self.lock:
            if active:
                # Include queued device audio and a bounded acoustic echo tail.
                end = self.clock() + self.output_latency_ms / 1000 + len(pcm) / (self.rate * 2)
                self.speaker_until = max(self.speaker_until, end + 0.25)
            self.render_buffer.extend(pcm)
            maximum = self.rate * 2 // 4  # 250ms bound: discard stale reference on a stall.
            if len(self.render_buffer) > maximum:
                del self.render_buffer[:-maximum]
        self.residual.rendered(pcm)

    def speaker_active(self):
        with self.lock:
            return self.clock() < self.speaker_until

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

    def process(self, samples, timing=None):
        if np.any(samples):  # Exact digital silence means no working capture device.
            monitor.sensor_alive('microphone')
        if self.fan_filter is not None:
            try:
                samples = self.fan_filter(samples)
            except Exception:
                self.fan_filter = None
                print('[AUDIO] Fan filter disabled after error; cleanup continues.', flush=True)
        if self.failed:
            return self._fallback(samples)
        try:
            current = getattr(timing, 'currentTime', 0)
            adc = getattr(timing, 'inputBufferAdcTime', 0)
            if current > 0 and adc > 0:
                self.input_latency_ms = min(500, max(0, (current - adc) * 1000))
            echo_guard = self.speaker_active()
            self.pending = np.concatenate((self.pending, samples))
            self.far_pending = np.concatenate((self.far_pending, self._reference(len(samples))))
            self.processor.stream_delay_ms = int(min(500, max(0, self.input_latency_ms + self.output_latency_ms)))
            chunks = [self.output]
            count = (len(self.pending) // self.frame) * self.frame
            for offset in range(0, count, self.frame):
                clean = self.processor.process(self.pending[offset:offset+self.frame],
                                               self.far_pending[offset:offset+self.frame])
                if echo_guard:
                    # AGC follows AEC: undo its positive gain while the speaker
                    # is audible, so residual robot speech is not boosted again.
                    gain = max(0.0, float(self.processor.gain_db))
                    clean = (clean.astype(np.float32) * 10 ** (-gain / 20)).astype(np.int16)
                chunks.append(clean)
            self.pending = self.pending[count:]
            self.far_pending = self.far_pending[count:]
            ready = np.concatenate(chunks)
            result, self.output = ready[:len(samples)].copy(), ready[len(samples):]
            try:
                result = self.residual.process(result, echo_guard)
            except Exception:
                self.residual.enabled = False
                self.residual.metrics = {'state': 'disabled after error; WebRTC retained', 'correlation': 0.0}
                print('[AUDIO] Residual detector disabled after error; WebRTC continues.', flush=True)
            muted = self.mute_during_reply and echo_guard
            if muted:
                # AEC above still adapts on this audio; Gemini only gets silence.
                result = np.zeros_like(result)
            self.metrics = {'state': 'WebRTC echo cancellation + noise suppression + gain control',
                            'raw': self.levels(samples), 'clean': self.levels(result),
                            'speech_probability': round(float(self.processor.speech_probability), 3),
                            'gain_db': round(min(0.0, float(self.processor.gain_db)) if echo_guard else float(self.processor.gain_db), 1),
                            'agc_gain_db': round(float(self.processor.gain_db), 1),
                            'echo_guard': echo_guard,
                            'mic_muted_during_reply': muted,
                            'fan_filter_hz': self.fan_filter.cutoff if self.fan_filter is not None else 0,
                            'residual_echo': dict(self.residual.metrics),
                            'aec_delay_ms': self.processor.stream_delay_ms,
                            'input_latency_ms': round(self.input_latency_ms, 1),
                            'output_latency_ms': round(self.output_latency_ms, 1),
                            'full_duplex': True}
            return result
        except Exception:
            # Preserve microphone capture on DSP failure; restore half-duplex to
            # avoid feeding speaker echo back into Gemini. Expose the failure.
            self.failed = True
            return self._fallback(samples)

    def _fallback(self, samples):
        active = self.speaker_active()
        self.metrics = {'state': 'Audio cleanup failed; raw input / half-duplex fallback',
                        'full_duplex': False, 'echo_guard': active}
        # Playback may already have left the Python queue but still be audible.
        # Only failed DSP uses this mute; healthy AEC retains near-end speech.
        return np.zeros_like(samples) if active else samples


def create(rate=48000):
    return InputEnhancer(rate, fan_filter_hz=fan_filter_hz()) if enabled() else None
