"""The one entry point: facts in, a validated outcome out (Prompt 6.1),
now with a timeout, a bounded retry and the single place this feature
logs (Prompt 6.2).

FAIL CLOSED, AND SAY SO. A rejected explanation produces an outcome
carrying the machine-readable reason and NO generated content — not a
truncated version, not the parts that passed. The route reports it as
`status: "rejected"` beside the deterministic score, which is unaffected
either way.

RETRY IS FOR TRANSPORT, NEVER FOR CONTENT. A timeout or an unreachable
provider is retried once; a response that ARRIVED and failed validation
is not, ever. The distinction matters twice over: re-asking after a
grounding failure spends money to re-roll a dice the user does not need
rolled — they already have the deterministic score — and it would turn
one hallucination into a loop that eventually gets lucky and shows one.

NOTHING HERE CAN DUPLICATE EXPENSIVE WORK. The route is read-only, this
module writes no row and persists nothing, so a second attempt repeats
exactly one thing: the provider call. `explanation_max_attempts` is what
bounds that, and it is why the cap is 2 rather than "a few".

THE LOGGING CHOKE POINT. This is the ONLY module in the feature that
logs, so "no excerpt, no model output, no job title reaches a log line"
is a property of one file rather than a habit spread over six. See
`_log_*` below for exactly what is and is not recorded.

NO DATABASE. The adapter takes an `ExplanationFacts` and a provider, so
the whole chain is testable without Postgres and without a model.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from app.explanation.prompt import build_request
from app.explanation.provider import (
    ExplanationProvider,
    ExplanationTimeout,
    ExplanationUnavailable,
)
from app.explanation.schema import ExplanationFacts, MatchExplanation
from app.explanation.validate import (
    ExplanationRejected,
    RejectionReason,
    validate_explanation,
)
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExplanationOutcome:
    """Generated-and-validated, or rejected. Never partially either.

    `explanation` is None whenever `status` is "rejected" — the type
    makes "we do not show ungrounded content" checkable rather than
    something a route has to remember.

    `attempts` is how many provider calls were made. Reported so a test
    can prove a validation failure was NOT retried, which is the kind of
    guarantee that quietly stops being true if nothing watches it.
    """

    status: Literal["generated", "rejected"]
    provider: str
    explanation: MatchExplanation | None = None
    reason: str | None = None
    attempts: int = 0


def _retryable(error: Exception) -> bool:
    """Timeout and unavailable, and nothing else.

    An unexpected exception type is terminal: it is a bug or a
    misconfiguration, and retrying a bug just does it twice.
    """
    return isinstance(error, ExplanationTimeout | ExplanationUnavailable)


def _reason_for(error: Exception) -> RejectionReason:
    if isinstance(error, ExplanationTimeout):
        return RejectionReason.PROVIDER_TIMEOUT
    if isinstance(error, ExplanationUnavailable):
        return RejectionReason.PROVIDER_UNAVAILABLE
    return RejectionReason.PROVIDER_ERROR


async def _call(
    provider: ExplanationProvider,
    facts: ExplanationFacts,
    *,
    timeout_seconds: float,
) -> str:
    """One provider call, under a hard budget.

    The timeout lives HERE rather than inside a provider so that every
    provider has one, including a future implementation whose client
    library forgot to set a deadline. A hung request in the API's own
    request path is a stalled page for the user and a held connection
    for the server; neither is acceptable because a model is slow.
    """
    try:
        return await asyncio.wait_for(
            provider.complete(build_request(facts)), timeout=timeout_seconds
        )
    except TimeoutError as error:
        raise ExplanationTimeout("provider did not answer in time") from error


async def explain(
    facts: ExplanationFacts,
    *,
    provider: ExplanationProvider,
    taxonomy: frozenset[str],
    settings: Settings | None = None,
) -> ExplanationOutcome:
    """Ask the provider to explain these facts, and validate the answer.

    At most `explanation_max_attempts` provider calls, each bounded by
    `explanation_timeout_seconds`, separated by a fixed backoff. The
    worst case a user waits is therefore statable rather than open:
    attempts x timeout + (attempts - 1) x backoff.

    A provider that fails every attempt is treated exactly like one that
    returned nonsense: no explanation, a reason, and the deterministic
    facts still served. The model is never on the critical path for the
    score.
    """
    settings = settings or get_settings()
    max_attempts = max(1, settings.explanation_max_attempts)
    started = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        try:
            raw = await _call(provider, facts, timeout_seconds=settings.explanation_timeout_seconds)
        except Exception as error:
            reason = _reason_for(error)
            if _retryable(error) and attempt < max_attempts:
                _log_retry(facts, provider, attempt, reason, error)
                # Fixed, not exponential: with two attempts an
                # exponential schedule is decoration.
                await asyncio.sleep(settings.explanation_retry_backoff_seconds)
                continue
            _log_failure(facts, provider, attempt, reason, error, started)
            return ExplanationOutcome(
                status="rejected", provider=provider.name, reason=reason.value, attempts=attempt
            )

        try:
            explanation = validate_explanation(raw, facts=facts, taxonomy=taxonomy)
        except ExplanationRejected as rejected:
            # NOT RETRIED, and not on a later line either — the `return`
            # is the whole policy. `rejected.detail` can quote model
            # output and is deliberately carried into neither the
            # response nor the log.
            _log_rejection(facts, provider, attempt, rejected.reason, started)
            return ExplanationOutcome(
                status="rejected",
                provider=provider.name,
                reason=rejected.reason.value,
                attempts=attempt,
            )

        _log_success(facts, provider, attempt, started)
        return ExplanationOutcome(
            status="generated",
            provider=provider.name,
            explanation=explanation,
            attempts=attempt,
        )

    raise AssertionError("unreachable: the loop returns on every path")  # pragma: no cover


# --------------------------------------------------------------------
# Logging (Prompt 6.2)
#
# WHAT IS RECORDED: the saved job's id, the provider's name, the attempt
# number, the outcome, a machine-readable reason, and elapsed
# milliseconds.
#
# WHAT IS NEVER RECORDED, and the reason each one is named rather than
# merely omitted:
#
#   * an excerpt, or any part of one — it is a candidate's resume text
#     or somebody's README, and a log is the one place personal content
#     gets copied to a system nobody thought about
#   * the raw provider response, or `ExplanationRejected.detail` — both
#     quote model output, and "we never show ungrounded content" would
#     be false the moment it is pasted from a log into a ticket
#   * the job's title or company — it says where a person is applying
#   * the request itself (`data_json`, `system`, `delimited`)
#   * a credential, of which this feature has none and will not acquire
#     one here
#
# `logger.warning(..., type(error).__name__)` rather than
# `logger.exception`: a real client library's traceback can carry the
# request body, and a stack trace is exactly where an excerpt would
# reappear unnoticed. The exception TYPE is the diagnostic; its
# arguments are not.
# --------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log_retry(
    facts: ExplanationFacts,
    provider: ExplanationProvider,
    attempt: int,
    reason: RejectionReason,
    error: Exception,
) -> None:
    logger.warning(
        "explanation attempt %s failed for job %s (provider=%s reason=%s error=%s); retrying",
        attempt,
        facts.job.saved_job_id,
        provider.name,
        reason.value,
        type(error).__name__,
    )


def _log_failure(
    facts: ExplanationFacts,
    provider: ExplanationProvider,
    attempt: int,
    reason: RejectionReason,
    error: Exception,
    started: float,
) -> None:
    logger.warning(
        "explanation unavailable for job %s after %s attempt(s) "
        "(provider=%s reason=%s error=%s elapsed_ms=%s)",
        facts.job.saved_job_id,
        attempt,
        provider.name,
        reason.value,
        type(error).__name__,
        _elapsed_ms(started),
    )


def _log_rejection(
    facts: ExplanationFacts,
    provider: ExplanationProvider,
    attempt: int,
    reason: RejectionReason,
    started: float,
) -> None:
    logger.info(
        "explanation rejected for job %s (provider=%s reason=%s evidence=%s elapsed_ms=%s)",
        facts.job.saved_job_id,
        provider.name,
        reason.value,
        len(facts.evidence),
        _elapsed_ms(started),
    )


def _log_success(
    facts: ExplanationFacts,
    provider: ExplanationProvider,
    attempt: int,
    started: float,
) -> None:
    logger.info(
        "explanation generated for job %s (provider=%s attempts=%s evidence=%s elapsed_ms=%s)",
        facts.job.saved_job_id,
        provider.name,
        attempt,
        len(facts.evidence),
        _elapsed_ms(started),
    )
