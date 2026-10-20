import asyncio
import os
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from features.conversational_ability.processing import InputEnhancer, extend_config
from test_public_core_integration import load_function


class FakeDSP:
    speech_probability = .5
    gain_db = 3
    def reset(self):
        self.frames = []
    def process(self, near, far):
        self.frames.append((near.copy(), far.copy()))
        return near.copy()


class AudioInputTests(unittest.TestCase):
    def test_disabled_configuration_is_unchanged_and_enabled_preserves_model_audio(self):
        base = {'speech_config': {'voice': 'same'}, 'realtime_input_config': {'automatic_activity_detection': {'silence_duration_ms': 100}}}
        with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '0'}):
            self.assertIs(extend_config(base), base)
        with patch.dict(os.environ, {'BAYMAX_AUDIO_ENHANCEMENT': '1'}):
            result = extend_config(base)
        self.assertEqual(result['speech_config'], base['speech_config'])
        self.assertEqual(base['realtime_input_config']['automatic_activity_detection']['silence_duration_ms'], 100)
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['silence_duration_ms'], 500)
        self.assertEqual(result['realtime_input_config']['activity_handling'], 'START_OF_ACTIVITY_INTERRUPTS')
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['prefix_padding_ms'], 0)
        self.assertEqual(result['realtime_input_config']['automatic_activity_detection']['start_of_speech_sensitivity'], 'START_SENSITIVITY_HIGH')

    def test_chunking_preserves_every_sample_across_1024_sample_callbacks(self):
        dsp = FakeDSP()
        enhancer = InputEnhancer(processor=dsp)
        source = np.arange(8192, dtype=np.int16)
        rendered = source * 2
        result = []
        for offset in range(0, len(source), 1024):
            enhancer.rendered(rendered[offset:offset+1024].tobytes())
            result.append(enhancer.process(source[offset:offset+1024]))
        result = np.concatenate(result)
        np.testing.assert_array_equal(result[:480], np.zeros(480))
        np.testing.assert_array_equal(result[480:], source[:-480])
        self.assertTrue(all(len(near) == len(far) == 480 for near, far in dsp.frames))
        np.testing.assert_array_equal(np.concatenate([far for _, far in dsp.frames]), rendered[:len(dsp.frames)*480])
        self.assertTrue(enhancer.full_duplex)

    def test_brief_80ms_input_is_not_discarded_by_chunking_or_a_local_gate(self):
        enhancer = InputEnhancer(processor=FakeDSP())
        samples = np.zeros(16384, dtype=np.int16)
        samples[3500:7340] = 250
        out = np.concatenate([enhancer.process(samples[i:i+1024]) for i in range(0, len(samples), 1024)])
        np.testing.assert_array_equal(out[480:], samples[:-480])
        self.assertEqual(np.count_nonzero(out), 3840)

    def test_reference_is_bounded_and_missing_render_is_silence(self):
        dsp = FakeDSP()
        enhancer = InputEnhancer(processor=dsp)
        enhancer.process(np.ones(1024, dtype=np.int16))
        self.assertTrue(all(not np.any(far) for _, far in dsp.frames))
        enhancer.rendered(b'\1\0' * 100000)
        self.assertLessEqual(len(enhancer.render_buffer), 24000)

    def test_dsp_failure_keeps_raw_input_and_disables_full_duplex(self):
        dsp = FakeDSP()
        dsp.process = Mock(side_effect=RuntimeError('test DSP failure'))
        enhancer = InputEnhancer(processor=dsp)
        samples = np.full(1024, 1234, dtype=np.int16)
        np.testing.assert_array_equal(enhancer.process(samples), samples)
        self.assertFalse(enhancer.full_duplex)
        self.assertIn('failed', enhancer.metrics['state'])

    def test_negative_full_scale_is_detected_as_clipping(self):
        self.assertEqual(InputEnhancer.levels(np.array([-32768, 32767], dtype=np.int16))['clipped_percent'], 100)

    def test_microphone_is_sent_during_playback_with_healthy_echo_cancellation(self):
        async def exercise(active):
            mic_queue = asyncio.Queue()
            await mic_queue.put({'data': b'pcm-test', 'mime_type': 'audio/pcm;rate=48000'})
            sent = []
            async def send(**kwargs):
                sent.append(kwargs['audio'].data)
                raise asyncio.CancelledError()
            fn = load_function('send_audio_realtime', {'asyncio': asyncio, 'time': time,
                'audio_queue_mic': mic_queue, 'tracker_mic_queue': SimpleNamespace(record=Mock()),
                'tracker_send_audio': SimpleNamespace(record=Mock()), '_playback_lock': threading.Lock(),
                '_playback_buffer': b'playing', '_audio_enhancer': active,
                'types': SimpleNamespace(Blob=lambda **kw: SimpleNamespace(**kw))})
            try:
                await asyncio.wait_for(fn(SimpleNamespace(send_realtime_input=send)), .02)
            except (asyncio.CancelledError, TimeoutError):
                pass
            return sent
        self.assertEqual(asyncio.run(exercise(SimpleNamespace(full_duplex=True))), [b'pcm-test'])
        self.assertEqual(asyncio.run(exercise(SimpleNamespace(full_duplex=False))), [])

    def test_desktop_v8_preserves_raw_recording_with_processed_gemini_input(self):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "realtime_gemini_8.py"
        with patch('test_public_core_integration.CORE', path):
            self.test_recording_stays_raw_while_only_gemini_input_is_processed()

    def test_recording_stays_raw_while_only_gemini_input_is_processed(self):
        raw_recording = queue.Queue()
        microphone = asyncio.Queue()
        samples = np.full((1024, 1), 1234, dtype=np.int16)
        enhancer = SimpleNamespace(reset=lambda: None, process=lambda values: np.zeros_like(values))
        class Stream:
            latency = .02
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def __enter__(self):
                self.callback(samples, 1024, None, None)
            def __exit__(self, *args):
                pass
        listen = load_function('listen_audio', {'asyncio': asyncio, 'np': np, 'queue': queue,
            'time': time, '_audio_enhancer': enhancer, '_waiting_cue': None, 'INPUT_CHANNELS': 1,
            '_audio_record_queue': raw_recording, 'audio_queue_mic': microphone,
            'INTERRUPT_RMS_THRESHOLD': 1000, 'sd': SimpleNamespace(InputStream=Stream),
            'SEND_SAMPLE_RATE': 48000, 'CHUNK_SIZE': 1024})
        async def run():
            try:
                await asyncio.wait_for(listen(), .02)
            except TimeoutError:
                pass
        asyncio.run(run())
        self.assertEqual(raw_recording.get_nowait(), samples.tobytes())
        self.assertEqual(microphone.get_nowait()['data'], bytes(2048))
