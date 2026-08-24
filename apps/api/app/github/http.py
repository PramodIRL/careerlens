"""The real GitHub REST client (Prompt 3.1).

Reads exactly one public, unauthenticated endpoint:

    GET {base}/users/{username}

NO CREDENTIAL IS EVER SENT. There is no code path here that adds an
Authorization header, reads a token from settings or the environment, or
requests a private scope — and tests/test_github_client.py asserts the
outgoing request carries no Authorization header, so that stays true
rather than merely being true today.

Everything this module does beyond the one-line request is translation:
HTTP statuses, transport exceptions and unexpected payloads become the
small closed set of domain errors in app/github/base.py, so no route
ever branches on a status code from another company's API.
"""

import logging
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from app.github.base import (
    GitHubClient,
    GitHubRateLimited,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUser,
    GitHubUserNotFound,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

_ACCEPT = "application/vnd.github+json"
# Pinning the API version means a future breaking change to GitHub's
# payload shape arrives when we upgrade this string, not silently on the
# morning they ship it.
_API_VERSION = "2022-11-28"


def _reset_at(response: httpx.Response) -> datetime | None:
    """Parse GitHub's X-RateLimit-Reset (epoch seconds) into a UTC
    datetime. Returns None if the header is absent or unparseable —
    a missing reset time downgrades the message ("try again shortly")
    rather than turning a rate-limit into an unhandled error."""
    raw = response.headers.get("X-RateLimit-Reset")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _is_rate_limited(response: httpx.Response) -> bool:
    """True only for a genuine rate-limit response.

    GitHub signals an exhausted limit as 403 or 429 *with*
    X-RateLimit-Remaining: 0. The remaining-header check is what stops
    an ordinary 403 (blocked user agent, abuse detection, a forbidden
    resource) being reported to the user as "come back in an hour" —
    which would be both wrong and unactionable.
    """
    if response.status_code not in (403, 429):
        return False
    # bool(...) because httpx's Headers.get is typed as returning Any,
    # which would otherwise leak out of this bool-declared function.
    return bool(response.headers.get("X-RateLimit-Remaining") == "0")


class HttpGitHubClient:
    """GitHubClient backed by httpx.

    The transport is injectable so tests can drive every branch below
    through `httpx.MockTransport` without a network. That is why the
    timeout/rate-limit/malformed-body handling here is actually
    exercised, rather than only being stubbed out one layer up by the
    fake client the route tests use.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # A single float applies to every phase (connect, read, write,
        # pool), so a server that accepts the connection and then stalls
        # is bounded just as tightly as one that never accepts at all.
        self._timeout = httpx.Timeout(timeout_seconds)
        self._user_agent = user_agent
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        # Note what is absent: Authorization. GitHub requires a
        # User-Agent (it 403s requests without one), which is why that
        # header is mandatory rather than decorative.
        return {
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": self._user_agent,
        }

    async def get_user(self, username: str) -> GitHubUser:
        url = f"{self._base_url}/users/{username}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            # Logged without the exception's own text: it can contain the
            # full URL, and this message is one grep away from a support
            # ticket. The username is already known to the caller.
            logger.warning("github request timed out for username %s", username)
            raise GitHubTimeout("github request timed out") from exc
        except httpx.HTTPError as exc:
            logger.warning("github request failed for username %s", username)
            raise GitHubUnavailable("github request failed") from exc

        # Rate limiting is checked before the status-code ladder because
        # it arrives *as* a 403/429 — checking 404 first would be fine,
        # but checking "not 2xx -> unavailable" first would swallow it.
        if _is_rate_limited(response):
            raise GitHubRateLimited(_reset_at(response))

        if response.status_code == 404:
            raise GitHubUserNotFound(f"no public github account for {username}")

        if response.status_code != 200:
            logger.warning(
                "unexpected github status %s for username %s", response.status_code, username
            )
            raise GitHubUnavailable(f"github returned status {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("github returned a non-JSON body for username %s", username)
            raise GitHubUnavailable("github returned an unreadable response") from exc

        try:
            return GitHubUser.model_validate(payload)
        except ValidationError as exc:
            # A 200 is not a promise about the body. Missing `id`, a
            # string where `public_repos` should be a number, or a JSON
            # array instead of an object all land here.
            logger.warning("github returned an unexpected payload for username %s", username)
            raise GitHubUnavailable("github returned an unexpected response") from exc


def get_github_client() -> GitHubClient:
    """FastAPI dependency (see app/api/v1/github_connection.py).

    A plain function, like app.storage.local.get_resume_storage — tests
    replace it through `app.dependency_overrides` with a fake, so no
    test in the suite ever reaches the network.

    Deliberately NOT lru_cached, unlike get_resume_storage: this builds a
    cheap value object, and caching it would freeze the settings it was
    first constructed with — awkward for a value a test may want to vary.
    """
    settings = get_settings()
    return HttpGitHubClient(
        base_url=settings.github_api_base_url,
        timeout_seconds=settings.github_request_timeout_seconds,
        user_agent=settings.github_user_agent,
    )
