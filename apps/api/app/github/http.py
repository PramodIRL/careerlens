"""The real GitHub REST client (Prompt 3.1).

Reads only public, unauthenticated endpoints:

    GET {base}/users/{username}                  (Prompt 3.1)
    GET {base}/users/{username}/repos            (Prompt 3.2, paginated)
    GET {base}/repos/{owner}/{repo}/languages    (Prompt 3.2)
    GET {base}/repos/{owner}/{repo}/readme       (Prompt 3.2)

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

import base64
import binascii
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from app.github.base import (
    GitHubClient,
    GitHubRateLimited,
    GitHubReadme,
    GitHubRepository,
    GitHubRepositoryNotFound,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUser,
    GitHubUserNotFound,
    RepositoryListing,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

_ACCEPT = "application/vnd.github+json"
# Pinning the API version means a future breaking change to GitHub's
# payload shape arrives when we upgrade this string, not silently on the
# morning they ship it.
_API_VERSION = "2022-11-28"

# GitHub's maximum page size. Fewer round trips per page is the single
# cheapest thing we can do against a 60-request/hour budget.
_PER_PAGE = 100

# RFC 8288 Link header: <url>; rel="next". Pagination follows this
# rather than incrementing a page counter — a counter silently truncates
# if a repository is added or removed mid-walk, whereas the server's own
# next-link stays correct.
_NEXT_LINK = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="next"')


def _next_page_url(response: httpx.Response) -> str | None:
    match = _NEXT_LINK.search(response.headers.get("Link", ""))
    return match.group("url") if match else None


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

    async def _request(self, client: httpx.AsyncClient, url: str, context: str) -> httpx.Response:
        """One GET, with every transport-level failure translated.

        `context` is only ever a description of what was being fetched
        (a username or "owner/repo") — never a URL, and never the
        exception's own text, which can carry the full request URL into
        a log line that ends up in a support ticket.
        """
        try:
            return await client.get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            logger.warning("github request timed out for %s", context)
            raise GitHubTimeout("github request timed out") from exc
        except httpx.HTTPError as exc:
            logger.warning("github request failed for %s", context)
            raise GitHubUnavailable("github request failed") from exc

    def _check_common(self, response: httpx.Response, context: str) -> None:
        """Raise for everything except 200 and 404.

        Rate limiting is checked FIRST because it arrives as a 403/429 —
        a "not 2xx -> unavailable" check placed before it would swallow
        the one failure that has a specific remedy. 404 is deliberately
        left to each caller: it means "no such user", "repository gone"
        and "no README" in three different places, and only the caller
        knows which.
        """
        if _is_rate_limited(response):
            raise GitHubRateLimited(_reset_at(response))
        if response.status_code not in (200, 404):
            logger.warning("unexpected github status %s for %s", response.status_code, context)
            raise GitHubUnavailable(f"github returned status {response.status_code}")

    def _payload(self, response: httpx.Response, context: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            logger.warning("github returned a non-JSON body for %s", context)
            raise GitHubUnavailable("github returned an unreadable response") from exc

    async def get_user(self, username: str) -> GitHubUser:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await self._request(client, f"{self._base_url}/users/{username}", username)

        self._check_common(response, username)
        if response.status_code == 404:
            raise GitHubUserNotFound(f"no public github account for {username}")

        try:
            return GitHubUser.model_validate(self._payload(response, username))
        except ValidationError as exc:
            # A 200 is not a promise about the body. Missing `id`, a
            # string where `public_repos` should be a number, or a JSON
            # array instead of an object all land here.
            logger.warning("github returned an unexpected payload for %s", username)
            raise GitHubUnavailable("github returned an unexpected response") from exc

    async def list_repositories(self, username: str) -> RepositoryListing:
        """Walk every page of `username`'s public, owned repositories.

        `type=owner` excludes repositories the user merely has access to;
        `sort=pushed` puts the most recently active work first, which is
        what the caller's repository cap should keep.

        Stops early — and reports `complete=False` — only if the page
        safety valve is hit. Every other failure raises, because a
        partial listing must never be mistaken for a short one.
        """
        settings = get_settings()
        url: str | None = (
            f"{self._base_url}/users/{username}/repos"
            f"?per_page={_PER_PAGE}&type=owner&sort=pushed&direction=desc"
        )
        repositories: list[GitHubRepository] = []
        pages = 0
        complete = True

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            while url is not None:
                if pages >= settings.github_max_repository_pages:
                    # Not an error — but the caller must know the list is
                    # not authoritative, or it will "reconcile" thousands
                    # of unseen repositories into deletion.
                    logger.warning(
                        "github repository listing for %s hit the page cap (%s pages)",
                        username,
                        pages,
                    )
                    complete = False
                    break

                response = await self._request(client, url, username)
                self._check_common(response, username)
                if response.status_code == 404:
                    raise GitHubUserNotFound(f"no public github account for {username}")

                payload = self._payload(response, username)
                if not isinstance(payload, list):
                    logger.warning("github returned a non-list repository page for %s", username)
                    raise GitHubUnavailable("github returned an unexpected response")

                for item in payload:
                    try:
                        repositories.append(GitHubRepository.model_validate(item))
                    except ValidationError as exc:
                        logger.warning(
                            "github returned an unexpected repository payload for %s", username
                        )
                        raise GitHubUnavailable("github returned an unexpected response") from exc

                pages += 1
                url = _next_page_url(response)

        return RepositoryListing(repositories=tuple(repositories), complete=complete)

    async def get_languages(self, full_name: str) -> dict[str, int]:
        """Byte counts per language. `{}` is a valid answer."""
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await self._request(
                client, f"{self._base_url}/repos/{full_name}/languages", full_name
            )

        self._check_common(response, full_name)
        if response.status_code == 404:
            # This endpoint returns `200 {}` for a real repository with
            # no detected code, so a 404 genuinely means the repository
            # is no longer reachable.
            raise GitHubRepositoryNotFound(f"repository {full_name} is no longer available")

        payload = self._payload(response, full_name)
        if not isinstance(payload, dict) or not all(
            isinstance(k, str) and isinstance(v, int) for k, v in payload.items()
        ):
            logger.warning("github returned an unexpected languages payload for %s", full_name)
            raise GitHubUnavailable("github returned an unexpected response")
        return payload

    async def get_readme(self, full_name: str) -> GitHubReadme | None:
        """The decoded README, or None when the repository has none.

        The one place a 404 is absorbed rather than raised: GitHub has
        no other way to say "this repository has no README", and that is
        an ordinary state, not a failure.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await self._request(
                client, f"{self._base_url}/repos/{full_name}/readme", full_name
            )

        self._check_common(response, full_name)
        if response.status_code == 404:
            return None

        payload = self._payload(response, full_name)
        if not isinstance(payload, dict):
            logger.warning("github returned an unexpected readme payload for %s", full_name)
            raise GitHubUnavailable("github returned an unexpected response")

        encoding = payload.get("encoding")
        content = payload.get("content")
        sha = payload.get("sha")
        size = payload.get("size")
        if encoding != "base64" or not isinstance(content, str) or not isinstance(sha, str):
            logger.warning("github returned an unusable readme for %s", full_name)
            raise GitHubUnavailable("github returned an unexpected response")

        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as exc:
            logger.warning("github returned an undecodable readme for %s", full_name)
            raise GitHubUnavailable("github returned an unexpected response") from exc

        return GitHubReadme(
            text=text,
            sha=sha,
            size_bytes=size if isinstance(size, int) else len(text.encode("utf-8")),
        )


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
