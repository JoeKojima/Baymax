"""Opt-in native AEC check on generated signals; never opens audio devices.

Run from Baymax-main: python tests/aec_synthetic_probe.py
Requires the pinned pywebrtc-audio and numpy from requirements-pc.txt.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from pywebrtc_audio import AudioProcessor
from features.conversational_ability.processing import InputEnhancer


def speech_shaped(rng, count, rate, rms):
    spectrum = np.fft.rfft(rng.normal(size=count))
    frequencies = np.fft.rfftfreq(count, 1 / rate)
    spectrum[(frequencies < 150) | (frequencies > 3500)] = 0
    signal = np.fft.irfft(spectrum, n=count)
    return signal * rms / np.sqrt(np.mean(signal * signal))


def main():
    rate, count = 48000, 48000 * 10
    rng = np.random.default_rng(42)
    speaker = speech_shaped(rng, count, rate, 2500).astype(np.int16)
    delay = rate // 20
    echo = np.concatenate((np.zeros(delay), speaker[:-delay] * .4)).astype(np.int16)
    # Voiced, syllabic signal rather than stationary noise (which NS should remove).
    t = np.arange(count) / rate
    phase = 2 * np.pi * (180 * t + 3 * np.sin(2 * np.pi * .8 * t))
    user = sum(np.sin(k * phase) / k for k in range(1, 16))
    user *= (.3 + .7 * np.sin(2 * np.pi * 2.5 * t) ** 2)
    user *= 1200 / np.sqrt(np.mean(user * user))
    user[:rate * 5] = 0
    tail = slice(rate * 7, None)
    rms = lambda values: float(np.sqrt(np.mean(values[tail].astype(float) ** 2)))
    outcomes = {}
    for name, guarded, near in (
        ('unguarded echo', False, echo),
        ('guarded echo', True, echo),
        ('guarded double talk', True, np.clip(echo + user, -32768, 32767).astype(np.int16)),
    ):
        clock = [0.0]
        processor = AudioProcessor(sample_rate=rate, echo_cancellation=True,
            noise_suppression=True, high_pass_filter=True, auto_gain_control=True,
            ns_level=1, agc_max_gain_db=18, stream_delay_ms=50)
        enhancer = InputEnhancer(processor=processor, clock=lambda: clock[0])
        enhancer.input_latency_ms = enhancer.output_latency_ms = 25
        if not guarded:
            # Compare the previous positive-gain behavior with identical DSP.
            enhancer.speaker_active = lambda: False
        output = []
        for offset in range(0, count, 1024):
            clock[0] = offset / rate
            enhancer.rendered(speaker[offset:offset + 1024].tobytes())
            output.append(enhancer.process(near[offset:offset + 1024]))
        if enhancer.failed:
            raise RuntimeError('Native audio processing failed: ' + str(enhancer.metrics))
        clean = np.concatenate(output)
        outcomes[name] = rms(clean)
        reduction = 20 * np.log10(max(rms(near), .001) / max(rms(clean), .001))
        print(f'{name}: raw RMS {rms(near):.1f}, clean RMS {rms(clean):.1f}, '
              f'reduction {reduction:.1f} dB', flush=True)
    improvement = 20 * np.log10(outcomes['unguarded echo'] / max(outcomes['guarded echo'], .001))
    if improvement < 10:
        raise AssertionError(f'Expected at least 10 dB less echo amplification; got {improvement:.1f}')
    if outcomes['guarded double talk'] < rms(user) * .2:
        raise AssertionError('Near-end speech was excessively attenuated')
    print('PASS: less echo amplification and non-muted double talk on synthetic signals. '
          'This does not validate the physical robot.')


if __name__ == '__main__':
    main()
