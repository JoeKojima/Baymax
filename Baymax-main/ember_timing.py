"""Content-free local OpenTelemetry and measurement-only audio timing.

Hardware and the OTel SDK are never imported at module import time. Callbacks
use AudioTimeline.marker only; trace construction/export happens elsewhere.
"""
from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import math
import queue
import re
import threading
import time
import uuid
from pathlib import Path


class Timeline:
    # String values outside these explicit vocabularies are dropped. IDs must
    # be opaque application-generated values, never patient/database IDs.
    ENUMS = {
        "source": {"chroma", "exact_cache", "prepared_cache", "disabled", "obsolete", "error"},
        "phase": {"patient_speech", "robot_playback", "idle", "unknown"},
        "kind": {"start", "end", "activity_start", "activity_end", "activity", "unknown"},
        "reason": {"overlap", "ambiguous", "missing_boundary", "uncertain_clock", "negative_wait", "interrupted"},
    }
    IDS = {"lookup_id", "snapshot_id", "answer_id", "utterance_id"}
    NUMBERS = {"queue_ms", "throttle_ms", "elapsed_ms", "result_count", "profile_overlap",
               "cue_count", "sample_offset", "uncertain", "ambiguous", "overlap", "during_speech",
               "head_start_ms", "ready_before_lookup", "revision", "count", "underrun"}

    def __init__(self, directory=None, *, enabled=False, sample_rate=0.1, benchmark=False,
                 max_bytes=10_000_000, queue_size=2048, clock=time.monotonic):
        if not isinstance(enabled, bool) or not isinstance(benchmark, bool):
            raise ValueError("instrumentation flags must be booleans")
        self.enabled = enabled
        self.clock = clock
        self._epoch_offset = time.time_ns() - int(clock() * 1e9)
        self._lock = threading.RLock()
        self._counts = collections.Counter()
        self._samples = collections.defaultdict(lambda: collections.deque(maxlen=2048))
        self._roots = collections.OrderedDict()
        self._provider = self._exporter = None
        self._closed = False
        if enabled:
            if not isinstance(sample_rate, (int, float)) or not math.isfinite(sample_rate) or not 0 <= sample_rate <= 1:
                raise ValueError("trace_sample_rate must be between zero and one")
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource
            self._exporter = _local_exporter(directory, max_bytes)
            self._provider = TracerProvider(
                resource=Resource({"service.name": "ember-memory"}),
                sampler=ParentBased(TraceIdRatioBased(1.0 if benchmark else sample_rate)),
                shutdown_on_exit=False,
            )
            self._provider.add_span_processor(BatchSpanProcessor(
                self._exporter, max_queue_size=queue_size,
                max_export_batch_size=min(128, queue_size), schedule_delay_millis=1000,
            ))
            self._tracer = self._provider.get_tracer("ember.timeline", "1")

    def _attributes(self, attributes):
        safe = {}
        for key, value in attributes.items():
            if key in self.ENUMS and isinstance(value, str) and value in self.ENUMS[key]:
                safe[key] = value
            elif key in self.IDS and isinstance(value, str) and re.fullmatch(r"[a-f0-9-]{1,64}", value):
                safe[key] = value
            elif key in self.NUMBERS and isinstance(value, (int, float, bool)) and math.isfinite(value):
                safe[key] = value
        return safe

    def _time_ns(self, timestamp):
        return self._epoch_offset + int(timestamp * 1e9)

    def event(self, name, session_id="", turn_id="", timestamp=None, **attributes):
        self._record(name, 0.0, session_id, turn_id, timestamp, attributes)

    def duration(self, name, elapsed_ms, session_id="", turn_id="", **attributes):
        self._record(name, elapsed_ms, session_id, turn_id, None, attributes, duration=True)

    def _record(self, name, elapsed, sid, tid, timestamp, attributes, duration=False):
        if not self.enabled or self._closed:
            return
        # Event names are application constants. Never accept dynamic content.
        if not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", name):
            return
        now = self.clock() if timestamp is None else timestamp
        with self._lock:
            self._counts[name] += 1
            if duration:
                self._samples[name].append(elapsed)
            for key in ("queue_ms", "throttle_ms", "head_start_ms"):
                value = attributes.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    self._samples[f"{name}.{key}"].append(value)
            safe = self._attributes(attributes)
            # Only UUID-like IDs can enter exported correlation attributes.
            for key, value in (("session_id", sid), ("turn_id", tid)):
                if re.fullmatch(r"[a-f0-9-]{1,64}", value or ""):
                    safe[key] = value
            key = sid, tid
            while self._roots and (len(self._roots) >= 128 or now - next(iter(self._roots.values()))[1] > 60):
                self._roots.popitem(last=False)[1][0].end()
            root = self._roots.get(key)
            if root is None:
                root = self._tracer.start_span("conversation.turn", attributes={k: v for k, v in safe.items()
                                               if k in ("session_id", "turn_id")},
                                               start_time=self._time_ns(now - elapsed / 1000)), now
                self._roots[key] = root
            from opentelemetry.trace import set_span_in_context
            span = self._tracer.start_span(name, context=set_span_in_context(root[0]),
                                          attributes=safe, start_time=self._time_ns(now - elapsed / 1000))
            if duration:
                span.set_attribute("elapsed_ms", elapsed)
            span.end(end_time=self._time_ns(now))

    def aggregate(self):
        with self._lock:
            return {"counts": dict(self._counts), "durations_ms": {k: list(v) for k, v in self._samples.items()}}

    def flush_aggregates(self):
        """Called by the background consumer, never by a callback."""
        if self._exporter:
            self._exporter.write({"type": "aggregate", "timestamp_ns": time.time_ns(), **self.aggregate()})

    def close(self):
        if self._closed:
            return
        with self._lock:
            for span, _ in self._roots.values():
                span.end()
            self._roots.clear()
            self.flush_aggregates()
            self._closed = True
        if self._provider:
            self._provider.shutdown()


