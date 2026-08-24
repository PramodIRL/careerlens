"""The GitHub client interface, its domain errors, and the one upstream
shape this prompt reads (Prompt 3.1).

Same three-part structure as app/storage/: a Protocol the domain code
talks to, one real implementation (app/github/http.py), and a plain
factory function tests can override. Route code never imports httpx, and
never sees an HTTP status code — it sees the errors below, which say
what happened in the product's own terms.

THIS FILE DEFINES THE WHOLE GITHUB SURFACE OF PROMPTS 3.1 AND 3.2:
four methods, all reading public, unauthenticated endpoints. No token is
ever sent, no private data is ever requested, and there is no method
here that could reach a private repository, a commit history, or an
organization's internals.
"""

from dataclasses import dataclass
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


class GitHubRepository(BaseModel):
    """One public repository, as GitHub's listing endpoint describes it.

    Deliberately narrow. Prompt 3.3 matches languages, topics, README
    text and `description` against the existing taxonomy, so those are
    what this carries — along with the flags and counts Prompt 3.2 is
    asked to record. Everything else GitHub returns (clone URLs, default
    branch, watcher and subscriber counts, license text, the owner
    object with its avatar and profile URLs) is dropped right here at
    the parse boundary rather than "not persisted later", which is a
    much harder guarantee to lose by accident.

    `topics` arrives in this same payload under the standard
    `application/vnd.github+json` Accept header, so reading them costs
    no extra request — which matters a great deal against a 60/hour
    budget.
    """

    model_config = ConfigDict(extra="ignore")

    # GitHub's immutable numeric repository id. Renames and transfers
    # change `full_name`; this never changes. It is the ONLY safe
    # identity for idempotent upserts across reruns.
    id: int
    name: str
    # "owner/repo" — the display value, and exactly what
    # app/models/skill_evidence.py reserves for source_type='github'.
    full_name: str
    # Should always be False on an unauthenticated listing. Kept so
    # app/github/ingestion.py can fail closed and skip it if GitHub ever
    # returns something unexpected here.
    private: bool = False
    fork: bool = False
    archived: bool = False
    description: str | None = None
    # GitHub's single "primary language" guess. The full byte breakdown
    # comes from a separate endpoint.
    language: str | None = None
    stargazers_count: int = 0
    forks_count: int = 0
    topics: list[str] = []
    pushed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GitHubReadme(BaseModel):
    """A repository's README, already decoded from GitHub's base64.

    THE ONLY RAW SOURCE SNAPSHOT THIS PRODUCT RETAINS. Prompt 3.3 has to
    quote a verbatim excerpt as evidence, and a hash cannot be quoted —
    so the text itself is kept (truncated on storage), alongside
    GitHub's blob `sha` and the original `size_bytes`. Those two make
    the snapshot traceable and let a rerun skip re-storing unchanged
    content. No other GitHub response body is retained in any form; see
    docs/decisions.md.
    """

    text: str
    # GitHub's blob SHA for the README. Traceability, and the rerun
    # short-circuit.
    sha: str
    # Size of the ORIGINAL content in bytes, before any truncation —
    # so a truncated record still says how much there really was.
    size_bytes: int


@dataclass(frozen=True)
class RepositoryListing:
    """The result of walking every page of a user's public repositories.

    `complete` is the load-bearing field, not a diagnostic. Deletion
    reconciliation (app/github/ingestion.py) may only run when the
    listing is complete: if pagination stopped early, the repositories
    we did not see are UNKNOWN, not absent, and marking them deleted
    would let a single timeout erase a user's entire history. Making
    completeness an explicit field rather than something inferred from
    the list length is what keeps that decision impossible to get wrong
    by accident.
    """

    repositories: tuple[GitHubRepository, ...]
    complete: bool


class GitHubError(Exception):
    """Base for every failure the client reports.

    Deliberately a small closed set: each one maps to exactly one HTTP
    status and one user-visible message in
    app/api/v1/github_connection.py. Adding a case means deciding what
    the user should be told, which is the point.
    """


class GitHubUserNotFound(GitHubError):
    """GitHub returned 404 — no public account with that username."""


class GitHubRepositoryNotFound(GitHubError):
    """A repository returned 404 on a detail endpoint after having been
    successfully listed.

    A separate class from GitHubUserNotFound because it means something
    genuinely different and is handled differently: the account is fine,
    but this one repository vanished (deleted, made private, or
    transferred) between the listing call and the detail call. It is a
    per-repository failure, and it deliberately does NOT soft-delete the
    row — deletion is single-sourced from a COMPLETE listing diff, and a
    race-condition 404 is not that. See app/github/ingestion.py.

    Note that this is specific to detail endpoints: `/repos/{o}/{r}/languages`
    returns `200 {}` for a real repository with no detected code, never
    404, so a 404 there really does mean "gone". A missing README is a
    different thing again and is not an error at all — get_readme
    returns None.
    """


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

    Every method takes already-validated input and either returns a
    fully-formed result or raises one of the errors above — none of them
    returns a partially-populated object, so callers have no "did this
    work?" branch to forget. No method here can reach anything that is
    not public.

    None of these ever raises a transport-level exception; translating
    those into the domain errors above is precisely this interface's job.
    """

    async def get_user(self, username: str) -> GitHubUser:
        """Fetch one public GitHub user.

        Raises GitHubUserNotFound, GitHubTimeout, GitHubRateLimited or
        GitHubUnavailable.
        """
        ...

    async def list_repositories(self, username: str) -> RepositoryListing:
        """Every public repository OWNED by `username`, most recently
        pushed first, walking all pages.

        Returns a RepositoryListing whose `complete` flag says whether
        pagination actually reached the end — see that class for why
        callers must not treat a short list as authoritative.

        Raises GitHubUserNotFound, GitHubTimeout, GitHubRateLimited or
        GitHubUnavailable.
        """
        ...

    async def get_languages(self, full_name: str) -> dict[str, int]:
        """Byte counts per language for one repository, e.g.
        `{"Python": 12345, "HTML": 234}`.

        An empty dict is a valid answer (a repository with no detected
        code) and is NOT an error. Raises GitHubRepositoryNotFound if
        the repository has vanished since it was listed, plus the usual
        timeout/rate-limit/unavailable errors.
        """
        ...

    async def get_readme(self, full_name: str) -> GitHubReadme | None:
        """The repository's README, decoded, or None when it has none.

        A missing README is the normal case for many repositories and is
        deliberately NOT an error — GitHub signals it with a 404 and has
        no other way to say it, so this method absorbs that one 404 and
        returns None. Every other failure still raises.
        """
        ...
