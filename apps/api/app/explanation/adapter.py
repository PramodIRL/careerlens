"""The one entry point: facts in, a validated outcome out (Prompt 6.1).

FAIL CLOSED, AND SAY SO. A rejected explanation produces an outcome
carrying the machine-readable reason and NO generated content — not a
truncated version, not the parts that passed. The route reports it as
`status: "rejected"` beside the deterministic score, which is unaffected
either way.

NO RETRY, NO REPAIR, NO SECOND OPINION. Asking again after a rejection
is a reliability concern with its own questions about cost, latency and
rate limits, and none of those are answered in this slice.

NO DATABASE. The adapter takes an `ExplanationFacts` and a provider, so
the whole chain is testable without Postgres and without a model.
"""

from dataclasses import dataclass
from typing import Literal

from app.explanation.prompt import build_request
from app.explanation.provider import ExplanationProvider
from app.explanation.schema import ExplanationFacts, MatchExplanation
from app.explanation.validate import ExplanationRejected, validate_explanation


@dataclass(frozen=True)
class ExplanationOutcome:
    """Generated-and-validated, or rejected. Never partially either.

    `explanation` is None whenever `status` is "rejected" — the type
    makes "we do not show ungrounded content" checkable rather than
    something a route has to remember.
    """

    status: Literal["generated", "rejected"]
    provider: str
    explanation: MatchExplanation | None = None
    reason: str | None = None


async def explain(
    facts: ExplanationFacts,
    *,
    provider: ExplanationProvider,
    taxonomy: frozenset[str],
) -> ExplanationOutcome:
    """Ask the provider to explain these facts, and validate the answer.

    A provider that raises is treated exactly like one that returned
    nonsense: no explanation, a reason, and the deterministic facts
    still served. The model is never on the critical path for the score.
    """
    try:
        raw = await provider.complete(build_request(facts))
    except Exception:  # pragma: no cover - no provider in 6.1 can raise
        return ExplanationOutcome(
            status="rejected", provider=provider.name, reason="provider_error"
        )

    try:
        explanation = validate_explanation(raw, facts=facts, taxonomy=taxonomy)
    except ExplanationRejected as rejected:
        # `rejected.detail` can quote model output and is deliberately
        # NOT carried here — it belongs in logs, not in a response.
        return ExplanationOutcome(
            status="rejected", provider=provider.name, reason=rejected.reason.value
        )

    return ExplanationOutcome(status="generated", provider=provider.name, explanation=explanation)
