"""Deterministic (non-LLM) plain-text extraction from PDF and DOCX
bytes — see docs/project-brief.md's Evidence-First AI Rule: raw text
extraction must never involve an LLM.

Both functions are pure: bytes in, text out, raising ExtractionFailed
on any failure. Kept separate from app/worker.py's Celery/retry
orchestration so they're directly testable without any worker/Redis
machinery.
"""

import io

from docx import Document
from pypdf import PdfReader

# A document that parses cleanly but yields (near-)no text is treated
# the same as a parse failure — e.g. a scanned/image-only PDF with no
# embedded text layer. This is a real, deterministic limit of "no LLM,
# no OCR" extraction (see docs/project-brief.md), not a bug.
_MIN_MEANINGFUL_CHARS = 10


class ExtractionFailed(Exception):
    """Raised for any extraction failure: the caller (app/worker.py)
    decides what to do about it — this exception carries no retry
    judgment itself, just that the given bytes didn't yield usable text."""


def extract_pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ExtractionFailed("could not parse PDF") from exc
    if len(text.strip()) < _MIN_MEANINGFUL_CHARS:
        raise ExtractionFailed("no readable text found in PDF")
    return text


def extract_docx_text(content: bytes) -> str:
    try:
        document = Document(io.BytesIO(content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception as exc:
        raise ExtractionFailed("could not parse DOCX") from exc
    if len(text.strip()) < _MIN_MEANINGFUL_CHARS:
        raise ExtractionFailed("no readable text found in DOCX")
    return text
