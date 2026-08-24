"""Schemas for GitHub ingestion runs and ingested repositories (Prompt 3.2)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class IngestionStatus(StrEnum):
    """Processing state of one ingestion run.

    Exactly the four states app.schemas.resume.ResumeStatus uses, on
    purpose: a reader who understands resume extraction already
    understands this.

    THERE IS DELIBERATELY NO "partial". A run that listed successfully
    but lost three READMEs is SUCCEEDED with repositories_failed = 3 —
    partiality is data the UI reports, not a fifth state. And a run
    paused by rate limiting stays PROCESSING: it is waiting, not broken,
    and everything it already imported is still there.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GitHubIngestionRunResponse(BaseModel):
    """One ingestion run's state and progress.

    THE THREE COUNTS ARE NOT INTERCHANGEABLE, and the UI needs all of
    them to tell the truth:

        repositories_available       every public repository GitHub
                                     listed, forks included
        repositories_forks_excluded  how many of those were forks
        repositories_total           how many this run actually fetched
                                     detail for (the capped set)

    `repository_cap_reached` is DERIVED here rather than stored — it is
    simply (available - forks) > total. Without it, a UI can only say
    "20 imported", which a user reasonably reads as "I have 20
    repositories". See app/models/github_ingestion_run.py.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    status: IngestionStatus
    # Null until the listing finishes — that null is the "still working
    # out how much there is" state, not a zero.
    repositories_available: int | None
    repositories_forks_excluded: int | None
    repositories_total: int | None
    repositories_completed: int
    # Repositories whose languages/README could not be fetched. Their
    # base row is still stored, so this never means "nothing imported".
    # Rate limiting never lands here — it pauses the run instead.
    repositories_failed: int
    # A curated, safe reason — only ever set when status == "failed".
    # Never an upstream status code, body or traceback.
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def repository_cap_reached(self) -> bool:
        if self.repositories_available is None or self.repositories_total is None:
            return False
        forks = self.repositories_forks_excluded or 0
        return (self.repositories_available - forks) > self.repositories_total


class GitHubRepositoryLanguageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    language: str
    byte_count: int


class GitHubRepositoryResponse(BaseModel):
    """One ingested public repository.

    Public facts only. Note what is absent: no raw GitHub payload, no
    owner details, no clone URLs — none of it is stored, so none of it
    can be exposed. `readme_text` is deliberately NOT returned either:
    it exists for Prompt 3.3 to quote excerpts from server-side, and
    shipping a 20k-character blob per repository to a dashboard that
    only lists them would be waste. `has_readme` tells the UI what it
    actually needs to know.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    github_repo_id: int
    name: str
    full_name: str
    description: str | None
    is_fork: bool
    is_archived: bool
    primary_language: str | None
    stargazers_count: int
    forks_count: int
    pushed_at: datetime | None
    languages: list[GitHubRepositoryLanguageResponse]
    topics: list[str]
    has_readme: bool
    # True when this repository's languages and README were fetched.
    # False means base data only — a fork, or beyond this run's cap.
    detail_fetched: bool
    updated_at: datetime
