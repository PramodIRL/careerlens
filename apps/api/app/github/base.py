"""The GitHub client interface, its domain errors, and the one upstream
shape this prompt reads (Prompt 3.1).

Same three-part structure as app/storage/: a Protocol the domain code
talks to, one real implementation (app/github/http.py), and a plain
factory function tests can override. Route code never imports httpx, and
never sees an HTTP status code — it sees the errors below, which say
what happened in the product's own terms.

THIS FILE DEFINES THE WHOLE GITHUB SURFACE OF PROMPT 3.1: one method,
reading one public, unauthenticated endpoint. No token is ever sent, no
private data is ever requested, and there is no method here that could
list repositories — that arrives in Prompt 3.2 as an addition to this
Protocol.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class GitHubUser(BaseModel):
    """The subset of GitHub's public user payload this product uses.

    Validated rather than trusted: a 200 response is not a promise about
    the body's shape, and every field here is load-bearing (see
    app/models/github_connection.py). `extra="ignore"` because GitHub's
    payload is large and mostly irrelevant — we deliberately read four
    fields and drop the rest, including the email, bio, avatar and
    follower counts this product has no reason to store.
    """

    model_config = ConfigDict(extra="ignore")

    # GitHub's immutable numeric id. The username can be renamed and
    # later recycled by a different person, so this — not `login` — is
    # what Prompt 3.2's ingestion must key on.
    id: int
    # Canonical casing, as GitHub spells it.
    login: str
    # "User" or "Organization". Checked by the API layer: an
    # organization is public and has repositories, but it is not a
    # candidate's personal account.
    type: str
    public_repos: int


class GitHubError(Exception):
    """Base for every failure the client reports.

    Deliberately a small closed set: each one maps to exactly one HTTP
    status and one user-visible message in
    app/api/v1/github_connection.py. Adding a case means deciding what
    the user should be told, which is the point.
    """


class GitHubUserNotFound(GitHubError):
    """GitHub returned 404 — no public account with that username."""


class GitHubTimeout(GitHubError):
    """GitHub did not answer within the configured budget."""


class GitHubRateLimited(GitHubError):
    """GitHub's unauthenticated rate limit (60/hour per IP) is exhausted.

    `reset_at` is GitHub's own X-RateLimit-Reset, parsed to a UTC
    datetime when present — it lets the user be told when to come back
    instead of "try later". None when GitHub sent no usable reset header.
    """

    def __init__(self, reset_at: datetime | None = None) -> None:
        super().__init__("github rate limit exceeded")
        self.reset_at = reset_at


class GitHubUnavailable(GitHubError):
    """GitHub is unreachable, returned a server error, or sent something
    that is not a usable user payload.

    The malformed-response case lives here rather than in its own class
    on purpose: from the user's point of view "GitHub sent nonsense" and
    "GitHub returned a 502" are the same event — the service cannot be
    relied on right now — and there is nothing different they could do
    about either.
    """


class GitHubClient(Protocol):
    """Reads public GitHub data.

    One method today. It takes an already-validated username (see
    app/github/username.py) and either returns the public user or raises
    one of the errors above — it never returns None and never returns a
    partially-populated object, so callers have no "did this work?"
    branch to forget.
    """

    async def get_user(self, username: str) -> GitHubUser:
        """Fetch one public GitHub user.

        Raises GitHubUserNotFound, GitHubTimeout, GitHubRateLimited or
        GitHubUnavailable. Never raises a transport-level exception —
        translating those is precisely this interface's job.
        """
        ...
