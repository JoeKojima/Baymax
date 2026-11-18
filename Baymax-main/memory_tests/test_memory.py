import asyncio
import json
import threading

import numpy as np
import pytest
import ember_memory as implementation
from ember_memory import PatientContext, PriorityWorker, RETRIEVAL
from conftest import Clock, FakeEmbedder, memory, run_async, until


def calibration(tmp_path, **changes):
    data = dict(threshold=.75, model_fingerprint="fake", backend="minilm-chroma",
                retrieval_settings=RETRIEVAL, development_passed=True, heldout_passed=True)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data | changes))
    return str(path)


@run_async
async def test_exact_hits_skip_work_and_do_not_extend_expiry():
    clock, events = Clock(), []
    async with memory({"cache_enabled": True}, clock=clock,
                      record=lambda name, ms: events.append((name, ms))) as s:
        first = await s.lookup("Sarah  garden", "s")
        clock.advance(299)
        hit = await s.lookup(" Sarah\n garden ", "s")
        assert hit.source == "exact_cache" and first.records == hit.records
        assert len(s.embedder.embed_calls) == len(s.embedder.search_calls) == 1
        assert [name for name, _ in events].count("retrieval.exact_cache") == 1
        assert [name for name, _ in events].count("retrieval.chroma") == 1
        assert dict(events)["retrieval"] >= dict(events)["queue"] >= 0
        clock.advance(1)
        assert (await s.lookup("Sarah garden", "s")).source == "chroma"
        await s.start_session("new")
        assert not s.queries and not s.documents
        assert (await s.lookup("Sarah garden", "s")).source == "disabled"


@pytest.mark.parametrize("changes", [{}, {"heldout_passed": False}, {"model_fingerprint": "other"},
    {"retrieval_settings": RETRIEVAL | {"preparation": "topic-tags-v1"}},
    {"threshold": float("nan")}, {"threshold": .1}])
@run_async
async def test_prepared_scoring_requires_current_calibration(tmp_path, changes):
    config = {"prepared_cache_enabled": True, "calibration_path": calibration(tmp_path, **changes)}
    async with memory(config) as s:
        await s.lookup("Sarah", "s")
        result = await s.lookup("actual Sarah", "s")
        assert result.source == ("chroma" if changes else "prepared_cache")
        assert len(s.embedder.search_calls) == (2 if changes else 1)
        for record in result.records:
            vector = next(r["embedding"] for r in s.embedder.records if r["id"] == record["id"])
            assert record["distance"] == pytest.approx(1 - np.dot(vector, [.8, .6, 0]), abs=1e-6)
        s.invalidate()
        s.embedder.records = s.embedder.records[:2]
        await s.lookup("Sarah", "s")
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


def test_patient_paths_do_not_reuse_legacy_store(tmp_path):
    for patient in ("alpha", "beta"):
        context = PatientContext.resolve(tmp_path, patient)
        context.prepare()
        assert context.chroma_dir == tmp_path / "patients" / patient / "chroma_store"
    with pytest.raises(ValueError):
        PatientContext.resolve(tmp_path, "../escape")


def test_worker_prioritizes_foreground():
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


@pytest.mark.parametrize("background,action", [
    (False, "invalidate"), (False, "reconnect"), (True, "discard_preparation"),
    (True, "invalidate"), (True, "reconnect")])
@run_async
async def test_stale_work_cannot_repopulate_cache(background, action):
    embedder = FakeEmbedder()
    embedder.release = threading.Event()
    async with memory(embedder=embedder) as s:
        if background:
            s.observe_transcript("family ", "s", "1")
        else:
            task = asyncio.create_task(s.lookup("Sarah", "s"))
        assert await asyncio.to_thread(embedder.entered.wait, 1)
        if action == "reconnect":
            await s.start_session("new")
        else:
            getattr(s, action)()
        embedder.release.set()
        if not background:
            assert (await task).source == "obsolete"
        await asyncio.wrap_future(s.worker.submit(lambda: None))
        assert not s.documents and not s.pending and not embedder.search_calls


@run_async
async def test_topic_cues_merge_deduplicate_and_obey_five_second_budget():
    clock, events = Clock(), []
    async with memory({"topic_aliases": {"Sarah": "family"}}, clock=clock,
                      record=lambda name, ms: events.append((name, ms))) as s:
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
        assert len(s.embedder.search_calls) == len(events) == 2
        assert all(name == "preparation" and ms >= 0 for name, ms in events)
        s.observe_transcript("family ", "s", "2")
        await until(lambda: len(s.embedder.search_calls) == 3)
        s.observe_transcript("music ", "s", "2")
        s.observe_transcript("appointment ", "s", "3")
        assert s.pending[2] == {"appointments"}
        await s.start_session("new")
        s.observe_transcript("music ", "new", "4")
        await asyncio.sleep(.01)
        assert len(s.embedder.search_calls) == 3
        clock.advance(5)
        s.wake.set()
        await until(lambda: len(s.documents) == 3)
        assert s.embedder.embed_calls[-1] == "music"
