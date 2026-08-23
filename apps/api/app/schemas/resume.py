from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ResumeStatus(StrEnum):
    """Processing state of an uploaded resume's text extraction.

    A resume is "queued" the instant it's uploaded, moves to
    "processing" when the Celery worker (app/worker.py) claims it, and
    reaches exactly one terminal state — "succeeded" or "failed" — once.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResumeResponse(BaseModel):
    """Resume metadata returned to a client — this doubles as the
    extraction status endpoint (no separate route; see
    app/api/v1/resume.py): `status` and `error_message` are enough for
    the dashboard's polling UI to know when a job finishes and why it
    failed, without exposing the extracted text itself.

    Deliberately excludes `storage_key` (the server-generated locator
    the storage interface uses to find the file — see
    app/models/resume.py) and `extracted_text` — there is no endpoint to
    read the extracted text back in this prompt; that's for a later
    prompt (skill extraction) to do server-side.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    original_filename: str
    content_type: str
    file_size_bytes: int
    status: ResumeStatus
    # A curated, safe reason — only ever set (non-null) when
    # status == "failed". Never the raw exception/traceback; see
    # app/worker.py.
    error_message: str | None
    created_at: datetime
    updated_at: datetime
