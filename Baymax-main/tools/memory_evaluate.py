#!/usr/bin/env python3
"""Offline cache calibration and repeatable retrieval replay. See docs/early-memory.md.

Real calibration always uses MiniLM + a new, isolated synthetic Chroma store.
The fake backend can exercise tools/tests but cannot produce an enablement artifact.
No hardware-performance or patient-wait gates are inferred from this replay.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import resource
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_memory import MemoryService, MemorySettings, model_fingerprint
from ember_timing import Timeline
from ember_topics import TopicMatcher, topic_vocabulary
from tools.memory_fixture import query_snapshots, synthetic_fixture


class ReplayClock:
    def __init__(self, realtime):
        self.realtime, self.now = realtime, time.monotonic()

    def __call__(self):
        return time.monotonic() if self.realtime else self.now

    async def advance(self, seconds):
        if self.realtime:
            await asyncio.sleep(seconds)
        else:
            self.now += seconds
            # Let network/model tasks run; these pauses are not timed retrieval.
            await asyncio.sleep(.001)


class HashEmbedder:
    """Deterministic bag-of-words stand-in. Never a MiniLM quality benchmark."""
    def __init__(self, memories):
        self._collection = self
        self.records = [{"id": m["id"], "document": m["text"], "metadata": {"tags": m["tags"]} if m.get("tags") else {},
                         "embedding": self.embed(m["text"])[0]} for m in memories]

    def embed(self, text):
        from ember_topics import tokens
        vector = np.zeros(96, dtype=np.float32)
        for word in tokens(text):
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:2], "big") % len(vector)] += 1
        norm = np.linalg.norm(vector)
        return np.asarray([vector / norm if norm else vector])

    def search_by_embedding(self, vector, n_results=3, where=None, include_embeddings=False):
        norm = np.linalg.norm(vector)
        vector = vector / norm if norm else vector
        results = sorted(({**r, "distance": float(1-np.dot(vector, r["embedding"]))}
                          for r in self.records if matches_filter(r, where)), key=lambda r: r["distance"])[:n_results]
        return [{k: v for k, v in r.items() if k != "embedding" or include_embeddings} for r in results]

    def get(self, limit=50):
        return {"ids": [r["id"] for r in self.records[:limit]],
                "documents": [r["document"] for r in self.records[:limit]]}


def matches_filter(record, where):
    """The topic-filter subset for the offline stand-in backend."""
    if not where:
        return True
    if "$or" in where:
        return any(matches_filter(record, item) for item in where["$or"])
    return where["tags"]["$contains"] in record["metadata"].get("tags", [])


def validate_dataset(dataset):
    if dataset.get("version") != 1:
        raise ValueError("unsupported dataset version")
    ids = {m["id"]: m["patient_id"] for m in dataset["memories"]}
    if len(ids) != len(dataset["memories"]):
        raise ValueError("memory IDs must be unique")
    for memory in dataset["memories"]:
        tags = memory.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or t not in topic_vocabulary() for t in tags):
            raise ValueError("memory tags must use canonical topics")
    conversations = dataset["conversations"]
    if len({c["id"] for c in conversations}) != len(conversations):
        raise ValueError("development and heldout conversations must have different IDs")
    if {c["split"] for c in conversations} != {"development", "heldout"}:
        raise ValueError("separate development and heldout conversations are required")
    seen = {}
    for conversation in conversations:
        signature = (conversation["patient_id"], tuple(tuple((f["offset_ms"], f["text"])
            for f in turn["fragments"]) for turn in conversation["turns"]))
        if signature in seen and seen[signature] != conversation["split"]:
            raise ValueError("heldout conversations must not duplicate development transcripts")
        seen[signature] = conversation["split"]
        for turn in conversation["turns"]:
            offsets = [f["offset_ms"] for f in turn["fragments"]]
            if not offsets or offsets != sorted(offsets) or offsets[0] < 0:
                raise ValueError("transcript fragments must be in arrival order")
            if any(ids.get(i) != conversation["patient_id"] for i in turn["expected_ids"]):
                raise ValueError("expected memories must belong to the conversation's patient")
    return dataset


def make_adapters(dataset, backend, model_dir, directory):
    adapters = {}
    model = None
    for index, patient in enumerate(sorted({m["patient_id"] for m in dataset["memories"]})):
        memories = [m for m in dataset["memories"] if m["patient_id"] == patient]
        if backend == "fake":
            adapter = HashEmbedder(memories)
        else:
            from semantic_embedder import SemanticEmbedder
            if model is None:
                model = SemanticEmbedder(model_dir=model_dir, chroma_dir=None, verbose=False)
            adapter = copy.copy(model)
            adapter._setup_chroma(Path(directory) / str(index), "replay_memories")
            adapter.save([m["text"] for m in memories], ids=[m["id"] for m in memories],
                         metadatas=[{"tags": m["tags"]} if m.get("tags") else {"source": "untagged_replay"}
                                    for m in memories])
        adapters[patient] = adapter
    return adapters


def recall(records, expected):
    return len(set(records) & set(expected)) / len(expected) if expected else 1.0


def correctness(rows, threshold):
    """Evaluate the actual serving rule; unconfident/short candidates fall back."""
    baseline, selected, wrong, served = [], [], 0, 0
    for row in rows:
        use_cache = (len(row["candidate_ids"]) == 3 and row["candidate_score"] is not None
                     and row["candidate_score"] >= threshold)
        ids = row["candidate_ids"] if use_cache else row["baseline_ids"]
        served += int(use_cache)
        baseline.append(recall(row["baseline_ids"], row["expected_ids"]))
        selected.append(recall(ids, row["expected_ids"]))
        wrong += sum(row["owners"].get(i) != row["patient_id"] for i in ids)
    loss = float(np.mean(baseline) - np.mean(selected)) if rows else 1.0
    return {"cases": len(rows), "served": served, "wrong_person_results": wrong,
            "recall_loss": loss, "passed": len(rows) >= 20 and served >= 3 and wrong == 0 and loss <= .02 + 1e-12}


def choose_threshold(development, heldout):
    sweep = [{"threshold": round(.30 + .05*i, 2),
              **correctness(development, round(.30 + .05*i, 2))} for i in range(14)]
    chosen = next((r for r in sweep if r["passed"]), None)
    # Select using development only; never keep searching thresholds on heldout.
    verification = correctness(heldout, chosen["threshold"]) if chosen else None
    return chosen, verification, sweep


async def collect_comparisons(dataset, adapters, fingerprint):
    rows = {"development": [], "heldout": []}
    owners = {m["id"]: m["patient_id"] for m in dataset["memories"]}
    for conversation in dataset["conversations"]:
        service = MemoryService(adapters[conversation["patient_id"]], conversation["patient_id"], fingerprint,
                                settings=MemorySettings(comparison_enabled=True, prefetch_enabled=False))
        sid = uuid.uuid4().hex
        await service.start_session(sid)
        try:
            for turn, query in query_snapshots(conversation):
                tid = uuid.uuid4().hex
                result = await service.lookup(query, sid, tid)
                rows[conversation["split"]].append({"baseline_ids": [r["id"] for r in result.records],
                    "candidate_ids": [r["id"] for r in result.candidate_records],
                    "candidate_score": result.candidate_score, "expected_ids": turn["expected_ids"],
                    "owners": owners, "patient_id": conversation["patient_id"]})
                # Isolate ranking from scheduling, using only topics actually
                # observed in this turn, never expected IDs or predicted phrases.
                matcher = TopicMatcher()
                topics = set()
                for fragment in turn["fragments"]:
                    topics.update(matcher.feed(fragment["text"], fragment["offset_ms"] / 1000))
                topics.update(matcher.poll(turn["fragments"][-1]["offset_ms"] / 1000 + .21))
                if topics:
                    snapshot = {"token": service._token(), "turn_id": tid,
                                "epoch": service._preparation_epoch, "topics": tuple(sorted(topics))}
                    await asyncio.wrap_future(service.worker.submit(
                        lambda wait: service._prefetch(snapshot, wait), background=True))
        finally:
            await service.close()
    return rows


async def benchmark_run(dataset, adapters, fingerprint, mode, args, repetition):
    enabled = mode != "off"
    clock = ReplayClock(args.realtime)
    timeline = Timeline(Path(args.output).parent / "traces" / f"{mode}-{repetition}",
                        enabled=enabled, benchmark=True)
    samples, sources, coverages, correct, late, timeout = [], Counter(), [], [], 0, 0
    wrong_person = 0
    owners = {m["id"]: m["patient_id"] for m in dataset["memories"]}
    before = resource.getrusage(resource.RUSAGE_SELF)
    count = 0
    while count < args.turns:
        for conversation in dataset["conversations"]:
            if count >= args.turns:
                break
            service = MemoryService(adapters[conversation["patient_id"]], conversation["patient_id"], fingerprint,
                clock=clock, timing=timeline,
                settings=MemorySettings(exact_enabled=mode in ("reactive", "topic"),
                    comparison_enabled=mode in ("reactive", "topic"),
                    prepared_enabled=mode in ("reactive", "topic"),
                    prefetch_enabled=mode == "topic", calibration_path=args.calibration or ""))
            sid = uuid.uuid4().hex
            await service.start_session(sid)
            await service.profile_records()
            words = []
            try:
                for turn in conversation["turns"]:
                    if count >= args.turns:
                        break
                    tid, elapsed_offset, task = uuid.uuid4().hex, 0, None
                    async def lookup(query):
                        t = time.perf_counter()
                        result = await service.lookup(query, sid, tid)
                        return result, (time.perf_counter()-t)*1000
                    for fragment in turn["fragments"]:
                        await clock.advance((fragment["offset_ms"]-elapsed_offset)/1000)
                        elapsed_offset = fragment["offset_ms"]
                        service._wake.set()
                        text = fragment["text"]
                        if text.strip():
                            words.extend(text.strip().split())
                            words[:] = words[-30:]
                            if task is None:
                                task = asyncio.create_task(lookup(" ".join(words)))
                        if text:
                            service.observe_transcript(text, {"session_id": sid, "turn_id": tid})
                    if task is None:
                        continue
                    result, elapsed = await task
                    samples.append(elapsed)
                    sources[result.source] += 1
                    ids = [r["id"] for r in result.records]
                    wrong_person += sum(owners.get(i) != conversation["patient_id"] for i in ids)
                    correct.append(recall(ids, turn["expected_ids"]))
                    coverages.append(int(result.source != "chroma" and set(ids) >= set(turn["expected_ids"])))
                    budget = turn.get("reply_offset_ms", 900) - turn["fragments"][0]["offset_ms"]
                    late += int(elapsed > budget)
                    timeout += int(elapsed > budget + 150)
                    count += 1
                    await clock.advance(turn.get("gap_ms", 15_000)/1000)
                    service._wake.set()
                    # Complete eligible local work before advancing virtual turns.
                    await asyncio.sleep(0)
                    await asyncio.wrap_future(service.worker.submit(lambda wait: None, background=True))
            finally:
                await service.close()
    aggregates = timeline.aggregate()
    await asyncio.to_thread(timeline.close)
    after = resource.getrusage(resource.RUSAGE_SELF)
    return {"mode": mode, "repetition": repetition, "turns": len(samples),
            "retrieval_ms": {f"p{p}": float(np.percentile(samples, p)) for p in (50, 95, 99)},
            "sources": dict(sources), "relevant_memory_recall": float(np.mean(correct)),
            "correct_cache_coverage": float(np.mean(coverages)),
            "wrong_person_results": wrong_person,
            "late_replay_budget_count": late, "timeout_replay_budget_count": timeout,
            "cpu_seconds": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
            "process_peak_rss_bytes": after.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
            "aggregate": aggregates}


def local_gate_observations(runs):
    """Local replay observations only; audio/hardware gates remain outstanding."""
    observations = []
    for repetition in sorted({r["repetition"] for r in runs}):
        group = [r for r in runs if r["repetition"] == repetition]
        off, instrumented, reactive = (next(r for r in group if r["mode"] == mode)
                                      for mode in ("off", "instrumentation", "reactive"))
        p95 = lambda r: r["retrieval_ms"]["p95"]
        overhead = p95(instrumented) - p95(off)
        gain = 1 - p95(reactive) / max(p95(instrumented), 1e-9)
        loss = instrumented["relevant_memory_recall"] - reactive["relevant_memory_recall"]
        observations.append({"repetition": repetition, "instrumentation_p95_overhead_ms": overhead,
                             "instrumentation_gate": overhead <= 2,
                             "reactive_p95_improvement_fraction": gain, "caching_latency_gate": gain >= .10,
                             "reactive_recall_loss": loss,
                             "correctness_gate": loss <= .02 + 1e-12 and
                                 reactive["wrong_person_results"] <= instrumented["wrong_person_results"],
                             "topic_preparation": [{
                                 "p95_improvement_fraction": 1 - p95(r)/max(p95(reactive), 1e-9),
                                 "recall_loss": reactive["relevant_memory_recall"] - r["relevant_memory_recall"],
                                 "incremental_benefit_gate": p95(r) < p95(reactive) or
                                     r["late_replay_budget_count"] < reactive["late_replay_budget_count"] or
                                     r["timeout_replay_budget_count"] < reactive["timeout_replay_budget_count"]}
                                 for r in group if r["mode"] == "topic"]})
    return observations


async def evaluate(args):
    dataset = validate_dataset(json.loads(Path(args.dataset).read_text()) if args.dataset else synthetic_fixture())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = model_fingerprint(args.model_dir) if args.backend == "minilm-chroma" else "fake-not-calibratable"
    with tempfile.TemporaryDirectory(prefix="ember-replay-") as directory:
        adapters = await asyncio.to_thread(make_adapters, dataset, args.backend, args.model_dir, directory)
        if args.command == "calibrate":
            if args.backend != "minilm-chroma":
                raise ValueError("fake backend cannot create a calibration artifact")
            rows = await collect_comparisons(dataset, adapters, fingerprint)
            chosen, verification, sweep = choose_threshold(rows["development"], rows["heldout"])
            report = {"backend": args.backend, "model_fingerprint": fingerprint,
                      "retrieval_settings": MemorySettings().retrieval_identity(),
                      "dataset_sha256": hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest(),
                      "threshold": chosen["threshold"] if chosen else None,
                      "development_passed": bool(chosen),
                      "heldout_passed": bool(verification and verification["passed"]),
                      "heldout": verification, "development_sweep": sweep}
        else:
            runs = []
            for repetition in range(args.repetitions):
                for mode in ("off", "instrumentation", "reactive", "topic"):
                    runs.append(await benchmark_run(dataset, adapters, fingerprint, mode, args, repetition))
            report = {"backend": args.backend, "realtime_replay": args.realtime,
                      "model_fingerprint": fingerprint, "preparation": "detected topics with tag filtering; no Gemini network requests",
                      "runs": runs, "deployment_gates_passed": False,
                      "local_gate_observations": local_gate_observations(runs),
                      "pending": ["representative calibration", "Live transcript timing and patient wait",
                                  "LattePanda camera/pose/audio/dashboard load", "audio underruns and interruptions",
                                  "steady-state RSS growth", "two-hour stability run"]}
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Saved {args.command} report to {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("calibrate", "benchmark"))
    parser.add_argument("--backend", choices=("fake", "minilm-chroma"), default="fake")
    parser.add_argument("--model-dir", default="all-MiniLM-L6-v2-onnx")
    parser.add_argument("--dataset", help="JSON dataset using the memory_fixture schema")
    parser.add_argument("--output", required=True)
    parser.add_argument("--calibration", help="passing artifact required to serve prepared records")
    parser.add_argument("--turns", type=int, default=200)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--realtime", action="store_true", help="preserve fragment and between-turn wall-clock gaps")
    args = parser.parse_args()
    if args.turns <= 0 or args.repetitions <= 0:
        parser.error("turns and repetitions must be positive")
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
