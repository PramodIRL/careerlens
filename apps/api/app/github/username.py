"""Normalization and validation of a public GitHub username (Prompt 3.1).

Pure: text in, text out, no database and no I/O — so every accepted and
rejected spelling can be pinned down exhaustively in
tests/test_github_username.py without fixtures, the same split
app/skill_matching.py uses.

This decides only whether a string *could* be a GitHub username. Whether
the account actually exists is a question only GitHub can answer, and
that happens one layer up (app/github/http.py). Both checks are needed:
validating first means an obviously malformed string never becomes an
outbound HTTP request, which keeps typos from spending the caller's
unauthenticated rate-limit budget (60/hour per IP).
"""

import re

# GitHub's own rule: 1-39 characters, alphanumeric or single hyphens,
# cannot begin or end with a hyphen, no two hyphens in a row. The
# lookahead is what enforces "single" — a hyphen is only allowed when
# the next character is alphanumeric, which rules out both "a--b" and a
# trailing "a-" in one clause.
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

MAX_USERNAME_LENGTH = 39

INVALID_USERNAME_MESSAGE = (
    "enter a valid GitHub username — letters, numbers and single hyphens, "
    f"up to {MAX_USERNAME_LENGTH} characters"
)
URL_SUBMITTED_MESSAGE = "enter just the username, not the full GitHub URL"


class InvalidGitHubUsername(ValueError):
    """The submitted text cannot be a GitHub username.

    Carries a message written for the person who typed it — the API
    layer passes it straight through to the client, so it must never
    contain internals.
    """


def normalize_username(raw: str) -> str:
    """Return the cleaned username, or raise InvalidGitHubUsername.

    Normalization is deliberately tiny: trim surrounding whitespace and
    drop one optional leading "@" (people type handles that way). URLs
    are *rejected with their own message* rather than parsed — accepting
    them would mean host checks, path-segment counting and trailing-slash
    handling, which is real surface to get wrong for a small nicety, and
    a specific error tells the user exactly what to do instead.

    Casing is preserved here but is NOT what gets stored: GitHub lookups
    are case-insensitive, and app/api/v1/github_connection.py persists
    the canonical `login` GitHub returns, so typing "ADA-SAMPLE" stores
    whatever casing the real account uses.
    """
    candidate = raw.strip()
    if not candidate:
        raise InvalidGitHubUsername(INVALID_USERNAME_MESSAGE)

    if candidate.startswith("@"):
        candidate = candidate[1:]

    # Checked before the pattern purely so the user gets the actionable
    # message instead of the generic one — "/" and ":" would fail the
    # pattern anyway.
    if "/" in candidate or ":" in candidate or candidate.lower().startswith("github.com"):
        raise InvalidGitHubUsername(URL_SUBMITTED_MESSAGE)

    if not _USERNAME_PATTERN.match(candidate):
        raise InvalidGitHubUsername(INVALID_USERNAME_MESSAGE)

    return candidate
