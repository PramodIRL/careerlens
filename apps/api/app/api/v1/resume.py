"""Resume upload, list, fetch-metadata, and delete endpoints (Prompt 2.1),
plus triggering asynchronous text extraction on upload (Prompt 2.2).

Ownership enforcement: unlike `/profiles/{user_id}` (1:1 with a user, so
addressable by the user's own id), a resume is addressed by its own
`id` — one user can have several. Upload/list are implicitly scoped to
`current_user.id` (a client can never name a different owner: there is
no user id in the upload request or the list query, only the JWT).
Fetch-metadata and delete load the row by `resume_id` first, then check
`resume.user_id == current_user.id` before returning anything — not
found is 404, found-but-not-yours is 403. See docs/decisions.md.

There is no separate extraction "status" route: `GET /{resume_id}` and
`GET /` already return `status`/`error_message` (app/schemas/resume.py),
which is all the dashboard's polling UI needs — see app/worker.py for
the actual extraction logic. No text extraction *content*, skill
extraction, or LLM use happens in this module — extracted text is
never returned by any endpoint here (see docs/project-brief.md and the
Prompt 2.1/2.2 plans for what's explicitly deferred to later prompts).
"""

import uuid
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.resume import Resume
from app.models.user import User
from app.schemas.resume import ResumeResponse, ResumeStatus
from app.settings import get_settings
from app.skill_extraction import remove_resume_skill_evidence
from app.storage import ResumeStorage, get_resume_storage
from app.worker import enqueue_extraction

router = APIRouter()

_PDF_CONTENT_TYPE = "application/pdf"
_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Real file-signature ("magic number") bytes — checked against the
# actual uploaded content, not just the filename or the client-declared
# Content-Type header, both of which are trivially spoofable (e.g.
# renaming evil.exe to resume.pdf). PDFs start with "%PDF-"; DOCX files
# are ZIP containers (OOXML) and start with the ZIP local-file-header
# signature.
_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"

_ALLOWED_EXTENSIONS: dict[str, str] = {".pdf": _PDF_CONTENT_TYPE, ".docx": _DOCX_CONTENT_TYPE}
_MAGIC_BYTES: dict[str, bytes] = {_PDF_CONTENT_TYPE: _PDF_MAGIC, _DOCX_CONTENT_TYPE: _DOCX_MAGIC}

_MAX_FILENAME_LENGTH = 255
_NOT_YOUR_RESUME = "not authorized to access this resume"
_NOT_FOUND = "resume not found"


