import asyncio
from argparse import Namespace

import pytest

from tools.memory_evaluate import choose_threshold, correctness, evaluate, local_gate_observations, validate_dataset
from tools.memory_fixture import query_snapshots, synthetic_fixture


def row(score=.8, good=True):
    return {"baseline_ids": ["a", "b", "c"], "candidate_ids": ["a", "b", "c"] if good else ["d", "e", "f"],
            "candidate_score": score, "expected_ids": ["a", "b", "c"], "patient_id": "p",
            "owners": {i: "p" for i in "abcdef"}}


def test_threshold_is_chosen_on_development_then_frozen_for_heldout():
    development = [row(.8)] * 25 + [row(.5, good=False)] * 5
    chosen, heldout, sweep = choose_threshold(development, [row(.8, good=False)] * 30)
    assert chosen["threshold"] == .55
    assert not heldout["passed"]  # Must not retune to .85 after seeing heldout.
    assert len(sweep) == 14 and sweep[-1]["threshold"] == .95
    assert not correctness([], .3)["passed"]
    bad = row()
    bad["owners"] = {"a": "someone_else", "b": "p", "c": "p"}
    assert not correctness([bad]*30, .3)["passed"]


def test_fixture_replays_fragments_and_real_rolling_snapshot():
    fixture = validate_dataset(synthetic_fixture())
    conversation = fixture["conversations"][0]
    snapshots = list(query_snapshots(conversation))
    assert snapshots[0][1] == snapshots[1][1]
    assert len(snapshots[0][1].split()) == 30
    # Existing frontend splits fragments before adding words; preserve that
    # behaviour in replay rather than silently replacing it with clean text.
    assert "visiting" in snapshots[1][1]
    assert len(snapshots[-1][1].split()) <= 30


def test_heldout_transcripts_are_distinct_and_duplicate_conversations_are_rejected():
    import copy
    fixture = synthetic_fixture()
    validate_dataset(fixture)
    development = next(c for c in fixture["conversations"] if c["split"] == "development")
    heldout = next(c for c in fixture["conversations"] if c["split"] == "heldout")
    assert next(query_snapshots(development))[1] != next(query_snapshots(heldout))[1]
    heldout["turns"] = copy.deepcopy(development["turns"])
    with pytest.raises(ValueError, match="duplicate"):
        validate_dataset(fixture)


def test_fake_backend_cannot_produce_calibration_artifact(tmp_path):
    args = Namespace(command="calibrate", dataset=None, backend="fake", model_dir="unused",
                     output=str(tmp_path / "artifact.json"))
    with pytest.raises(ValueError, match="fake backend"):
        asyncio.run(evaluate(args))
    assert not (tmp_path / "artifact.json").exists()


def test_gate_report_requires_latency_benefit_and_checks_recall_loss():
    def run(mode, p95, recall=.9, topics=2):
        return {"mode": mode, "topics": topics, "repetition": 0, "retrieval_ms": {"p95": p95},
                "relevant_memory_recall": recall, "wrong_person_results": 0,
                "late_replay_budget_count": 0, "timeout_replay_budget_count": 0,
                "sources": {"exact_cache": 999}}
    results = local_gate_observations([run("off", 1), run("instrumentation", 2),
        run("reactive", 1.5, .87), run("prediction", 1.5, topics=2), run("prediction", 1.4, topics=3)])
    assert results[0]["instrumentation_gate"]
    assert results[0]["caching_latency_gate"]
    assert not results[0]["correctness_gate"]
    assert not results[0]["prediction"][0]["incremental_benefit_gate"]
    assert results[0]["prediction"][1]["incremental_benefit_gate"]
