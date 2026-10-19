"""Conservative background API policy; billing tier is managed by Google."""
import threading
import time

FREE_BACKGROUND_MODELS = frozenset({'gemini-3.1-flash-lite', 'gemini-3.5-flash-lite'})
DEFAULT_BACKGROUND_MODEL = 'gemini-3.1-flash-lite'


def background_model(configured=None):
    """Select one explicit free-tier eligible model, never an alias/fallback."""
    model = DEFAULT_BACKGROUND_MODEL if configured is None else configured.strip()
    if model not in FREE_BACKGROUND_MODELS:
        raise ValueError('EMBER_FLASH_MODELS must be one free-tier Flash-Lite model: '
                         + ', '.join(sorted(FREE_BACKGROUND_MODELS)))
    return model


class BackgroundQuota:
    """Share pacing/backoff across appearance, summary and rewrite workers."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.retry_at = 0.0
        self.unavailable = False
        self.quota_failures = 0
        self.lock = threading.Lock()

    def reserve(self):
        with self.lock:
            now = self.clock()
            if self.unavailable:
                raise RuntimeError('Background model unavailable; memory work deferred until restart.')
            if now < self.retry_at:
                raise RuntimeError('Background API cooling down; memory work deferred.')
            self.retry_at = now + 5.0  # at most 12 background attempts/minute

    def failed(self, code):
        with self.lock:
            if code == 404:
                self.unavailable = True
            elif code == 429:
                self.quota_failures = min(self.quota_failures + 1, 3)
                delay = 900 * 2 ** (self.quota_failures - 1)
                self.retry_at = max(self.retry_at, self.clock() + delay)
            elif code in (500, 503, 504):
                self.retry_at = max(self.retry_at, self.clock() + 60)

    def succeeded(self):
        with self.lock:
            self.quota_failures = 0
