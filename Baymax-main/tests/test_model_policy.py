import ast
from pathlib import Path
import unittest

from features.memory_palace.model_policy import BackgroundQuota, background_model


class ModelPolicyTests(unittest.TestCase):
    def test_explicit_free_model_only(self):
        self.assertEqual(background_model(), 'gemini-3.1-flash-lite')
        self.assertEqual(background_model(' gemini-3.1-flash-lite '), 'gemini-3.1-flash-lite')
        for value in ('', 'gemini-3.5-flash-lite', 'gemini-flash-latest', 'gemini-4-argon',
                      'gemini-3.1-flash-lite,gemini-3.8-flash'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                background_model(value)

    def setUp(self):
        self.now = 0
        self.quota = BackgroundQuota(clock=lambda: self.now)

    def test_shared_pacing(self):
        self.quota.reserve()
        with self.assertRaises(RuntimeError):
            self.quota.reserve()
        self.now = 5
        self.quota.reserve()

    def test_quota_backoff_and_recovery(self):
        for delay in (900, 1800, 3600, 3600):
            self.quota.reserve()
            self.quota.failed(429)
            self.now += delay - 1
            with self.assertRaises(RuntimeError):
                self.quota.reserve()
            self.now += 1
        self.quota.succeeded()
        self.quota.reserve()
        self.quota.failed(429)
        self.assertEqual(self.quota.retry_at, self.now + 900)

    def test_server_and_unavailable(self):
        self.quota.failed(503)
        self.now = 59
        with self.assertRaises(RuntimeError):
            self.quota.reserve()
        self.now = 60
        self.quota.reserve()
        self.quota.failed(404)
        self.now = 100000
        with self.assertRaises(RuntimeError):
            self.quota.reserve()

    def test_runtime_defers_without_another_provider_call(self):
        # Execute the real wrapper without importing hardware or opening devices.
        source = Path(__file__).resolve().parents[1] / 'realtime_gemini_10.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_flash')
        class ApiError(Exception):
            code = 429
        calls = []
        class Client:
            models = None
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise ApiError()
        client = Client()
        client.models = client
        scope = {'_flash_client': client, '_flash_quota': self.quota,
                 'SUMMARY_MODEL': background_model(),
                 'genai_errors': type('Errors', (), {'APIError': ApiError})}
        exec(compile(ast.Module(body=[func], type_ignores=[]), str(source), 'exec'), scope)
        with self.assertRaises(ApiError):
            scope['_flash']('synthetic memory')
        with self.assertRaises(RuntimeError):
            scope['_flash']('synthetic memory')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['model'], 'gemini-3.1-flash-lite')

    def test_preflight_reads_the_actual_models(self):
        from gemini_preflight import configured_models
        runtime = Path(__file__).resolve().parents[1] / 'realtime_gemini_10.py'
        self.assertEqual(configured_models(runtime), (
            'gemini-2.5-flash-native-audio-preview-12-2025', background_model()))
