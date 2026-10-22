import asyncio
import json
import threading
from dataclasses import replace

import numpy as np
import pytest

from ember_memory import MemoryService, MemorySettings, PatientContext, PriorityWorker
from conftest import Clock, Events, FakeEmbedder


def calibration(tmp_path, **overrides):
    data = {"threshold": .75, "model_fingerprint": "fake", "backend": "minilm-chroma",
            "retrieval_settings": MemorySettings().retrieval_identity(),
            "development_passed": True, "heldout_passed": True}
    data.update(overrides)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_exact_hit_normalization_and_absolute_expiration():
    async def run():
        clock, embedder = Clock(), FakeEmbedder()
        service = MemoryService(embedder, "alpha", "fake", clock=clock,
                                settings=MemorySettings(exact_enabled=True))
        try:
            await service.start_session("s")
            first = await service.lookup("Sarah  garden", "s", "1")
            clock.advance(299)
            hit = await service.lookup(" Sarah\n garden ", "s", "2")
            assert hit.source == "exact_cache"
            assert len(embedder.embed_calls) == len(embedder.search_calls) == 1
            assert first.records == hit.records
            clock.advance(1)
            assert (await service.lookup("Sarah garden", "s", "3")).source == "chroma"
            assert len(embedder.embed_calls) == 2
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("passing", [True, False])
def test_prepared_scores_actual_question_and_requires_calibration(tmp_path, passing):
    async def run():
        path = calibration(tmp_path, heldout_passed=passing)
        embedder = FakeEmbedder()
        service = MemoryService(embedder, "alpha", "fake", settings=MemorySettings(
            prepared_enabled=True, comparison_enabled=True, calibration_path=path))
        try:
            await service.start_session("s")
            await service.lookup("prediction Sarah", "s", "1")
            result = await service.lookup("actual question Sarah", "s", "2")
            assert result.source == ("prepared_cache" if passing else "chroma")
            assert len(embedder.embed_calls) == 2
            assert len(embedder.search_calls) == (1 if passing else 2)
            assert len(result.candidate_records) == 3
            for record in result.records:
                vector = next(r["embedding"] for r in embedder.records if r["id"] == record["id"])
                assert record["distance"] == pytest.approx(1-np.dot(vector, [.8, .6, 0]), abs=1e-6)
        finally:
            await service.close()
    asyncio.run(run())


def test_insufficient_candidates_corrupt_vectors_and_fingerprint_fall_back(tmp_path):
    async def run():
        embedder, events = FakeEmbedder(), Events()
        service = MemoryService(embedder, "alpha", "changed-model", timing=events,
            settings=MemorySettings(prepared_enabled=True, calibration_path=calibration(tmp_path)))
        try:
            await service.start_session("s")
            assert not service.calibrated
            await service.lookup("Sarah", "s", "1")
            with service._lock:
                next(iter(service._documents.values())).vector = np.ones(7)
            assert (await service.lookup("actual Sarah", "s", "2")).source == "chroma"
            assert any(e[0] == "cache.error" for e in events.events)
            embedder.records = embedder.records[:2]
            service.invalidate()
            assert len((await service.lookup("Sarah", "s", "3")).records) == 2
            assert (await service.lookup("actual Sarah", "s", "4")).source == "chroma"
        finally:
            await service.close()
    asyncio.run(run())


