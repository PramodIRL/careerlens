"""When a retrieval change should be rejected (Prompt 5.4).

SIMPLE DELTAS, NOT STATISTICS. Twelve synthetic cases cannot support a
significance test, and a policy nobody can reproduce in their head is a
policy people learn to override. Every rule here is "did this number
drop by more than X", with X argued from the measured 5.3 results.

WHAT CI CAN AND CANNOT ENFORCE. The checks that need no model — dataset
fingerprint, model identifier, `semantic_fit_v1` constants — run
everywhere. The metric comparison needs the real model, so it runs
locally. THAT GAP IS REAL AND MUST NOT BE PAPERED OVER: a change to
app/embeddings/retrieval.py alters ranking without touching any
fingerprint, so CI will pass while saying nothing about whether
retrieval still works. Such a change requires a local
`make evaluate-check` before merge, and `describe()` says so out loud
rather than leaving a green tick to imply otherwise.
"""

from dataclasses import dataclass

from app.evaluation.report import REPORTED_K, EvaluationReport

# A drop larger than this fails. Grounded in the 5.3 run: one case out
# of twelve degrading moves a mean by roughly 0.017-0.03, so 0.02 is
# about "one case got worse". For scale, the gap between the measured
# system and a random ranker is 0.18 at NDCG@5 and 0.30 at NDCG@3 — this
# tolerance sits far inside the signal it needs to protect.
METRIC_TOLERANCE = 0.02

# Duplicate crowding is a quality signal, not a correctness one: it says
# near-identical evidence is using up display slots, which is worth
# knowing and not worth blocking a merge over. 3 slots of 60 today.
CROWDING_WARN_DELTA = 2

_METRIC_NAMES = ("precision", "recall", "ndcg")

# How many cases a regression report names. Enough to start debugging,
# short enough to read.
_WORST_CASES_SHOWN = 3


@dataclass(frozen=True)
class Finding:
    """One metric that moved the wrong way."""

    kind: str  # "regression" | "warning"
    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return round(self.current - self.baseline, 4)


@dataclass(frozen=True)
class ComparisonResult:
    """The verdict, and everything needed to act on it."""

    rebaseline_required: bool
    rebaseline_reason: str | None
    findings: tuple[Finding, ...]
    worst_cases: tuple[tuple[str, float], ...]

    @property
    def regressions(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.kind == "regression")

    @property
    def ok(self) -> bool:
        """Warnings do not fail. A re-baseline requirement does — not
        because anything regressed, but because the comparison could not
        be made and a silent pass would imply it had been."""
        return not self.rebaseline_required and not self.regressions


def _mismatch(baseline: EvaluationReport, current: EvaluationReport) -> str | None:
    """Whether these two reports are even comparable.

    A different benchmark, model or threshold set means the numbers
    measure different things. Reporting a delta across that would invent
    a regression (or hide one), so the answer is "re-baseline", not a
    number.
    """
    if baseline.dataset_fingerprint != current.dataset_fingerprint:
        return (
            f"benchmark content changed (dataset_version "
            f"{baseline.dataset_version} -> {current.dataset_version})"
        )
    if baseline.model_identifier != current.model_identifier:
        return f"model changed ({baseline.model_identifier} -> {current.model_identifier})"
    if baseline.config != current.config:
        return f"semantic_fit_v1 config changed ({baseline.config} -> {current.config})"
    return None


def compare(baseline: EvaluationReport, current: EvaluationReport) -> ComparisonResult:
    """Apply the policy. Improvements never fail."""
    reason = _mismatch(baseline, current)
    if reason is not None:
        return ComparisonResult(
            rebaseline_required=True, rebaseline_reason=reason, findings=(), worst_cases=()
        )

    findings: list[Finding] = []
    for k in REPORTED_K:
        old, new = baseline.metrics[k], current.metrics[k]
        for name in _METRIC_NAMES:
            before, after = getattr(old, name), getattr(new, name)
            if before - after > METRIC_TOLERANCE:
                findings.append(Finding("regression", f"{name}@{k}", before, after))
        # Coverage has no tolerance: a case that stopped returning
        # anything is a user seeing "no related evidence" where they
        # previously saw something, which always deserves a look.
        if new.coverage < old.coverage:
            findings.append(Finding("regression", f"coverage@{k}", old.coverage, new.coverage))

    if current.duplicate_slots_lost - baseline.duplicate_slots_lost > CROWDING_WARN_DELTA:
        findings.append(
            Finding(
                "warning",
                "duplicate_slots_lost",
                float(baseline.duplicate_slots_lost),
                float(current.duplicate_slots_lost),
            )
        )

    return ComparisonResult(
        rebaseline_required=False,
        rebaseline_reason=None,
        findings=tuple(findings),
        worst_cases=_worst_cases(baseline, current) if findings else (),
    )


def _worst_cases(
    baseline: EvaluationReport, current: EvaluationReport
) -> tuple[tuple[str, float], ...]:
    """The individual cases whose NDCG@5 fell furthest — where to look
    first when a mean moved and it is not obvious why."""
    deltas = [
        (query, round(current.per_case_ndcg_at_5.get(query, 0.0) - before, 4))
        for query, before in baseline.per_case_ndcg_at_5.items()
    ]
    worsened = sorted((d for d in deltas if d[1] < 0), key=lambda pair: pair[1])
    return tuple(worsened[:_WORST_CASES_SHOWN])


def describe(result: ComparisonResult) -> str:
    """Human-readable verdict, for a terminal or a CI log."""
    if result.rebaseline_required:
        return (
            f"RE-BASELINE REQUIRED — {result.rebaseline_reason}.\n"
            "  This is not a regression: the baseline measures a different test.\n"
            "  Re-run `make evaluate-baseline` and commit the result IN THE SAME PR\n"
            "  as the change that caused it, so a reviewer sees both together."
        )

    lines: list[str] = []
    for finding in result.findings:
        label = "REGRESSION" if finding.kind == "regression" else "WARNING   "
        lines.append(
            f"{label}  {finding.metric:<18} baseline {finding.baseline:.4f}  "
            f"current {finding.current:.4f}  delta {finding.delta:+.4f}"
            + (f"  (tolerance {METRIC_TOLERANCE})" if finding.kind == "regression" else "")
        )
    if result.worst_cases:
        lines.append("  worst-affected cases (NDCG@5):")
        lines.extend(f"    {delta:+.4f}  {query!r}" for query, delta in result.worst_cases)
    if not lines:
        return "OK — no metric regressed against the baseline."
    if result.ok:
        lines.append("OK — warnings only, nothing blocking.")
    return "\n".join(lines)
