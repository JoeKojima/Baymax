import asyncio
import json
import threading
from collections import Counter

import numpy as np
import pytest
import ember_memory as implementation
from ember_memory import MemoryService, PatientContext, PriorityWorker, RETRIEVAL
from conftest import Clock, FakeEmbedder, memory, run_async, until


def calibration(tmp_path, **changes):
    data = dict(threshold=.75, model_fingerprint="fake", backend="minilm-chroma",
                retrieval_settings=RETRIEVAL, development_passed=True, heldout_passed=True)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data | changes))
    return str(path)


@run_async
async def test_exact_hits_skip_work_and_do_not_extend_expiry():
    clock = Clock()
    async with memory({"cache_enabled": True}, clock=clock) as s:
        first = await s.lookup("Sarah  garden", "s")
        clock.advance(299)
        hit = await s.lookup(" Sarah\n garden ", "s")
        assert hit.source == "exact_cache" and first.records == hit.records
        assert len(s.embedder.embed_calls) == len(s.embedder.search_calls) == 1
        clock.advance(1)
        assert (await s.lookup("Sarah garden", "s")).source == "chroma"


@pytest.mark.parametrize("passing", [True, False])
@run_async
async def test_prepared_results_score_the_actual_question_and_require_calibration(tmp_path, passing):
    config = {"prepared_cache_enabled": True, "calibration_path": calibration(tmp_path, heldout_passed=passing)}
    async with memory(config) as s:
        await s.lookup("Sarah", "s")
        result = await s.lookup("actual Sarah", "s")
        assert result.source == ("prepared_cache" if passing else "chroma")
        assert len(s.embedder.search_calls) == (1 if passing else 2)
        for record in result.records:
            vector = next(r["embedding"] for r in s.embedder.records if r["id"] == record["id"])
            assert record["distance"] == pytest.approx(1 - np.dot(vector, [.8, .6, 0]), abs=1e-6)


@pytest.mark.parametrize("changes", [{"model_fingerprint": "other"}, {"backend": "fake"},
    {"retrieval_settings": RETRIEVAL | {"preparation": "topic-tags-v1"}},
    {"retrieval_settings": {k: v for k, v in RETRIEVAL.items() if k != "preparation"}},
    {"development_passed": False}, {"threshold": True}, {"threshold": float("nan")}, {"threshold": .1}])
@run_async
async def test_incompatible_calibration_never_serves_prepared_memories(tmp_path, changes):
    async with memory({"prepared_cache_enabled": True, "calibration_path": calibration(tmp_path, **changes)}) as s:
        await s.lookup("Sarah", "s")
        assert (await s.lookup("actual Sarah", "s")).source == "chroma"


@run_async
async def test_short_or_damaged_candidates_fall_back_to_database(tmp_path):
    async with memory({"prepared_cache_enabled": True, "calibration_path": calibration(tmp_path)}) as s:
        await s.lookup("Sarah", "s")
        next(iter(s.documents.values()))[0]["embedding"] = np.ones(7)
        assert (await s.lookup("actual Sarah", "s")).source == "chroma"
        s.embedder.records = s.embedder.records[:2]
        s.invalidate()
        assert len((await s.lookup("Sarah", "s")).records) == 2
        assert (await s.lookup("actual Sarah", "s")).source == "chroma"


@run_async
async def test_cache_limits_and_oversized_entries(monkeypatch):
    monkeypatch.setattr(implementation, "QUERY_LIMIT", 2)
    monkeypatch.setattr(implementation, "DOCUMENT_LIMIT", 4)
    async with memory({"cache_enabled": True}) as s:
        for i in range(6):
            s.embedder.records = [{**r, "id": f"{i}-{n}"} for n, r in enumerate(s.embedder.records)]
            await s.lookup(f"query {i}", "s")
        assert len(s.queries) == 2 and len(s.documents) == 4
        monkeypatch.setattr(implementation, "BYTE_LIMIT", 1024)
        s.invalidate()
        await s.lookup("Sarah", "s")
        assert not s.queries  # Three-record query result exceeds this test budget.
        assert sum(e[2] for c in (s.queries, s.documents) for e in c.values()) <= 1024
        assert (await s.lookup("Sarah", "s")).source == "chroma"
        s.embedder.records = []
        await s.lookup("empty", "s")
        assert (await s.lookup("empty", "s")).source == "exact_cache"
        await s.lookup("x" * 2048, "s")
        assert "x" * 2048 not in s.queries


