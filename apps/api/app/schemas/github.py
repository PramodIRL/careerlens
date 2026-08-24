"""Request/response schemas for the public GitHub connection (Prompt 3.1)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.github.username import InvalidGitHubUsername, normalize_username


class GitHubConnectRequest(BaseModel):
    """PUT body — a public GitHub username, and nothing else.

    `extra="forbid"` is load-bearing rather than pedantic. This product
    never asks for a GitHub password or token, and a rejected request is
    a much clearer statement of that than silently ignoring a `password`
    field would be: a client that sends one gets a 422 telling it the
    field is not permitted, and nothing is ever stored. There is a test
    for exactly that.
    """

    model_config = ConfigDict(extra="forbid")

    username: str

    @field_validator("username")
    @classmethod
    def _normalize(cls, value: str) -> str:
        """Trim, drop a leading '@', reject anything that cannot be a
        GitHub username — see app/github/username.py for the rules.

        Re-raised as ValueError so Pydantic turns it into FastAPI's
        standard 422 body; the message is written for the person who
        typed it and is safe to show verbatim.
        """
        try:
            return normalize_username(value)
        except InvalidGitHubUsername as exc:
            raise ValueError(str(exc)) from exc


class GitHubConnectionResponse(BaseModel):
    """The connected account, as shown on the dashboard.

    Contains only public, non-sensitive facts: which account, how many
    public repositories it had when we last looked, and when that was.
    No token, no email, no private data — there is none to expose,
    because none is stored (see app/models/github_connection.py).
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    username: str
    github_user_id: int
    # A snapshot taken at `last_verified_at`, not a live count. The UI
    # shows the two together so the number is never presented as current
    # truth.
    public_repo_count: int
    last_verified_at: datetime
    created_at: datetime
    updated_at: datetime
