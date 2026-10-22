import json
import types

import pytest

from ember_timing import AudioTimeline, Timeline, device_time
from conftest import Clock, Events


def test_local_otel_privacy_correlation_sampling_and_aggregates(tmp_path):
    timeline = Timeline(tmp_path, enabled=True, benchmark=True)
    sid, tid = "a"*32, "b"*32
    timeline.event("transcript.received", sid, tid, transcript="Sarah is visiting",
                   patient_name="Sarah", memory_text="Secret", source="chroma")
    timeline.duration("retrieval.total", 12, sid, tid, lookup_id="c"*32)
    timeline.event("retrieval.worker_started", sid, tid, queue_ms=4)
    timeline.close()
    text = (tmp_path / "timeline.jsonl").read_text()
    assert all(secret not in text for secret in ("Sarah", "Secret", "patient_name", "memory_text"))
    records = [json.loads(line) for line in text.splitlines()]
    spans = [r for r in records if r["type"] == "span"]
    assert {r["name"] for r in spans} >= {"transcript.received", "retrieval.total", "conversation.turn"}
    assert len({r["context"]["trace_id"] for r in spans}) == 1
    assert any(r.get("counts", {}).get("transcript.received") == 1 for r in records)
    from tools.timeline_report import summarize
    report = summarize(tmp_path)
    assert report["durations_ms"]["retrieval.total"]["p95"] == 12
    assert report["durations_ms"]["retrieval.worker_started.queue_ms"]["p95"] == 4
    zero = Timeline(tmp_path / "zero", enabled=True, sample_rate=0)
    for _ in range(25):
        zero.event("transcript.received", sid, tid)
    zero.close()
    records = [json.loads(line) for line in (tmp_path / "zero/timeline.jsonl").read_text().splitlines()]
    assert not any(r["type"] == "span" for r in records)
    assert records[0]["counts"]["transcript.received"] == 25


def test_rotation_is_bounded(tmp_path):
    timeline = Timeline(tmp_path, enabled=True, benchmark=True, max_bytes=4096, queue_size=128)
    for i in range(100):
        timeline.event("memory.empty", "a"*32, f"{i:032x}")
    timeline.close()
    files = list(tmp_path.glob("timeline.jsonl*"))
    assert 1 <= len(files) <= 5
    assert all(p.stat().st_size < 6000 for p in files)


def test_measurement_backdates_quiet_end_and_excludes_overlap_late_or_missing_text():
    for mode in ("valid", "overlap", "late", "missing", "uncertain"):
        clock, events = Clock(), Events()
        audio = AudioTimeline(events, clock=clock)
        audio.reset("a"*32)
        audio.marker("mic", 100, True, 100.02, mode == "overlap", mode == "uncertain")
        clock.advance(.4)
        audio.drain()
        assert audio.intervals[0]["end"] == 100.02
        if mode not in ("late", "missing"):
            audio.transcript("b"*32)
        audio.reply("b"*32)
        if mode == "late":
            audio.transcript("b"*32)
        audio.marker("output", 100.6, "b"*32, 100.7, False, False)
        audio.drain()
        clock.advance(1)
        audio.drain()
        waits = [v for n, v in events.durations if n == "patient_wait.estimated"]
        assert waits == (pytest.approx([580]) if mode == "valid" else [])
        assert any(e[0] == "patient_wait.excluded" for e in events.events) == (mode != "valid")


def test_ambiguous_intervals_and_marker_overflow_are_excluded():
    clock, events = Clock(), Events()
    audio = AudioTimeline(events, clock=clock)
    audio.reset("s")
    audio.marker("mic", 100, True, 100.02, False, False)
    audio.marker("mic", 101, True, 101.02, False, False)
    audio.drain(101.4)
    audio.transcript("turn")
    audio.reply("turn")
    audio.marker("output", 101.5, "turn", 101.6, False, False)
    audio.drain(102)
    assert not events.durations
    for _ in range(3000):
        audio.marker("underrun", 102)
    assert audio.markers.qsize() == 2048
    assert audio.dropped > 0


def test_device_clock_mapping_and_uncertain_fallback():
    info = types.SimpleNamespace(currentTime=50, outputBufferDacTime=50.1)
    mapped, uncertain = device_time(info, "outputBufferDacTime", 100)
    assert mapped == pytest.approx(100.1) and not uncertain
    assert device_time(None, "outputBufferDacTime", 100) == (100, True)
