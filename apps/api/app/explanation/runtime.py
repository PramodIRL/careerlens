"""Calling a provider safely: one timeout, one bounded retry, one log
line (Prompt 6.2, made shareable in Prompt 6.3).

WHY THIS IS ITS OWN MODULE. Prompt 6.3 needed the same reliability
behaviour for a second LLM-facing path. Copying forty lines of retry
policy is how two paths quietly stop agreeing about what "we retry
transport, never content" means — the same argument app/matching/
resolve.py makes for having one resolver behind `/match` and `/gaps`.
So the loop lives here once and both adapters call it.

IT KNOWS NOTHING ABOUT EITHER FEATURE. A provider, a request and a
subject string to log against. It does not parse, validate or ground
anything — deciding whether an answer is acceptable is the caller's
business, and keeping that out of here is what makes "a validation
failure is never retried" impossible to get wrong: this function never
sees a validation result.

BEHAVIOUR IS UNCHANGED FROM 6.2. Same per-attempt `asyncio.wait_for`,
same transient/terminal split, same fixed backoff, same refusal to log
anything but ids, reasons and exception TYPE names.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.explanation.prompt import ExplanationRequest
from app.explanation.provider import (
    ExplanationProvider,
    ExplanationTimeout,
    ExplanationUnavailable,
)
from app.explanation.validate import RejectionReason
from app.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderRun:
    """What came back, or why nothing did. Never both.

    `attempts` is reported so a caller's tests can prove a validation
    failure was not retried — a guarantee that quietly stops being true
    if nothing counts the calls.
    """

    raw: str | None = None
    reason: RejectionReason | None = None
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


async def _attempt(
    provider: ExplanationProvider,
    request: ExplanationRequest,
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
        return await asyncio.wait_for(provider.complete(request), timeout=timeout_seconds)
    except TimeoutError as error:
        raise ExplanationTimeout("provider did not answer in time") from error


async def call_provider(
    provider: ExplanationProvider,
    request: ExplanationRequest,
    *,
    settings: Settings,
    subject: str,
) -> ProviderRun:
    """At most `explanation_max_attempts` calls, each time-boxed.

    The worst case a user waits is statable rather than open:
    attempts x timeout + (attempts - 1) x backoff.

    `subject` is a safe identifier — a saved job's id — recorded so a
    failure can be traced. Nothing else about the request is logged: see
    the logging note in app/explanation/adapter.py for which fields are
    excluded and why each one is named rather than merely omitted.
    """
    max_attempts = max(1, settings.explanation_max_attempts)

    for attempt in range(1, max_attempts + 1):
        try:
            raw = await _attempt(
                provider, request, timeout_seconds=settings.explanation_timeout_seconds
            )
        except Exception as error:
            reason = _reason_for(error)
            if _retryable(error) and attempt < max_attempts:
                logger.warning(
                    "provider attempt %s failed for %s (provider=%s reason=%s error=%s); retrying",
                    attempt,
                    subject,
                    provider.name,
                    reason.value,
                    type(error).__name__,
                )
                # Fixed, not exponential: with two attempts an
                # exponential schedule is decoration.
                await asyncio.sleep(settings.explanation_retry_backoff_seconds)
                continue
            logger.warning(
                "provider unavailable for %s after %s attempt(s) (provider=%s reason=%s error=%s)",
                subject,
                attempt,
                provider.name,
                reason.value,
                type(error).__name__,
            )
            return ProviderRun(reason=reason, attempts=attempt)

        return ProviderRun(raw=raw, attempts=attempt)

    raise AssertionError("unreachable: the loop returns on every path")  # pragma: no cover
