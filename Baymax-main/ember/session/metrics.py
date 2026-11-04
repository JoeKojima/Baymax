"""Rolling latency profiles printed to the log, as in the version-8 runtime."""
import collections

import numpy as np

PROFILE_WINDOW = 50


class LatencyTracker:
    def __init__(self, name: str, window: int = PROFILE_WINDOW, queues=lambda: (0, 0)):
        self.name = name
        self.samples = collections.deque(maxlen=window)
        self._count = 0
        self._report_every = window
        self.queues = queues  # () -> (mic queue size, playback buffer bytes), for the summary line

    def record(self, duration_ms: float):
        self.samples.append(duration_ms)
        self._count += 1
        if self._count % self._report_every == 0:
            self._print_summary()

    def _print_summary(self):
        arr = np.array(self.samples)
        queue_mic, pbuf = self.queues()
        print(
            f"[PROFILE] {self.name:.<30s} "
            f"n={len(arr):>4d}  "
            f"avg={arr.mean():7.1f} ms  "
            f"p50={np.percentile(arr, 50):7.1f} ms  "
            f"p95={np.percentile(arr, 95):7.1f} ms  "
            f"max={arr.max():7.1f} ms  "
            f"queue_mic={queue_mic:>4d}  "
            f"pbuf={pbuf:>6d}"
        )


class Trackers:
    NAMES = ("mic_queue", "send_audio", "send_video", "receive", "roundtrip", "first_audio")
    LABELS = {"mic_queue": "mic_queue_wait", "send_audio": "send_audio_to_gemini",
              "send_video": "send_video_to_gemini", "receive": "receive_from_gemini",
              "roundtrip": "roundtrip_estimate", "first_audio": "first_audio_latency"}

    def __init__(self, queues):
        for name in self.NAMES:
            setattr(self, name, LatencyTracker(self.LABELS[name], queues=queues))

    def final_order(self):
        """The order v8 prints its final profiling summary in."""
        return [self.mic_queue, self.send_audio, self.send_video, self.receive, self.first_audio, self.roundtrip]
