"""Diagnostics checks use fake connections and audio; no capture or cloud calls."""
import ast
import base64
import collections
import io
from pathlib import Path
import socket
import ssl
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np
import network_probe


class ConnectionTests(unittest.TestCase):
    def test_success_reports_connection_stages_to_fixed_host(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        tls = Mock()
        tls.__enter__ = Mock(return_value=tls)
        tls.__exit__ = Mock(return_value=False)
        context = Mock()
        context.wrap_socket.return_value = tls
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch.object(network_probe.socket, 'getaddrinfo', return_value=address) as dns, \
                patch.object(network_probe.socket, 'socket', return_value=connection), \
                patch.object(network_probe.ssl, 'create_default_context', return_value=context):
            result = network_probe.probe()
        self.assertTrue(result['ok'])
        self.assertEqual(set(('dns_ms', 'tcp_ms', 'tls_ms')) - result.keys(), set())
        self.assertTrue(all(result[name] >= 0 for name in ('dns_ms', 'tcp_ms', 'tls_ms', 'total_ms')))
        dns.assert_called_once_with(network_probe.HOST, 443, type=socket.SOCK_STREAM)
        connection.send.assert_not_called()
        context.wrap_socket.assert_called_once_with(connection, server_hostname=network_probe.HOST)

    def test_failed_dns_is_safe_and_does_not_connect(self):
        with patch.object(network_probe.socket, 'getaddrinfo', side_effect=OSError('private error')), \
                patch.object(network_probe.socket, 'socket') as connect:
            result = network_probe.probe()
        self.assertFalse(result['ok'])
        self.assertIn('DNS', result['error'])
        self.assertNotIn('private error', result['error'])
        connect.assert_not_called()

    def test_failed_tcp_closes_socket(self):
        connection = Mock()
        connection.connect.side_effect = TimeoutError()
        with patch.object(network_probe.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), \
                patch.object(network_probe.socket, 'socket', return_value=connection):
            result = network_probe.probe()
        self.assertFalse(result['ok'])
        self.assertIn('TCP', result['error'])
        connection.close.assert_called_once()

    def test_tls_verification_failure_is_not_bypassed(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLError('certificate details')
        with patch.object(network_probe.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), \
                patch.object(network_probe.socket, 'socket', return_value=connection), \
                patch.object(network_probe.ssl, 'create_default_context', return_value=context):
            result = network_probe.probe()
        self.assertFalse(result['ok'])
        self.assertIn('TLS', result['error'])
        self.assertNotIn('certificate details', result['error'])


class LatencyTests(unittest.TestCase):
    def test_tracker_snapshot_is_bounded_and_rejects_invalid_samples(self):
        source = Path(__file__).resolve().parents[1] / 'realtime_gemini_10.py'
        node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.ClassDef) and n.name == 'LatencyTracker')
        namespace = {'collections': collections, 'threading': threading, 'np': np,
                     'time': Mock(monotonic=Mock(return_value=10)), 'PROFILE_WINDOW': 50}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        tracker = namespace['LatencyTracker']('test', window=3)
        tracker._print_summary = Mock()
        self.assertEqual(tracker.snapshot(), {'count': 0})
        for value in (10, 20, 30, 40, float('nan'), float('inf'), -1):
            tracker.record(value)
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot['count'], 3)
        self.assertEqual(snapshot['last_ms'], 40)
        self.assertEqual(snapshot['p50_ms'], 30)
        self.assertEqual(snapshot['p95_ms'], 39)

    def test_microphone_replay_is_local_mono_wav(self):
        from device_test import microphone_preview
        preview = microphone_preview([np.array([[0], [.5], [-2]], dtype=np.float32)], 48000)
        self.assertTrue(preview.startswith('data:audio/wav;base64,'))
        with wave.open(io.BytesIO(base64.b64decode(preview.split(',')[1])), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 48000))
            self.assertEqual(audio.getnframes(), 3)