def test_cache_capacity_revision_reconnect_and_patient_isolation(tmp_path):
    async def run():
        settings = MemorySettings(exact_enabled=True, max_queries=2, max_documents=4, max_bytes=3500)
        service = MemoryService(FakeEmbedder(), "alpha", "fake", settings=settings)
        other = MemoryService(FakeEmbedder("beta"), "beta", "fake", settings=settings)
        try:
            await service.start_session("s")
            await other.start_session("s")
            for query in ("Sarah", "music", "doctor", "Sarah again"):
                await service.lookup(query, "s", "1")
            assert len(service._queries) <= 2
            assert len(service._documents) <= 4
            assert service.cache_payload_bytes() <= 3500
            assert all(r["id"].startswith("beta") for r in (await other.lookup("Sarah", "s", "1")).records)
            service.invalidate(9)
            assert not service._documents and not service._queries
            assert (await service.lookup("Sarah", "s", "2")).memory_revision == 9
            await service.start_session("new")
            assert not service._documents
            assert (await service.lookup("Sarah", "s", "old")).source == "disabled"
        finally:
            await service.close()
            await other.close()
    asyncio.run(run())
    legacy = tmp_path / "chroma_store"
    legacy.mkdir()
    (legacy / "unassigned").write_text("legacy")
    assert PatientContext.resolve(tmp_path, "") is None
    patient = PatientContext.resolve(tmp_path, "alpha")
    patient.prepare()
    assert patient.chroma_dir != legacy
    assert (legacy / "unassigned").read_text() == "legacy"
    with pytest.raises(ValueError):
        PatientContext.resolve(tmp_path, "../alpha")


def test_stale_inflight_work_cannot_repopulate_cache():
    async def run():
        embedder = FakeEmbedder()
        embedder.release = threading.Event()
        service = MemoryService(embedder, "alpha", "fake", settings=MemorySettings(exact_enabled=True))
        await service.start_session("s")
        task = asyncio.create_task(service.lookup("Sarah", "s", "1"))
        assert await asyncio.to_thread(embedder.entered.wait, 1)
        service.invalidate()
        embedder.release.set()
        assert (await task).source == "obsolete"
        assert not service._documents
        await service.close()
    asyncio.run(run())


def test_foreground_priority_between_nonpreemptible_background_jobs():
    worker = PriorityWorker()
    entered, release = threading.Event(), threading.Event()
    order = []
    def block(wait):
        entered.set()
        release.wait(2)
        order.append("running-background")
    first = worker.submit(block, background=True)
    assert entered.wait(1)
    second = worker.submit(lambda wait: order.append("pending-background"), background=True)
    foreground = worker.submit(lambda wait: order.append("foreground"))
    release.set()
    for future in (first, second, foreground):
        future.result(2)
    worker.close()
    assert order == ["running-background", "foreground", "pending-background"]


def test_prediction_starts_from_partial_cue_and_retains_latest_pending_snapshot():
    async def run():
        clock, events, seen = Clock(), Events(), []
        started, release = asyncio.Event(), asyncio.Event()
        async def predictor(text, count):
            seen.append((text, count))
            started.set()
            await release.wait()
            return ["Sarah garden", "music"]
        service = MemoryService(FakeEmbedder(), "alpha", "fake", predictor=predictor,
            timing=events, clock=clock, settings=MemorySettings(prediction_enabled=True,
                                                              topic_aliases={"Sarah": "family"}))
        try:
            await service.start_session("s")
            context = {"session_id": "s", "turn_id": "1", "messages": [("robot", "Who is visiting?")]}
            service.observe_transcript("Sa", context)
            service.observe_transcript("rah is not visiting ", context)
            await asyncio.wait_for(started.wait(), .5)
            assert "Sarah is not visiting " in seen[0][0]
            assert seen[0][1] == 2
            # No sentence/reply completion signal was supplied.
            service.observe_transcript("music ", {**context, "turn_id": "2"})
            service.observe_transcript("appointment cancelled ", {**context, "turn_id": "3"})
            assert service._pending["turn_id"] == "3"
            release.set()
            for _ in range(100):
                await asyncio.sleep(.002)
                if len(service.embedder.search_calls) == 2:
                    break
            assert len(seen) == 1  # Throttled even though preparation finished.
            clock.advance(30)
            service._wake.set()
            for _ in range(100):
                await asyncio.sleep(.002)
                if len(seen) == 2:
                    break
            assert "appointment cancelled" in seen[1][0]
            assert "patient: music" not in seen[1][0]
        finally:
            await service.close()
    asyncio.run(run())


