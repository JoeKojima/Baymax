import asyncio
import json
import threading
from dataclasses import replace

import numpy as np
import pytest

from ember_memory import MemoryService, MemorySettings, PatientContext, PriorityWorker
from conftest import Clock, FakeEmbedder


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
        embedder = FakeEmbedder()
        service = MemoryService(embedder, "alpha", "changed-model",
            settings=MemorySettings(prepared_enabled=True, calibration_path=calibration(tmp_path)))
        try:
            await service.start_session("s")
            assert not service.calibrated
            await service.lookup("Sarah", "s", "1")
            with service._lock:
                next(iter(service._documents.values())).vector = np.ones(7)
            assert (await service.lookup("actual Sarah", "s", "2")).source == "chroma"
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
    def block():
        entered.set()
        release.wait(2)
        order.append("running-background")
    first = worker.submit(block, background=True)
    assert entered.wait(1)
    second = worker.submit(lambda: order.append("pending-background"), background=True)
    foreground = worker.submit(lambda: order.append("foreground"))
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
        clock, embedder = Clock(), FakeEmbedder()
        service = MemoryService(embedder, "alpha", "fake", clock=clock,
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
            await wait_until(lambda: bool(embedder.search_calls))
            await asyncio.wrap_future(service.worker.submit(lambda: None))
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


def test_profiling_counts_retrieval_sources_without_changing_results(tmp_path):
    async def run():
        measurements = []
        service = MemoryService(FakeEmbedder(), "alpha", "fake",
            record=lambda name, ms: measurements.append((name, ms)),
            settings=MemorySettings(exact_enabled=True, prepared_enabled=True,
                                    calibration_path=calibration(tmp_path)))
        try:
            await service.start_session("s")
            sources = [(await service.lookup(query, "s", "1")).source
                       for query in ("Sarah", "Sarah", "actual Sarah")]
            assert sources == ["chroma", "exact_cache", "prepared_cache"]
            names = [name for name, ms in measurements]
            assert names.count("queue") == names.count("retrieval") == 3
            assert all(names.count("retrieval." + source) == 1 for source in sources)
            assert all(ms >= 0 for name, ms in measurements)
        finally:
            await service.close()
    asyncio.run(run())


def test_profiling_includes_queue_and_work_finishing_after_caller_timeout():
    async def run():
        measurements = []
        entered, release = threading.Event(), threading.Event()
        service = MemoryService(FakeEmbedder(), "alpha", "fake",
            record=lambda name, ms: measurements.append((name, ms)))
        def block():
            entered.set()
            release.wait(1)
        try:
            await service.start_session("s")
            service.worker.submit(block)
            assert await asyncio.to_thread(entered.wait, 1)
            task = asyncio.create_task(service.lookup("Sarah", "s", "1"))
            await asyncio.sleep(.02)
            release.set()
            await task
            first = dict(measurements)
            assert first["queue"] >= 10 and first["retrieval"] >= first["queue"]
            original = service.embedder.embed
            finish = threading.Event()
            service.embedder.entered.clear()
            def slow(query):
                value = original(query)
                finish.wait(1)
                return value
            service.embedder.embed = slow
            task = asyncio.create_task(service.lookup("music", "s", "2"))
            try:
                assert await asyncio.to_thread(service.embedder.entered.wait, 1)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, .02)
            finally:
                finish.set()
            await asyncio.wrap_future(service.worker.submit(lambda: None))
            assert sum(name == "retrieval" for name, ms in measurements) == 2
            assert dict(measurements)["retrieval"] >= 15
        finally:
            release.set()
            await service.close()
    asyncio.run(run())


def test_profiling_preparation_attempts_have_no_idle_repeats():
    async def run():
        measurements = []
        service = MemoryService(FakeEmbedder(), "alpha", "fake",
            record=lambda name, ms: measurements.append((name, ms)))
        try:
            await service.start_session("s")
            service.observe_transcript("family ", {"session_id": "s", "turn_id": "1"})
            await wait_until(lambda: bool(measurements))
            assert len(measurements) == 1 and measurements[0][0] == "preparation"
            assert measurements[0][1] >= 0
            service._wake.set()
            await asyncio.sleep(.01)
            assert len(measurements) == 1
        finally:
            await service.close()
    asyncio.run(run())


def test_broken_profiling_callback_cannot_break_cache_or_retrieval():
    async def run():
        def broken(name, duration):
            raise RuntimeError("profiling unavailable")
        service = MemoryService(FakeEmbedder(), "alpha", "fake", record=broken,
                                settings=MemorySettings(exact_enabled=True))
        try:
            await service.start_session("s")
            assert (await service.lookup("Sarah", "s", "1")).source == "chroma"
            assert (await service.lookup("Sarah", "s", "2")).source == "exact_cache"
            service.observe_transcript("music ", {"session_id": "s", "turn_id": "3"})
            await wait_until(lambda: len(service._documents) == 6)
        finally:
            await service.close()
    asyncio.run(run())
