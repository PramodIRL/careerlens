"""The evaluation regression policy (Prompt 5.4).

ALL PURE — no model, no database, no network, so every one of these runs
in CI. That is the point: the model-independent half of the policy
(fingerprints, config, comparison arithmetic) is what CI can honestly
enforce, and it is enforced here.

WHAT CI STILL CANNOT SEE. A change to app/embeddings/retrieval.py moves
ranking without touching any fingerprint, so these tests pass while
saying nothing about it. `make evaluate-check` locally is what covers
that, and test_the_policy_documents_its_own_blind_spot keeps the
limitation written down where someone reading the suite will find it.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.embeddings.semantic_fit import SIMILARITY_CEIL, SIMILARITY_FLOOR, TOP_K
from app.evaluation.policy import (
    CROWDING_WARN_DELTA,
    METRIC_TOLERANCE,
    compare,
    describe,
)
from app.evaluation.report import (
    EvaluationReport,
    MetricSet,
    current_config,
    dataset_fingerprint,
    from_json,
    to_json,
)

BASELINE_PATH = Path(__file__).resolve().parents[1] / "app" / "evaluation" / "baseline.json"


def _report(**overrides: object) -> EvaluationReport:
    base = EvaluationReport(
        model_identifier="test-model",
        dataset_version="test-v1",
        dataset_fingerprint="abc123",
        case_count=2,
        item_count=8,
        config={"ceil": 0.6, "floor": 0.2, "top_k": 5.0},
        metrics={
            3: MetricSet(0.75, 0.83, 0.90, 0.92, 1.0),
            5: MetricSet(0.52, 0.52, 1.00, 0.97, 1.0),
        },
        duplicate_slots_lost=3,
        per_case_ndcg_at_5={"query a": 1.0, "query b": 0.94},
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _with_ndcg5(value: float, **overrides: object) -> EvaluationReport:
    report = _report(**overrides)
    metrics = dict(report.metrics)
    metrics[5] = replace(metrics[5], ndcg=value)
    return replace(report, metrics=metrics)


# --- serialization ------------------------------------------------------


def test_serialization_is_deterministic_and_round_trips() -> None:
    """The baseline is a checked-in file reviewed in a diff. Unstable
    key order would produce noise diffs nobody reads."""
    report = _report()

    first, second = to_json(report), to_json(report)

    assert first == second
    assert first.endswith("\n")
    assert list(json.loads(first).keys()) == sorted(json.loads(first).keys())
    assert from_json(first) == report


def test_an_unknown_schema_version_is_refused() -> None:
    payload = json.loads(to_json(_report()))
    payload["schema_version"] = 999

    with pytest.raises(ValueError, match="regenerate"):
        from_json(json.dumps(payload))


# --- regression detection ----------------------------------------------


def test_a_drop_beyond_tolerance_is_a_regression_with_old_new_and_delta() -> None:
    baseline = _report()
    current = _with_ndcg5(0.97 - METRIC_TOLERANCE - 0.01)

    result = compare(baseline, current)

    assert not result.ok
    finding = next(f for f in result.regressions if f.metric == "ndcg@5")
    assert finding.baseline == 0.97
    assert finding.current == pytest.approx(0.94)
    assert finding.delta == pytest.approx(-0.03)
    assert "ndcg@5" in describe(result)


def test_a_drop_inside_tolerance_and_an_improvement_both_pass() -> None:
    baseline = _report()

    inside = compare(baseline, _with_ndcg5(0.97 - METRIC_TOLERANCE + 0.005))
    better = compare(baseline, _with_ndcg5(0.99))

    assert inside.ok and not inside.regressions
    assert better.ok and not better.regressions


def test_any_coverage_decrease_fails_however_small() -> None:
    """Coverage has no tolerance: a case that stopped returning anything
    means a user sees "nothing found" where they previously saw
    evidence."""
    baseline = _report()
    metrics = dict(baseline.metrics)
    metrics[5] = replace(metrics[5], coverage=0.99)

    result = compare(baseline, replace(baseline, metrics=metrics))

    assert not result.ok
    assert any(f.metric == "coverage@5" for f in result.regressions)


def test_duplicate_crowding_warns_but_never_blocks() -> None:
    baseline = _report()
    current = replace(baseline, duplicate_slots_lost=3 + CROWDING_WARN_DELTA + 1)

    result = compare(baseline, current)

    assert result.ok, "crowding is a quality signal, not a merge blocker"
    assert [f.kind for f in result.findings] == ["warning"]


def test_a_regression_names_the_worst_affected_cases() -> None:
    baseline = _report()
    current = _with_ndcg5(0.90, per_case_ndcg_at_5={"query a": 0.40, "query b": 0.94})

    result = compare(baseline, current)

    assert result.worst_cases[0] == ("query a", -0.6)
    assert "query a" in describe(result)


# --- mismatch is not a regression --------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("dataset_fingerprint", "different", "benchmark content changed"),
        ("model_identifier", "other-model", "model changed"),
        ("config", {"ceil": 0.5, "floor": 0.2, "top_k": 5.0}, "config changed"),
    ],
)
def test_a_changed_test_demands_a_rebaseline_rather_than_faking_a_delta(
    field: str, value: object, expected: str
) -> None:
    """Comparing across a changed benchmark, model or threshold set
    subtracts two numbers that measure different things. The answer is
    "re-baseline", never a delta."""
    baseline = _report()
    current = _report(**{field: value})

    result = compare(baseline, current)

    assert result.rebaseline_required
    assert not result.ok, "a refusal must not pass silently as if compared"
    assert result.regressions == ()
    assert expected in (result.rebaseline_reason or "")
    assert "SAME PR" in describe(result)


# --- the committed baseline --------------------------------------------


def test_the_committed_baseline_matches_the_current_dataset_and_config() -> None:
    """THE CI GUARD. Catches a benchmark edit, a provider swap or a
    FLOOR/CEIL/TOP_K change that was not re-baselined — all without
    needing model weights."""
    baseline = from_json(BASELINE_PATH.read_text())

    assert baseline.dataset_fingerprint == dataset_fingerprint(), (
        "the benchmark changed without a re-baseline — run `make evaluate-baseline` "
        "and commit it in the same PR as the change"
    )
    assert baseline.config == current_config(), (
        "semantic_fit_v1 constants changed without a re-baseline"
    )
    assert baseline.config == {"ceil": SIMILARITY_CEIL, "floor": SIMILARITY_FLOOR, "top_k": 5.0}
    assert TOP_K == 5


def test_the_committed_baseline_still_holds_the_5_3_measurements() -> None:
    """The numbers 5.3 reported and reviewed. If these move, the change
    was intentional and belongs in a reviewed diff."""
    baseline = from_json(BASELINE_PATH.read_text())

    assert (baseline.case_count, baseline.item_count) == (12, 56)
    assert baseline.metrics[3].ndcg == pytest.approx(0.9228, abs=5e-4)
    assert baseline.metrics[5].recall == pytest.approx(1.0)
    assert baseline.metrics[5].precision == baseline.metrics[5].precision_ceiling
    assert baseline.duplicate_slots_lost == 3


def test_the_policy_documents_its_own_blind_spot() -> None:
    """retrieval.py can change ranking without moving any fingerprint,
    so CI cannot validate such a change. That limitation must stay
    written down next to the code that has it."""
    source = (Path(__file__).resolve().parents[1] / "app" / "evaluation" / "policy.py").read_text()

    assert "retrieval.py" in source
    assert "locally" in source