def test_prediction_timeout_no_retry_and_reconnect_discards_pending():
    async def run():
        attempts = []
        async def predictor(text, count):
            attempts.append(text)
            await asyncio.sleep(10)
        service = MemoryService(FakeEmbedder(), "alpha", "fake", predictor=predictor,
            settings=MemorySettings(prediction_enabled=True, prediction_timeout=.01))
        await service.start_session("s")
        service.observe_transcript("family ", {"session_id": "s", "turn_id": "1"})
        await asyncio.sleep(.04)
        assert len(attempts) == 1
        service.observe_transcript("music ", {"session_id": "s", "turn_id": "2"})
        await service.start_session("new")
        await asyncio.sleep(.02)
        assert len(attempts) == 1 and not service._documents and service._pending is None
        await service.close()
    asyncio.run(run())


def test_empty_exact_hits_and_cache_errors_fall_back_once():
    async def run():
        embedder = FakeEmbedder()
        embedder.records = []
        service = MemoryService(embedder, "alpha", "fake", settings=MemorySettings(exact_enabled=True))
        try:
            await service.start_session("s")
            assert (await service.lookup("empty", "s", "1")).records == []
            assert (await service.lookup("empty", "s", "2")).source == "exact_cache"
            assert len(embedder.embed_calls) == 1
            # Corrupt cached state must not prevent ordinary database retrieval.
            service._queries[service._key("empty")] = None
            assert (await service.lookup("empty", "s", "3")).source == "chroma"
            assert len(embedder.embed_calls) == 2
        finally:
            await service.close()
    asyncio.run(run())


def test_foreground_does_not_await_hung_predictor_and_terminal_cue_debounces():
    async def run():
        clock, attempts, events = Clock(), [], Events()
        started = asyncio.Event()
        async def predict(text, number):
            attempts.append(text)
            started.set()
            await asyncio.Event().wait()
        service = MemoryService(FakeEmbedder(), "alpha", "fake", clock=clock, predictor=predict,
            timing=events, settings=MemorySettings(prediction_enabled=True))
        try:
            await service.start_session("s")
            await service.profile_records()  # Learns Sarah, without configured aliases.
            service.observe_transcript("Sarah", {"session_id": "s", "turn_id": "1"})
            await asyncio.sleep(0)
            assert not attempts
            clock.advance(.21)
            await asyncio.wait_for(started.wait(), .4)
            # Foreground can complete while prediction remains stuck in network I/O.
            result = await asyncio.wait_for(service.lookup("actual Sarah", "s", "1"), .2)
            assert result.source == "chroma"
            await service.start_session("new")
            service.observe_transcript("music ", {"session_id": "new", "turn_id": "2"})
            await asyncio.sleep(.01)
            assert len(attempts) == 1  # Reconnect does not bypass the 30s attempt limit.
        finally:
            await service.close()
    asyncio.run(run())


def test_prediction_worker_result_after_revision_change_is_rejected():
    async def run():
        embedder, events = FakeEmbedder(), Events()
        embedder.release = threading.Event()
        async def predict(text, number):
            return ["Sarah garden", "music"]
        service = MemoryService(embedder, "alpha", "fake", predictor=predict, timing=events,
            settings=MemorySettings(prediction_enabled=True))
        await service.start_session("s")
        service.observe_transcript("family ", {"session_id": "s", "turn_id": "1"})
        assert await asyncio.to_thread(embedder.entered.wait, 1)
        service.invalidate()
        embedder.release.set()
        await asyncio.sleep(.02)
        assert not service._documents
        assert len(embedder.embed_calls) == 1  # Second phrase also rejected.
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("settings", [{"max_queries": 129}, {"max_documents": 257},
                                     {"max_bytes": 8*1024*1024+1}, {"ttl_seconds": 301},
                                     {"prediction_interval": 29}, {"prediction_timeout": 4},
                                     {"exact_enabled": "true"}])
def test_resource_limits_cannot_be_bypassed(settings):
    with pytest.raises(ValueError):
        MemorySettings(**settings)
