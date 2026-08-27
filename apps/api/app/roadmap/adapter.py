"""Facts in, a validated narrative out (Prompt 6.3).

FAIL CLOSED, AND STILL USEFUL. A rejected narrative costs the user the
WORDING and none of the substance: the deterministic plan — which
skills, in what order, why, for which jobs — was computed before the
provider was called and is returned either way. That is the whole reason
the LLM sits at the end of this pipeline rather than inside it.

SHARES 6.2's RELIABILITY, DOES NOT REIMPLEMENT IT. The timeout, the
bounded retry and the transient/terminal split live in
app/explanation/runtime.py and are called from here unchanged. Two
copies of a retry policy is how two paths quietly stop agreeing about
what "we retry transport, never content" means.

VALIDATION FAILURES ARE NOT RETRIED, structurally: `call_provider`
never sees a validation result, so no code path exists that could ask
again after one.
"""

import logging
import time
from dataclasses import dataclass
from typing import Literal

from app.explanation.provider import ExplanationProvider
from app.explanation.runtime import call_provider
from app.explanation.validate import ExplanationRejected
from app.roadmap.prompt import build_request
from app.roadmap.schema import RoadmapFacts, RoadmapNarrative
from app.roadmap.validate import validate_narrative
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NarrativeOutcome:
    """Written-and-validated, or rejected. Never partially either."""

    status: Literal["generated", "rejected"]
    provider: str
    narrative: RoadmapNarrative | None = None
    reason: str | None = None
    attempts: int = 0


async def narrate(
    facts: RoadmapFacts,
    *,
    provider: ExplanationProvider,
    taxonomy: frozenset[str],
    settings: Settings | None = None,
) -> NarrativeOutcome:
    """Ask the provider to phrase this plan, and validate the answer."""
    settings = settings or get_settings()
    started = time.monotonic()
    subject = f"roadmap({len(facts.items)} items)"

    run = await call_provider(provider, build_request(facts), settings=settings, subject=subject)
    if run.raw is None:
        assert run.reason is not None
        return NarrativeOutcome(
            status="rejected",
            provider=provider.name,
            reason=run.reason.value,
            attempts=run.attempts,
        )

    try:
        narrative = validate_narrative(run.raw, facts=facts, taxonomy=taxonomy)
    except ExplanationRejected as rejected:
        # `rejected.detail` can quote model output and is deliberately
        # carried into neither the response nor the log — see the
        # logging note in app/explanation/adapter.py.
        logger.info(
            "roadmap narrative rejected (provider=%s reason=%s items=%s elapsed_ms=%s)",
            provider.name,
            rejected.reason.value,
            len(facts.items),
            int((time.monotonic() - started) * 1000),
        )
        return NarrativeOutcome(
            status="rejected",
            provider=provider.name,
            reason=rejected.reason.value,
            attempts=run.attempts,
        )

    logger.info(
        "roadmap narrative generated (provider=%s attempts=%s items=%s elapsed_ms=%s)",
        provider.name,
        run.attempts,
        len(facts.items),
        int((time.monotonic() - started) * 1000),
    )
    return NarrativeOutcome(
        status="generated",
        provider=provider.name,
        narrative=narrative,
        attempts=run.attempts,
    )