@run_async
async def test_patient_session_and_revision_separation():
    async with memory({"cache_enabled": True}) as a, memory(patient="beta") as b:
        first = await a.lookup("Sarah", "s")
        assert all(r["id"].startswith("beta-") for r in (await b.lookup("Sarah", "s")).records)
        a.invalidate()
        assert not a.valid(first.token) and not a.queries and not a.documents
        assert (await a.lookup("Sarah", "s")).source == "chroma"
        await a.start_session("new")
        assert (await a.lookup("Sarah", "s")).source == "disabled"
        assert not a.queries and not a.documents


def test_patient_paths_never_assign_legacy_data(tmp_path):
    legacy = tmp_path / "chroma_store"
    legacy.mkdir()
    (legacy / "sentinel").write_text("legacy")
    assert PatientContext.resolve(tmp_path, "") is None
    for patient in ("alpha", "beta"):
        context = PatientContext.resolve(tmp_path, patient)
        context.prepare()
        assert context.chroma_dir != legacy and context.root.name == patient
    assert (legacy / "sentinel").read_text() == "legacy"
    with pytest.raises(ValueError):
        PatientContext.resolve(tmp_path, "../escape")


def test_worker_prioritizes_foreground_and_rejects_jobs_after_close():
    worker, entered, release, order = PriorityWorker(), threading.Event(), threading.Event(), []
    def block():
        entered.set()
        release.wait(1)
        order.append("running")
    try:
        first = worker.submit(block, background=True)
        assert entered.wait(1)
        background = worker.submit(lambda: order.append("background"), background=True)
        foreground = worker.submit(lambda: order.append("foreground"))
        release.set()
        for future in (first, background, foreground):
            future.result(1)
        assert order == ["running", "foreground", "background"]
    finally:
        release.set()
        worker.close()
    with pytest.raises(RuntimeError):
        worker.submit(lambda: None).result()


@pytest.mark.parametrize("action", ["invalidate", "reconnect"])
@run_async
async def test_stale_foreground_results_cannot_repopulate_cache(action):
    embedder = FakeEmbedder()
    embedder.release = threading.Event()
    async with memory(embedder=embedder) as s:
        task = asyncio.create_task(s.lookup("Sarah", "s"))
        assert await asyncio.to_thread(embedder.entered.wait, 1)
        if action == "invalidate":
            s.invalidate()
        else:
            await s.start_session("new")
        embedder.release.set()
        assert (await task).source == "obsolete" and not s.documents


@run_async
async def test_topic_cues_merge_deduplicate_and_obey_five_second_budget():
    clock = Clock()
    async with memory({"topic_aliases": {"Sarah": "family"}}, clock=clock) as s:
        s.observe_transcript("Sa", "s", "1")
        s.observe_transcript("rah ", "s", "1")
        await until(lambda: len(s.documents) == 3)
        assert s.embedder.search_calls == [(8, True, {"tags": {"$contains": "family"}})]
        for text in ("music ", "appointment ", "Sarah music "):
            s.observe_transcript(text, "s", "1")
        assert s.pending[2] == {"appointments", "music"}
        clock.advance(4.99)
        s.wake.set()
        await asyncio.sleep(.01)
        assert len(s.embedder.search_calls) == 1
        clock.advance(.01)
        s.wake.set()
        await until(lambda: len(s.documents) == 9)
        assert s.embedder.search_calls[-1][2] == {"$or": [
            {"tags": {"$contains": "appointments"}}, {"tags": {"$contains": "music"}}]}
        clock.advance(50)
        s.wake.set()
        await asyncio.sleep(.01)
        assert len(s.embedder.search_calls) == 2


@run_async
async def test_new_turn_replaces_pending_and_reconnect_keeps_cooldown():
    clock = Clock()
    async with memory(clock=clock) as s:
        s.observe_transcript("family ", "s", "1")
        await until(lambda: len(s.documents) == 3)
        s.observe_transcript("music ", "s", "1")
        s.observe_transcript("appointment ", "s", "2")
        assert s.pending[2] == {"appointments"}
        await s.start_session("new")
        s.observe_transcript("music ", "new", "3")
        await asyncio.sleep(.01)
        assert len(s.embedder.search_calls) == 1
        clock.advance(5)
        s.wake.set()
        await until(lambda: len(s.documents) == 3)
        assert s.embedder.embed_calls == ["family", "music"]


