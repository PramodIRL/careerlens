from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ResumeStatus(StrEnum):
    """Processing state of an uploaded resume.

    Only UPLOADED is ever set by Prompt 2.1's code — the other three
    exist so this column's contract (app.models.resume.Resume.status)
    is stable before Prompt 2.2's extraction step needs them, without
    another migration.
    """

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ResumeResponse(BaseModel):
    """Resume metadata returned to a client.

    Deliberately excludes `storage_key` — the server-generated locator
    the storage interface uses to find the file — so a raw filesystem
    path or storage location is never exposed. There is also no field
    or endpoint for downloading the file itself in Prompt 2.1.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    original_filename: str
    content_type: str
    file_size_bytes: int
    status: ResumeStatus
    created_at: datetime
    updated_at: datetime
