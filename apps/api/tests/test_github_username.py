"""Tests for GitHub username normalization and validation
(app/github/username.py).

Pure functions, no database, no HTTP, no fixtures — which is the point
of keeping this module separate from the client and the route. Every
accepted and rejected spelling can be pinned down here cheaply, so the
API tests don't have to enumerate them.

Why this matters beyond tidiness: an obviously malformed username must
never become an outbound request. Unauthenticated GitHub allows 60
requests per hour per IP, so a typo that reaches the network is a typo
that spends a shared budget.
"""

import pytest

from app.github.username import (
    INVALID_USERNAME_MESSAGE,
    URL_SUBMITTED_MESSAGE,
    InvalidGitHubUsername,
    normalize_username,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("octocat", "octocat"),
        # Casing is preserved here; the canonical spelling that actually
        # gets stored comes from GitHub's response, not from this.
        ("OctoCat", "OctoCat"),
        ("  octocat  ", "octocat"),
        ("@octocat", "octocat"),
        ("  @octocat  ", "octocat"),
        ("ada-sample", "ada-sample"),
        ("a", "a"),
        ("123", "123"),
        # Exactly 39 characters — GitHub's maximum.
        ("a" * 39, "a" * 39),
    ],
)
def test_accepts_and_normalizes_valid_usernames(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "@",
        "a" * 40,  # one over GitHub's limit
        "-octocat",  # leading hyphen
        "octocat-",  # trailing hyphen
        "octo--cat",  # consecutive hyphens
        "octo cat",  # space
        "octo_cat",  # underscore is not a GitHub username character
        "octo.cat",
        "octo@cat",
        "octocát",  # non-ASCII
        "octo\ncat",
    ],
)
def test_rejects_invalid_usernames(raw: str) -> None:
    with pytest.raises(InvalidGitHubUsername) as excinfo:
        normalize_username(raw)
    assert str(excinfo.value) == INVALID_USERNAME_MESSAGE


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/octocat",
        "http://github.com/octocat",
        "github.com/octocat",
        "GitHub.com/octocat",
        "octocat/hello-world",  # a repo path, not a username
    ],
)
def test_rejects_urls_with_an_actionable_message(raw: str) -> None:
    """A URL is a predictable thing to paste, so it gets its own message
    telling the user what to do instead of the generic "invalid" one.
    Deliberately rejected rather than parsed — see the module docstring
    in app/github/username.py."""
    with pytest.raises(InvalidGitHubUsername) as excinfo:
        normalize_username(raw)
    assert str(excinfo.value) == URL_SUBMITTED_MESSAGE


def test_messages_never_echo_the_submitted_text() -> None:
    """The message is rendered straight into the UI, so it must not
    reflect arbitrary input back — an easy way to turn a validation
    error into an injection surface."""
    with pytest.raises(InvalidGitHubUsername) as excinfo:
        normalize_username("<script>alert(1)</script>")
    assert "script" not in str(excinfo.value)
