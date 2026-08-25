"""Job import from a PDF (Prompt 4.1b): turn a job-description PDF into
an editable DRAFT.

    POST /api/v1/job-imports/from-pdf  -> JobDraftResponse

TWO WAYS IN, ONE DOMAIN MODEL. A job is either pasted manually or read
from a PDF, and both converge on the same editable form and the same
`POST /api/v1/saved-jobs`. There is one SavedJob, one persistence path,
and one validation contract.

THIS ENDPOINT PERSISTS NOTHING. No saved job, no file, no candidate
skill, no skill evidence — not a single row. It reads an upload,
produces a draft, and returns it. The user reviews and corrects it, and
only the saved-jobs endpoint creates a row. That makes "never silently
save unreviewed extracted data" structural rather than a promise: there
is no code path from an import to a stored row that skips the human.

THE PDF IS PARSED AND DISCARDED. It is a transport format for text the
user is about to review, not a document we owe them access to later.
Storing it would add a storage key, a cleanup lifecycle, and an
orphaned-file problem for no benefit — the reviewed text is what the
product relies on. This is the deliberate difference from the resume
pipeline, which stores because it re-processes.

NO SKILL EXTRACTION HAPPENS HERE. Reading a job description for skills is
Prompt 4.2 and scoring it is 4.3. Both are asserted by tests, because it
would be easy to add "just a little" of either and quietly move the
boundary.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.v1.auth import get_current_user
from app.extraction import ExtractionFailed as PdfExtractionFailed
from app.extraction import extract_pdf_text
from app.job_import.pdf_extract import JobDraft, extract_job_draft_from_text
from app.models.user import User
from app.schemas.saved_job import (
    MAX_DESCRIPTION_LENGTH,
    EmploymentType,
    JobDraftResponse,
)
from app.settings import get_settings
from app.uploads import PDF_ONLY_EXTENSIONS, validate_upload

router = APIRouter()

_PDF_NO_TEXT = (
    "we could not read any text from that PDF — it may be a scan. Paste the description manually."
)
_PDF_TYPE_ERROR = "only .pdf files can be imported"


def _unprocessable(message: str) -> HTTPException:
    """Import failures are 422 with a curated, safe message — never a raw
    parser exception. Same rule as `resumes.error_message`."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _to_response(draft: JobDraft) -> JobDraftResponse:
    """Clip an over-long extraction rather than failing the import.

    A posting can legitimately carry more text than a saved job accepts,
    and rejecting the whole import for that would be needlessly hostile —
    the user is about to edit this anyway, and the note says what
    happened so nothing is silently lost.
    """
    description = draft.description
    notes = list(draft.notes)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[:MAX_DESCRIPTION_LENGTH]
        notes.append("The description was long and has been shortened — please check the end.")

    employment_type = None
    if draft.employment_type:
        try:
            employment_type = EmploymentType(draft.employment_type)
        except ValueError:  # pragma: no cover - the extractor already maps to this vocabulary
            employment_type = None

    return JobDraftResponse(
        company=draft.company,
        title=draft.title,
        location=draft.location,
        employment_type=employment_type,
        source_url=draft.source_url,
        description=description,
        notes=notes,
    )


@router.post("/from-pdf", response_model=JobDraftResponse)
async def import_job_from_pdf(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> JobDraftResponse:
    """Read a job-description PDF and return an editable draft.

    Reuses the resume pipeline's validation (extension, declared type,
    and MAGIC BYTES — a renamed executable fails here) and its pure
    `extract_pdf_text`, restricted to PDF only. It deliberately does NOT
    reuse the storage layer or the Celery worker: nothing is written
    anywhere, and parsing a file this size is fast enough for a request.

    Field extraction is deliberately conservative — only values the
    document explicitly labels are filled in, everything else is left
    blank for the user. See app/job_import/pdf_extract.py.
    """
    settings = get_settings()
    content = await file.read()
    validate_upload(
        file.filename,
        file.content_type,
        content,
        settings.job_pdf_max_size_bytes,
        allowed=PDF_ONLY_EXTENSIONS,
        type_error=_PDF_TYPE_ERROR,
    )

    try:
        text = extract_pdf_text(content)
    except PdfExtractionFailed as exc:
        raise _unprocessable(_PDF_NO_TEXT) from exc

    draft = extract_job_draft_from_text(text)
    if draft is None:
        raise _unprocessable(_PDF_NO_TEXT)
    return _to_response(draft)
