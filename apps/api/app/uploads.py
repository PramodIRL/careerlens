"""Shared upload validation (Prompt 2.1, generalized in 4.1b).

Extracted verbatim from app/api/v1/resume.py when the job-PDF import
needed the same checks. Shared rather than copied deliberately: this is
the code that decides whether bytes from the internet are what they
claim to be, and two drifting copies of that is exactly the bug you do
not want. `allowed` is a parameter so each caller declares its own
accepted types — a resume takes PDF or DOCX, a job import takes PDF
only — while the checks themselves stay in one place.

REJECT BEFORE PERSIST. Every check runs before anything is written to a
database or to storage, so a rejected upload leaves nothing behind.
"""

from pathlib import Path as FilePath

from fastapi import HTTPException, status

PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A client's filename and Content-Type are both trivially spoofable
# (renaming evil.exe to resume.pdf satisfies each). Only the first bytes
# say what a file actually is. PDFs start with "%PDF-"; DOCX files are
# ZIP archives, which start with "PK\x03\x04".
PDF_MAGIC = b"%PDF-"
DOCX_MAGIC = b"PK\x03\x04"

MAGIC_BYTES: dict[str, bytes] = {PDF_CONTENT_TYPE: PDF_MAGIC, DOCX_CONTENT_TYPE: DOCX_MAGIC}
RESUME_EXTENSIONS: dict[str, str] = {".pdf": PDF_CONTENT_TYPE, ".docx": DOCX_CONTENT_TYPE}
PDF_ONLY_EXTENSIONS: dict[str, str] = {".pdf": PDF_CONTENT_TYPE}

MAX_FILENAME_LENGTH = 255


def reject(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def validate_upload(
    filename: str | None,
    declared_content_type: str | None,
    content: bytes,
    max_size_bytes: int,
    *,
    allowed: dict[str, str],
    type_error: str,
) -> str:
    """Run every check and return the canonical content type on success.
    Raises HTTPException(422) with a clear reason on the first failure.

    Filename safety is checked first: it is a prerequisite for reading an
    extension off the filename at all, not because it matters most.
    """
    if filename is None or not filename.strip():
        raise reject("filename is required")
    name = filename.strip()
    if len(name) > MAX_FILENAME_LENGTH:
        raise reject(f"filename must be at most {MAX_FILENAME_LENGTH} characters")
    if "/" in name or "\\" in name or "\x00" in name:
        raise reject("filename contains invalid characters")

    extension = FilePath(name).suffix.lower()
    expected_content_type = allowed.get(extension)
    if expected_content_type is None:
        raise reject(type_error)

    if declared_content_type != expected_content_type:
        raise reject("file content type does not match its extension")

    if len(content) == 0:
        raise reject("file is empty")
    if len(content) > max_size_bytes:
        raise reject(f"file exceeds the {max_size_bytes} byte size limit")

    if not content.startswith(MAGIC_BYTES[expected_content_type]):
        raise reject("file content does not match its declared type")

    return expected_content_type