def _reject(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _validate_upload(
    filename: str | None, declared_content_type: str | None, content: bytes, max_size_bytes: int
) -> str:
    """Runs every Prompt 2.1 validation check and returns the canonical
    (validated) content type on success. Raises HTTPException(422) with
    a clear reason on the first failing check — nothing is persisted or
    written to storage before this returns successfully.

    Filename safety is checked first: it's a prerequisite for safely
    reading an extension off the filename at all, not because it's more
    important than the other checks."""
    if filename is None or not filename.strip():
        raise _reject("filename is required")
    name = filename.strip()
    if len(name) > _MAX_FILENAME_LENGTH:
        raise _reject(f"filename must be at most {_MAX_FILENAME_LENGTH} characters")
    if "/" in name or "\\" in name or "\x00" in name:
        raise _reject("filename contains invalid characters")

    extension = FilePath(name).suffix.lower()
    expected_content_type = _ALLOWED_EXTENSIONS.get(extension)
    if expected_content_type is None:
        raise _reject("only .pdf and .docx files are supported")

    if declared_content_type != expected_content_type:
        raise _reject("file content type does not match its extension")

    if len(content) == 0:
        raise _reject("file is empty")
    if len(content) > max_size_bytes:
        raise _reject(f"file exceeds the {max_size_bytes} byte size limit")

    if not content.startswith(_MAGIC_BYTES[expected_content_type]):
        raise _reject("file content does not match its declared type")

    return expected_content_type


async def _get_owned_resume(db: AsyncSession, resume_id: uuid.UUID, current_user: User) -> Resume:
    resume = await db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if resume.user_id != current_user.id:
        # Not a credential-guessing surface (resume ids are random
        # UUIDs, not enumerable) — a specific 403 is fine, the same
        # reasoning as the profile endpoints' ownership check.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_YOUR_RESUME)
    return resume


def _to_response(resume: Resume) -> ResumeResponse:
    return ResumeResponse(
        id=resume.id,
        user_id=resume.user_id,
        original_filename=resume.original_filename,
        content_type=resume.content_type,
        file_size_bytes=resume.file_size_bytes,
        status=ResumeStatus(resume.status),
        error_message=resume.error_message,
        created_at=resume.created_at,
        updated_at=resume.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ResumeResponse)
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ResumeStorage = Depends(get_resume_storage),
) -> ResumeResponse:
    settings = get_settings()
    content = await file.read()
    content_type = _validate_upload(
        file.filename, file.content_type, content, settings.resume_max_size_bytes
    )
    assert file.filename is not None  # guaranteed by _validate_upload above
    filename = file.filename.strip()

    resume_id = uuid.uuid4()
    extension = FilePath(filename).suffix.lower()
    # Server-generated and opaque — never derived from the user-supplied
    # filename — so this is the one thing that decides where the file
    # actually lands on disk. See app/storage/base.py.
    storage_key = f"{current_user.id}/{resume_id}{extension}"

    # Write the file before creating the database row: if the write
    # fails, nothing is persisted at all; if the row's insert/commit
    # fails after a successful write, the result is an orphaned file on
    # disk with no matching row (harmless, cleanable later) rather than
    # a row that claims to have a file that was never actually written.
    await storage.save(storage_key, content)

    resume = Resume(
        id=resume_id,
        user_id=current_user.id,
        storage_key=storage_key,
        original_filename=filename,
        content_type=content_type,
        file_size_bytes=len(content),
        status=ResumeStatus.QUEUED.value,
    )
    db.add(resume)
    await db.commit()
    await db.refresh(resume)

    # Best-effort (see docs/decisions.md): the upload has already fully
    # succeeded above — a failure here just leaves this resume "queued"
    # with nothing to process it yet, rather than failing the upload.
    enqueue_extraction(resume.id)

    return _to_response(resume)


@router.get("", response_model=list[ResumeResponse])
async def list_resumes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ResumeResponse]:
    rows = await db.scalars(
        select(Resume).where(Resume.user_id == current_user.id).order_by(Resume.created_at.desc())
    )
    return [_to_response(resume) for resume in rows.all()]


@router.get("/{resume_id}", response_model=ResumeResponse)
async def get_resume(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResumeResponse:
    resume = await _get_owned_resume(db, resume_id, current_user)
    return _to_response(resume)


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resume(
    resume_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ResumeStorage = Depends(get_resume_storage),
) -> None:
    resume = await _get_owned_resume(db, resume_id, current_user)
    storage_key = resume.storage_key

    # Skills derived from this resume go with it. `skill_evidence`
    # cites a resume by a polymorphic string, not a foreign key (no
    # single FK can span resume/github/manual), so no cascade fires and
    # this has to be explicit — without it the unified profile (Prompt
    # 3.4) kept citing a document the user had just deleted.
    #
    # Deliberately narrow: only THIS resume's evidence, and then only
    # candidate skills left unreviewed with no evidence from any source.
    # GitHub evidence, manual evidence, another resume's evidence, and
    # every confirmed or rejected decision are untouched — see
    # app/skill_extraction.py.
    #
    # Same transaction as the row delete below, sharing the one commit:
    # a resume that vanished while its evidence survived is exactly the
    # inconsistency this is fixing, so the two must not be able to
    # diverge.
    await remove_resume_skill_evidence(db, current_user.id, resume_id)

    # Delete the database row before the stored file, for the same
    # reason upload writes the file before the row: whichever side
    # effect might still fail happens last, so the only possible
    # inconsistency is "orphaned file, no row" — never "row claims a
    # file that isn't there". storage.delete() is also idempotent, so a
    # file that's somehow already gone isn't an error here either.
    await db.delete(resume)
    await db.commit()
    await storage.delete(storage_key)
