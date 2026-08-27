"""A comparable evaluation report, and the fingerprints that make it
comparable (Prompt 5.4).

WHY FINGERPRINTS. A metric is only meaningful next to the exact test
that produced it. Comparing today's NDCG against a baseline computed
over a different benchmark, a different model, or different thresholds
is not a regression check — it is two unrelated numbers being
subtracted. So the report carries the identity of everything that could
change the answer, and app/evaluation/policy.py refuses to compare
across a mismatch rather than reporting a fake delta.

READS PRODUCTION CONSTANTS, CHANGES NONE. `current_config()` imports
FLOOR, CEIL and TOP_K from app/embeddings/semantic_fit.py purely to
record them. Capturing them here is what makes a threshold change
detectable in CI without model weights — the constants are code, so a
mismatch is visible without embedding anything.

DETERMINISTIC ON PURPOSE. Sorted keys, fixed rounding, trailing
newline: the baseline is a checked-in file that has to diff cleanly in
review, and a report that reordered its own keys would produce noise
diffs that nobody reads.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from app.embeddings.semantic_fit import SIMILARITY_CEIL, SIMILARITY_FLOOR, TOP_K
from app.evaluation.dataset import BENCHMARK, DATASET_VERSION, total_items
from app.evaluation.metrics import (
    RankedItem,
    duplicate_crowding_at_k,
    has_result,
    ndcg_at_k,
    precision_at_k,
    precision_ceiling_at_k,
    recall_at_k,
)

SCHEMA_VERSION = 1

# The two windows the report covers. K=3 is where precision and recall
# discriminate; K=5 is what the product actually shows.
REPORTED_K = (3, 5)

# Four decimal places. Enough to see a real movement (the smallest
# meaningful one is ~0.017, a single case out of twelve), coarse enough
# that a different onnxruntime build's low-bit float differences do not
# churn the checked-in file.
_ROUND_DIGITS = 4


@dataclass(frozen=True)
class MetricSet:
    """Every headline metric at one K."""

    precision: float
    precision_ceiling: float
    recall: float
    ndcg: float
    coverage: float


@dataclass(frozen=True)
class EvaluationReport:
    model_identifier: str
    dataset_version: str
    dataset_fingerprint: str
    case_count: int
    item_count: int
    config: dict[str, float]
    metrics: dict[int, MetricSet]
    duplicate_slots_lost: int
    # Keyed by query text so a regression can name the cases it hurt.
    # NDCG is the per-case metric worth keeping: it is the one that
    # notices a distractor ranked above a genuine paraphrase.
    per_case_ndcg_at_5: dict[str, float]


def dataset_fingerprint() -> str:
    """SHA-256 over every query, evidence text and label.

    Content, not `DATASET_VERSION`: bumping the version string is a
    manual step someone will forget, whereas editing a case always
    changes this. The two are reported side by side so a version that
    stopped tracking its own content is visible.
    """
    digest = hashlib.sha256()
    for case in BENCHMARK:
        digest.update(case.query.encode("utf-8"))
        for item in case.items:
            digest.update(item.text.encode("utf-8"))
            digest.update(item.label.value.encode("utf-8"))
            digest.update((item.duplicate_group or "").encode("utf-8"))
    return digest.hexdigest()


def current_config() -> dict[str, float]:
    """The `semantic_fit_v1` constants this report was produced under."""
    return {"ceil": SIMILARITY_CEIL, "floor": SIMILARITY_FLOOR, "top_k": float(TOP_K)}


def build_report(
    ranked_cases: list[tuple[str, list[RankedItem]]], *, model_identifier: str
) -> EvaluationReport:
    """Aggregate ranked cases into a report. Pure — the model has
    already done its work by the time this is called, which is what lets
    the policy layer be tested without one."""
    cases = [ranked for _, ranked in ranked_cases]
    metrics: dict[int, MetricSet] = {}
    for k in REPORTED_K:
        metrics[k] = MetricSet(
            precision=_mean(precision_at_k(c, k) for c in cases),
            precision_ceiling=_mean(precision_ceiling_at_k(c, k) for c in cases),
            recall=_mean(recall_at_k(c, k) for c in cases),
            ndcg=_mean(ndcg_at_k(c, k) for c in cases),
            coverage=_mean(1.0 if has_result(c, SIMILARITY_FLOOR, k) else 0.0 for c in cases),
        )

    return EvaluationReport(
        model_identifier=model_identifier,
        dataset_version=DATASET_VERSION,
        dataset_fingerprint=dataset_fingerprint(),
        case_count=len(BENCHMARK),
        item_count=total_items(),
        config=current_config(),
        metrics=metrics,
        duplicate_slots_lost=sum(duplicate_crowding_at_k(c, TOP_K) for c in cases),
        per_case_ndcg_at_5={
            query: round(ndcg_at_k(ranked, 5), _ROUND_DIGITS) for query, ranked in ranked_cases
        },
    )


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return round(sum(collected) / len(collected), _ROUND_DIGITS) if collected else 0.0


def to_json(report: EvaluationReport) -> str:
    """The checked-in baseline format. Byte-stable for identical input."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "model_identifier": report.model_identifier,
        "dataset_version": report.dataset_version,
        "dataset_fingerprint": report.dataset_fingerprint,
        "case_count": report.case_count,
        "item_count": report.item_count,
        "config": {key: round(value, _ROUND_DIGITS) for key, value in report.config.items()},
        "metrics": {
            str(k): {
                "coverage": metric.coverage,
                "ndcg": metric.ndcg,
                "precision": metric.precision,
                "precision_ceiling": metric.precision_ceiling,
                "recall": metric.recall,
            }
            for k, metric in sorted(report.metrics.items())
        },
        "duplicate_crowding": {"total_slots_lost": report.duplicate_slots_lost},
        "per_case_ndcg_at_5": dict(sorted(report.per_case_ndcg_at_5.items())),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def from_json(text: str) -> EvaluationReport:
    payload = json.loads(text)
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"baseline uses schema {payload['schema_version']}, "
            f"this code expects {SCHEMA_VERSION} — regenerate it"
        )
    return EvaluationReport(
        model_identifier=payload["model_identifier"],
        dataset_version=payload["dataset_version"],
        dataset_fingerprint=payload["dataset_fingerprint"],
        case_count=payload["case_count"],
        item_count=payload["item_count"],
        config=payload["config"],
        metrics={
            int(k): MetricSet(
                precision=metric["precision"],
                precision_ceiling=metric["precision_ceiling"],
                recall=metric["recall"],
                ndcg=metric["ndcg"],
                coverage=metric["coverage"],
            )
            for k, metric in payload["metrics"].items()
        },
        duplicate_slots_lost=payload["duplicate_crowding"]["total_slots_lost"],
        per_case_ndcg_at_5=payload["per_case_ndcg_at_5"],
    )
