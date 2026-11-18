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
            await service.lookup("family Sarah", "s", "1")
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


async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(.002)
    assert predicate(), "background preparation did not complete"


def test_topics_start_immediately_merge_cues_and_search_at_five_seconds():
    async def run():
        clock, events, embedder = Clock(), Events(), FakeEmbedder()
        service = MemoryService(embedder, "alpha", "fake", timing=events, clock=clock,
                                settings=MemorySettings(topic_aliases={"Sarah": "family"}))
        try:
            await service.start_session("s")
            context = {"session_id": "s", "turn_id": "1"}
            service.observe_transcript("Sa", context)
            service.observe_transcript("rah ", context)
            await wait_until(lambda: len(service._documents) == 3)
            assert embedder.search_calls == [(8, True, {"tags": {"$contains": "family"}})]
            service.observe_transcript("music ", context)
            service.observe_transcript("appointment ", context)
            service.observe_transcript("music Sarah ", context)
            assert service._pending["topics"] == ("appointments", "music")
            clock.advance(4.99)
            service._wake.set()
            await asyncio.sleep(.01)
            assert len(embedder.search_calls) == 1
            clock.advance(.01)
            service._wake.set()
            await wait_until(lambda: len(service._documents) == 9)
            assert embedder.search_calls[-1][2] == {"$or": [
                {"tags": {"$contains": "appointments"}}, {"tags": {"$contains": "music"}}]}
            clock.advance(50)
            service._wake.set()
            await asyncio.sleep(.01)
            assert len(embedder.search_calls) == 2  # No idle searches or repeated cues.
            assert not any("prediction" in event[0] for event in events.events)
        finally:
            await service.close()
    asyncio.run(run())


def test_new_utterance_replaces_pending_and_reconnect_preserves_budget():
    async def run():
        clock, embedder = Clock(), FakeEmbedder()
        service = MemoryService(embedder, "alpha", "fake", clock=clock)
        try:
            await service.start_session("s")
            context = {"session_id": "s", "turn_id": "1"}
            service.observe_transcript("family ", context)
            await wait_until(lambda: len(service._documents) == 3)
            service.observe_transcript("music ", context)
            service.observe_transcript("appointment ", {**context, "turn_id": "2"})
            assert service._pending["topics"] == ("appointments",)
            clock.advance(5)
            service._wake.set()
            await wait_until(lambda: len(service._documents) == 6)
            assert embedder.embed_calls == ["family", "appointments"]
            service.observe_transcript("music ", {**context, "turn_id": "3"})
            await service.start_session("new")
            assert service._pending is None and not service._documents
            service.observe_transcript("music ", {"session_id": "new", "turn_id": "4"})
            await asyncio.sleep(.01)
            assert len(embedder.embed_calls) == 2
            clock.advance(5)
            service._wake.set()
            await wait_until(lambda: len(service._documents) == 3)
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("discard", ["discard_preparation", "invalidate"])
def test_interruption_and_revision_discard_pending_and_inflight_results(discard):
    async def run():
        embedder = FakeEmbedder()
        embedder.release = threading.Event()
        service = MemoryService(embedder, "alpha", "fake")
        try:
            await service.start_session("s")
            context = {"session_id": "s", "turn_id": "1"}
            service.observe_transcript("family ", context)
            assert await asyncio.to_thread(embedder.entered.wait, 1)
            service.observe_transcript("music ", context)
            getattr(service, discard)()
            embedder.release.set()
            await asyncio.sleep(.02)
            assert not service._documents and service._pending is None
            assert len(embedder.embed_calls) == 1 and not embedder.search_calls
        finally:
            embedder.release.set()
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


def test_terminal_cue_debounces_and_opt_out_ignores_transcripts():
    async def run():
        clock, embedder = Clock(), FakeEmbedder()
        service = MemoryService(embedder, "alpha", "fake", clock=clock)
        try:
            await service.start_session("s")
            context = {"session_id": "s", "turn_id": "1"}
            service.observe_transcript("family", context)
            await asyncio.sleep(0)
            assert not embedder.embed_calls
            clock.advance(.21)
            await wait_until(lambda: len(service._documents) == 3)
        finally:
            await service.close()
        service = MemoryService(embedder, "alpha", "fake", settings=MemorySettings(prefetch_enabled=False))
        try:
            await service.start_session("s")
            service.observe_transcript("music ", context)
            await asyncio.sleep(.01)
            assert embedder.embed_calls == ["family"]
        finally:
            await service.close()
    asyncio.run(run())


def test_untagged_background_miss_does_not_retry_but_foreground_still_searches():
    async def run():
        embedder = FakeEmbedder()
        for record in embedder.records:
            record["metadata"].pop("tags")
        service = MemoryService(embedder, "alpha", "fake")
        try:
            await service.start_session("s")
            service.observe_transcript("family ", {"session_id": "s", "turn_id": "1"})
            await wait_until(lambda: bool(service._preparation_searches))
            assert not service._documents and len(embedder.search_calls) == 1
            result = await service.lookup("Sarah", "s", "1")
            assert len(result.records) == 3 and result.source == "chroma"
            assert embedder.search_calls[-1][2] is None
        finally:
            await service.close()
    asyncio.run(run())


def test_legacy_calibration_is_rejected(tmp_path):
    service = MemoryService(FakeEmbedder(), "alpha", "fake", settings=MemorySettings(
        prepared_enabled=True, calibration_path=calibration(tmp_path,
            retrieval_settings={"metric": "cosine", "top_k": 3, "max_distance": 1.0})))
    assert not service.calibrated
    asyncio.run(service.close())


def test_new_configuration_defaults_and_retired_prediction_fields():
    settings = MemorySettings.from_config({"prediction_enabled": False,
        "prediction_interval_seconds": 30, "prediction_max_topics": 5})
    assert settings.prefetch_enabled and settings.prefetch_interval == 5
    settings = MemorySettings.from_config({"prefetch_enabled": False, "prefetch_interval_seconds": 10})
    assert not settings.prefetch_enabled and settings.prefetch_interval == 10


@pytest.mark.parametrize("settings", [{"max_queries": 129}, {"max_documents": 257},
                                     {"max_bytes": 8*1024*1024+1}, {"ttl_seconds": 301},
                                     {"prefetch_interval": 4.99}, {"prefetch_top_k": 9},
                                     {"exact_enabled": "true"}])
def test_resource_limits_cannot_be_bypassed(settings):
    with pytest.raises(ValueError):
        MemorySettings(**settings)
