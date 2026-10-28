import json
import base64
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave
import runtime_monitor as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'status.json'
        self.transcripts = patch.object(monitor, 'transcripts', monitor.deque(maxlen=60))
        self.transcripts.start()
        self.addCleanup(self.transcripts.stop)
        self.active = patch.object(monitor, 'active_transcripts', {})
        self.active.start()
        self.addCleanup(self.active.stop)
        self.events = patch.object(monitor, 'events', monitor.deque(maxlen=80))
        self.events.start()
        self.addCleanup(self.events.stop)
        self.activities = patch.object(monitor, 'activities', {})
        self.activities.start()
        self.addCleanup(self.activities.stop)
        self.state = patch.object(monitor, 'state', 'starting')
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_missing_and_stale_heartbeat_report_offline(self):
        self.assertEqual(monitor.read_status(self.path)['state'], 'offline')
        self.path.write_text(json.dumps({'heartbeat': 10, 'state': 'speaking'}))
        self.assertEqual(monitor.read_status(self.path, now=16)['state'], 'offline')

    def test_tool_timeline_and_audio_buffer_are_reported(self):
        monitor.emit('thinking', 'Waiting for Gemini')
        monitor.activity('get_weather')
        self.assertIn('get_weather', monitor.snapshot()['activities'])
        monitor.activity('get_weather', running=False)
        monitor.emit('listening', 'Reply complete')
        snapshot = monitor.snapshot({'playback_bytes': 100})
        self.assertEqual(snapshot['state'], 'speaking')
        self.assertFalse(snapshot['activities'])
        self.assertEqual(snapshot['events'][-2]['message'], 'Finished get_weather')

    def test_atomic_status_file_and_stopped_state(self):
        with patch.object(monitor, 'PATH', self.path):
            monitor.emit('stopped', 'Ember stopped')
            monitor.write_status()
        self.assertEqual(monitor.read_status(self.path)['state'], 'stopped')
        self.assertFalse(self.path.with_suffix('.tmp').exists())

    def test_repeated_state_does_not_flood_timeline(self):
        for _ in range(100):
            monitor.emit('speaking', 'Reply audio')
        self.assertEqual(len(monitor.snapshot()['events']), 1)

    def test_unavailable_features_are_not_reported_as_running(self):
        capabilities = monitor.snapshot()['capabilities']
        self.assertIn('Disabled', capabilities['memory'])
        with patch.dict('os.environ', {'BAYMAX_GOOGLE_SEARCH': '0'}):
            self.assertEqual(monitor.snapshot()['capabilities']['google_search'], 'Disabled')
        with patch.dict('os.environ', {'BAYMAX_GOOGLE_SEARCH': '1', 'BAYMAX_TOOLKIT_ENABLED': '1'}):
            self.assertIn('grounding', monitor.snapshot()['capabilities']['google_search'])

    def test_stop_request_only_targets_current_runtime(self):
        request_path = Path(self.temp.name) / 'stop.json'
        with patch.object(monitor, 'STOP_PATH', request_path):
            request_path.write_text(json.dumps({'pid': monitor.os.getpid() + 1, 'requested_at': time.time()}))
            self.assertFalse(monitor.consume_stop_request())
            request_path.write_text(json.dumps({'pid': monitor.os.getpid(), 'requested_at': time.time() - 20}))
            self.assertFalse(monitor.consume_stop_request())
            request_path.write_text(json.dumps({'pid': monitor.os.getpid(), 'requested_at': time.time()}))
            self.assertTrue(monitor.consume_stop_request())
            self.assertFalse(request_path.exists())

    def test_live_audio_preview_is_bounded_mono_wav(self):
        tap = monitor.AudioPreviewTap()
        tap.begin('a' * 32, duration_s=1)
        tap.capture(b'\x01\x00\x02\x00', sample_rate=2)
        request_id, pcm, sample_rate = tap.take_completed()
        payload = monitor._preview_payload(request_id, pcm, sample_rate)
        encoded = payload['audio'].split(',', 1)[1]
        with wave.open(io.BytesIO(base64.b64decode(encoded)), 'rb') as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 2))
            self.assertEqual(wav.readframes(2), b'\x01\x00\x02\x00')
        self.assertIsNone(tap.take_completed())

    def test_live_audio_preview_request_must_target_this_process(self):
        request_path = Path(self.temp.name) / 'audio-request.json'
        with patch.object(monitor, 'audio_preview', monitor.AudioPreviewTap()):
            request_path.write_text(json.dumps({'pid': monitor.os.getpid() + 1, 'requested_at': time.time(),
                                                'request_id': 'a' * 32, 'duration_s': 3}))
            self.assertFalse(monitor.consume_audio_preview_request(request_path))
            self.assertFalse(request_path.exists())
            request_path.write_text(json.dumps({'pid': monitor.os.getpid(), 'requested_at': time.time(),
                                                'request_id': 'b' * 32, 'duration_s': 3}))
            self.assertTrue(monitor.consume_audio_preview_request(request_path))
            self.assertEqual(monitor.audio_preview.request_id, 'b' * 32)

    def test_live_audio_preview_request_round_trip(self):
        request_path = Path(self.temp.name) / 'audio-request.json'
        response_path = Path(self.temp.name) / 'audio-response.json'
        result = {}
        def request():
            result.update(monitor.request_audio_preview(monitor.os.getpid(), timeout=1,
                                                        request_path=request_path,
                                                        response_path=response_path))
        with patch.object(monitor, 'audio_preview', monitor.AudioPreviewTap()):
            thread = threading.Thread(target=request)
            thread.start()
            for _ in range(100):
                if request_path.exists():
                    break
                time.sleep(.005)
            self.assertTrue(monitor.consume_audio_preview_request(request_path))
            monitor.capture_audio_preview(b'\x01\x00' * 3, sample_rate=1)
            self.assertTrue(monitor.write_audio_preview_response(response_path))
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result['audio'].startswith('data:audio/wav;base64,'))
        self.assertFalse(response_path.exists())


    def test_fragments_append_into_one_live_message_without_losing_spaces(self):
        for fragment in ('Hello', ' . . . my', ' name', ' is Alex.'):
            monitor.transcribe('user', fragment)
        messages = monitor.snapshot()['transcripts']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['text'], 'Hello . . . my name is Alex.')
        self.assertTrue(messages[0]['live'])
        monitor.finish_transcripts()
        self.assertFalse(monitor.snapshot()['transcripts'][0]['live'])

    def test_split_word_and_speaker_order(self):
        monitor.transcribe('user', 'Hel')
        monitor.transcribe('user', 'lo')
        monitor.transcribe('ember', 'Hi!')
        monitor.finish_transcripts('ember')
        monitor.transcribe('user', 'Next question')
        messages = monitor.snapshot()['transcripts']
        self.assertEqual([m['speaker'] for m in messages], ['user', 'ember', 'user'])
        self.assertEqual(messages[0]['text'], 'Hello')
        self.assertFalse(messages[0]['live'])

    def test_offline_status_preserves_last_conversation(self):
        self.path.write_text(json.dumps({'heartbeat': 10, 'state': 'listening', 'transcripts': [{'text': 'Hello', 'live': True}]}))
        result=monitor.read_status(self.path, now=20)
        self.assertEqual(result['state'], 'offline')
        self.assertEqual(result['transcripts'][0]['text'], 'Hello')
        self.assertFalse(result['transcripts'][0]['live'])

    def test_snapshot_transcript_is_not_mutated_by_later_fragment(self):
        monitor.transcribe('user', 'Hello')
        before=monitor.snapshot()
        monitor.transcribe('user', ' there')
        self.assertEqual(before['transcripts'][0]['text'], 'Hello')


    def test_whitespace_only_delta_is_preserved_inside_message(self):
        monitor.transcribe('user', 'Hello')
        monitor.transcribe('user', ' ')
        monitor.transcribe('user', 'there')
        self.assertEqual(monitor.snapshot()['transcripts'][0]['text'], 'Hello there')


    def test_tool_start_and_end_update_one_stable_event(self):
        monitor.activity('get_weather')
        start = monitor.snapshot()['events'][-1]
        monitor.activity('get_weather', running=False)
        finish = monitor.snapshot()['events'][-1]
        self.assertEqual(len(monitor.snapshot()['events']), 1)
        self.assertEqual(start['id'], finish['id'])
        self.assertIsNone(start['finished_at'])
        self.assertIsNotNone(finish['finished_at'])

    def test_repeated_tool_calls_remain_separate_invocations(self):
        monitor.activity('get_weather')
        monitor.activity('get_weather', running=False)
        monitor.activity('get_weather')
        monitor.activity('get_weather', running=False)
        events=monitor.snapshot()['events']
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]['id'], events[1]['id'])