@pytest.mark.parametrize("action", ["discard_preparation", "invalidate", "reconnect"])
@run_async
async def test_obsolete_preparation_is_discarded(action):
    embedder = FakeEmbedder()
    embedder.release = threading.Event()
    async with memory(embedder=embedder) as s:
        s.observe_transcript("family ", "s", "1")
        assert await asyncio.to_thread(embedder.entered.wait, 1)
        s.observe_transcript("music ", "s", "1")
        if action == "reconnect":
            await s.start_session("new")
        else:
            getattr(s, action)()
        embedder.release.set()
        await asyncio.wrap_future(s.worker.submit(lambda: None))
        assert not s.documents and not s.pending and not embedder.search_calls


@pytest.mark.parametrize("enabled", [True, False])
@run_async
async def test_untagged_memories_remain_available_and_preparation_can_be_disabled(enabled):
    async with memory({"prefetch_enabled": enabled}) as s:
        for record in s.embedder.records:
            record["metadata"].pop("tags")
        s.observe_transcript("family ", "s", "1")
        if enabled:
            await until(lambda: bool(s.embedder.search_calls))
            await asyncio.wrap_future(s.worker.submit(lambda: None))
            assert len(s.embedder.search_calls) == 1 and not s.documents
        else:
            assert not s.embedder.embed_calls
        assert len((await s.lookup("Sarah", "s")).records) == 3
        assert s.embedder.search_calls[-1][2] is None


@run_async
async def test_terminal_word_debounces():
    clock = Clock()
    async with memory(clock=clock) as s:
        s.observe_transcript("family", "s", "1")
        await asyncio.sleep(0)
        assert not s.embedder.embed_calls
        clock.advance(.21)
        await until(lambda: len(s.documents) == 3)


@pytest.mark.parametrize("config", [{"cache_enabled": "true"}, {"prefetch_enabled": 1},
    {"prefetch_interval_seconds": 4}, {"prefetch_interval_seconds": float("nan")}, {"topic_aliases": {"x": 1}}])
def test_invalid_public_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        MemoryService(FakeEmbedder(), "alpha", "fake", config=config)


@run_async
async def test_telemetry_counts_sources_and_survives_callback_failure(tmp_path):
    events = []
    config = {"cache_enabled": True, "prepared_cache_enabled": True, "calibration_path": calibration(tmp_path)}
    async with memory(config, record=lambda name, ms: events.append((name, ms))) as s:
        assert [(await s.lookup(q, "s")).source for q in ("Sarah", "Sarah", "actual Sarah")] == [
            "chroma", "exact_cache", "prepared_cache"]
        counts = Counter(name for name, ms in events)
        assert counts["queue"] == counts["retrieval"] == 3
        assert all(counts["retrieval." + source] == 1 for source in ("chroma", "exact_cache", "prepared_cache"))
        assert all(ms >= 0 for name, ms in events)
        def broken(*args):
            raise RuntimeError("profiler unavailable")
        s.record = broken
        assert (await s.lookup("Sarah", "s")).source == "exact_cache"
        s.observe_transcript("music ", "s", "1")
        await until(lambda: len(s.documents) == 6)


@run_async
async def test_telemetry_captures_queue_and_work_after_timeout():
    events, entered, release = [], threading.Event(), threading.Event()
    async with memory(record=lambda name, ms: events.append((name, ms))) as s:
        def block():
            entered.set()
            release.wait(1)
        s.worker.submit(block)
        assert await asyncio.to_thread(entered.wait, 1)
        task = asyncio.create_task(s.lookup("Sarah", "s"))
        await asyncio.sleep(.02)
        release.set()
        await task
        assert dict(events)["retrieval"] >= dict(events)["queue"] >= 10
        s.embedder.entered.clear()
        s.embedder.release = threading.Event()
        task = asyncio.create_task(s.lookup("music", "s"))
        assert await asyncio.to_thread(s.embedder.entered.wait, 1)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, .02)
        finally:
            s.embedder.release.set()
        await asyncio.wrap_future(s.worker.submit(lambda: None))
        assert Counter(name for name, ms in events)["retrieval"] == 2
        assert dict(events)["retrieval"] >= 15


@run_async
async def test_preparation_telemetry_has_no_idle_repeats():
    events = []
    async with memory(record=lambda name, ms: events.append((name, ms))) as s:
        s.observe_transcript("family ", "s", "1")
        await until(lambda: bool(events))
        assert len(events) == 1 and events[0][0] == "preparation" and events[0][1] >= 0
        s.wake.set()
        await asyncio.sleep(.01)
        assert len(events) == 1
