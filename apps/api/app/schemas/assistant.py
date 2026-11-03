"""Request and response models for the AI assistant and resume tailor
(app/api/v1/ai.py).

`extra="forbid"` on every request model, as elsewhere: a client that
sends a `user_id`, a `system` message or anything else unexpected gets a
422 rather than having it silently dropped.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounds on what a browser can push through to a billed model in one
# request. Generous for a real conversation, small enough that a runaway
# client cannot send a novel.
MAX_CHAT_MESSAGES = 40
MAX_CHAT_MESSAGE_CHARS = 8000


class ChatMessage(BaseModel):
    """One turn of the visible conversation. Only `user` and
    `assistant`: the system prompt and tool turns are the server's, and
    a client cannot inject either."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_CHAT_MESSAGES)


class ChatResponse(BaseModel):
    # Markdown. Rendered by the web client without raw HTML.
    reply: str
    # Distinct tool names, in the order they were first called.
    tools_used: list[str]
    model: str


class TailorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    saved_job_id: UUID
    # Omitted means the newest resume whose text extraction succeeded.
    resume_id: UUID | None = None


class TailoredBullet(BaseModel):
    """One resume bullet, as written and as rewritten for this job."""

    model_config = ConfigDict(extra="forbid")

    original: str
    rewritten: str
    why: str

    @field_validator("original", "rewritten")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class TailorDraft(BaseModel):
    """What the model must return. Also sent to it as the JSON schema.

    Every field is required and has no constraint keywords, so the
    generated schema is accepted by providers' strict structured-output
    modes; the non-blank rules live in validators instead.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str
    bullets: list[TailoredBullet]
    keywords: list[str]
    missing_skills: list[str]
    ats_note: str

    @field_validator("summary", "ats_note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class TailorResponse(TailorDraft):
    saved_job_id: UUID
    job_title: str
    company: str
    resume_id: UUID
    resume_filename: str
    # skill_match_v1, echoed from /match. The model never computes it.
    match_score: int
    model: str


class KeyCheckResponse(BaseModel):
    """What the Settings panel's "Test key" shows. Never the key."""

    model: str
    # False when OpenRouter does not know the model id (a typo in the
    # free-text field); `model_supports_tools` is then None.
    model_found: bool
    model_supports_tools: bool | None
    usage: float | None = None
    limit: float | None = None
    limit_remaining: float | None = None
    is_free_tier: bool | None = None