def _local_exporter(directory, max_bytes):
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class LocalExporter(SpanExporter):
        def __init__(self):
            Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
            self.handler = logging.handlers.RotatingFileHandler(
                Path(directory) / "timeline.jsonl", maxBytes=max_bytes, backupCount=4, encoding="utf-8")
            self.handler.setFormatter(logging.Formatter("%(message)s"))

        def write(self, payload):
            line = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            self.handler.handle(logging.LogRecord("ember", logging.INFO, "", 0, line, (), None))

        def export(self, spans):
            try:
                for span in spans:
                    self.write({"type": "span", **json.loads(span.to_json())})
                return SpanExportResult.SUCCESS
            except (OSError, ValueError):
                return SpanExportResult.FAILURE

        def shutdown(self):
            self.handler.close()

    return LocalExporter()


def device_time(info, field, now):
    """Translate PortAudio's stream clock to monotonic time; flag fallback."""
    try:
        current, target = float(getattr(info, "currentTime")), float(getattr(info, field))
        if current <= 0 or target <= 0 or not math.isfinite(target - current) or abs(target-current) > 5:
            raise ValueError
        return now + target - current, False
    except (AttributeError, ValueError, TypeError):
        return now, True


class AudioTimeline:
    """Bounded callback handoff and conservative speech/answer association.

    No control decisions or microphone gating depend on these estimates.
    A single unambiguous speech interval is needed to report patient wait.
    """
    def __init__(self, timeline, *, quiet_seconds=0.3, clock=time.monotonic):
        self.timeline, self.clock, self.quiet_seconds = timeline, clock, quiet_seconds
        self.markers = queue.Queue(maxsize=2048)
        self.dropped = 0
        self.reset("")

    def reset(self, session_id):
        self.session_id = session_id
        self.active = None
        self.intervals = collections.deque(maxlen=128)
        self.turns = collections.OrderedDict()
        self.playback_until = 0.0
        self._uncertain_until = 0.0
        while not self.markers.empty():
            try:
                self.markers.get_nowait()
            except queue.Empty:
                break

    def marker(self, kind, timestamp, *values):
        if not self.timeline.enabled:
            return
        try:
            self.markers.put_nowait((kind, timestamp, values))
        except queue.Full:
            self.dropped += 1

    @property
    def phase(self):
        if self.active is not None:
            return "patient_speech"
        return "robot_playback" if self.clock() < self.playback_until else "idle"

    def transcript(self, turn_id):
        self.drain()
        turn = self.turns.setdefault(turn_id, {"receipt": self.clock(), "ambiguous": False})
        if "reply" in turn:
            turn["ambiguous"] = True
        self.timeline.event("transcript.received", self.session_id, turn_id)

    def reply(self, turn_id):
        self.drain()
        turn = self.turns.setdefault(turn_id, {"receipt": self.clock(), "ambiguous": True})
        turn.setdefault("reply", self.clock())
        self.timeline.event("reply.first_content", self.session_id, turn_id, answer_id=turn_id)

    def interrupted(self, turn_id):
        turn = self.turns.get(turn_id)
        if turn:
            turn["ambiguous"] = True
        self.timeline.event("playback.interrupted", self.session_id, turn_id, reason="interrupted")

    def server_activity(self, kind, turn_id=""):
        self.timeline.event("speech.server_notification", self.session_id, turn_id, kind=kind, ambiguous=True)

    def drain(self, now=None):
        now = self.clock() if now is None else now
        if self.dropped:
            self.timeline.event("audio.marker_dropped", count=self.dropped)
            self.dropped = 0
            self._uncertain_until = now + 30
            if self.active:
                self.active["uncertain"] = True
            for turn in self.turns.values():
                turn["ambiguous"] = True
            for interval in self.intervals:
                interval["uncertain"] = True
        for _ in range(2048):
            try:
                kind, timestamp, values = self.markers.get_nowait()
            except queue.Empty:
                break
            if kind == "mic":
                loud, end, overlap, uncertain = values
                self._finalize(timestamp)
                if loud:
                    if self.active is None:
                        self.active = {"start": timestamp, "end": end, "overlap": overlap,
                                       "uncertain": uncertain or timestamp <= self._uncertain_until,
                                       "id": uuid.uuid4().hex}
                        self.timeline.event("speech.estimated_start", self.session_id, timestamp=timestamp,
                                            utterance_id=self.active["id"], overlap=overlap, uncertain=uncertain)
                    else:
                        self.active["end"] = end
                        self.active["overlap"] |= overlap
                        self.active["uncertain"] |= uncertain
            elif kind == "output":
                tid, end, uncertain, underrun = values
                self.playback_until = max(self.playback_until, end)
                self.timeline.event("audio.output_callback", self.session_id, tid, underrun=underrun)
                turn = self.turns.setdefault(tid, {"receipt": timestamp, "ambiguous": True})
                if "output" not in turn:
                    turn.update(output=timestamp, output_uncertain=uncertain)
                    self.timeline.event("playback.output_device_estimate", self.session_id, tid,
                                        timestamp=timestamp, uncertain=uncertain, answer_id=tid)
            elif kind == "underrun":
                self.timeline.event("audio.underrun", self.session_id, underrun=True)
            elif kind == "output_activity":
                self.playback_until = max(self.playback_until, values[0])
            elif kind == "association_uncertain":
                tid = values[0]
                self.turns.setdefault(tid, {"receipt": timestamp})["ambiguous"] = True
                self.timeline.event("playback.metadata_dropped", self.session_id, tid)
        self._finalize(now)
        for tid, turn in list(self.turns.items()):
            if "output" in turn and not turn.get("reported") and now >= turn["output"] + self.quiet_seconds:
                self._report_wait(tid, turn)
            if now - turn["receipt"] > 60 or len(self.turns) > 128:
                self.turns.pop(tid, None)

    def _finalize(self, now):
        if self.active and now - self.active["end"] >= self.quiet_seconds:
            interval, self.active = self.active, None
            self.intervals.append(interval)
            self.timeline.event("speech.estimated_end", self.session_id, timestamp=interval["end"],
                                utterance_id=interval["id"], overlap=interval["overlap"], uncertain=interval["uncertain"])

    def _report_wait(self, tid, turn):
        turn["reported"] = True
        candidates = [i for i in self.intervals if not i.get("used")
                      and turn["output"] - 30 <= i["end"] <= turn["output"]]
        # Even an excluded answer consumes the possible intervals. Otherwise a
        # late transcript could incorrectly attach the same speech to a later answer.
        for interval in candidates:
            interval["used"] = True
        reason = None
        if turn["ambiguous"] or len(candidates) > 1:
            reason = "ambiguous"
        elif not candidates or self.active is not None:
            reason = "missing_boundary"
        else:
            interval = candidates[0]
            interval["used"] = True
            if interval["overlap"]:
                reason = "overlap"
            elif interval["uncertain"] or turn["output_uncertain"]:
                reason = "uncertain_clock"
            elif turn["output"] < interval["end"]:
                reason = "negative_wait"
        if reason:
            self.timeline.event("patient_wait.excluded", self.session_id, tid, reason=reason,
                                utterance_id=candidates[0]["id"] if len(candidates) == 1 else "")
        else:
            self.timeline.duration("patient_wait.estimated", (turn["output"]-interval["end"])*1000,
                                   self.session_id, tid, utterance_id=interval["id"], answer_id=tid)
