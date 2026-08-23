"""Celery worker: asynchronous PDF/DOCX text extraction (Prompt 2.2).

Celery is a sync-worker framework — per docs/project-brief.md's explicit
"Redis and Celery" architecture direction, not a free choice — layered
onto an otherwise fully-async codebase. The bridge: `extract_resume_text`
(the Celery task) is a thin sync entry point that does `asyncio.run(...)`
into `_extract_and_persist`, an async function reusing the same async
SQLAlchemy session-factory pattern (app/db.py) and ResumeStorage
interface (app/storage/) the rest of the app already uses — only the
outermost Celery entry point is sync; the actual logic stays async.

No Celery result backend: Postgres (the `resumes` row itself) is the
single source of truth for extraction job state — nothing ever calls
`.get()` on a Celery AsyncResult. Run the worker with:
    uv run celery -A app.worker worker --loglevel=info
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from celery import Celery
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import build_session_factory
from app.extraction import ExtractionFailed, extract_docx_text, extract_pdf_text
from app.models.resume import Resume
from app.schemas.resume import ResumeStatus
from app.settings import get_settings
from app.skill_extraction import extract_skills_for_resume
from app.storage import ResumeStorage, get_resume_storage

logger = logging.getLogger(__name__)

celery_app = Celery("careerlens", broker=get_settings().redis_url)

_EXTRACTORS = {
    "application/pdf": extract_pdf_text,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx_text,
}


def get_worker_session_factory() -> async_sessionmaker[AsyncSession]:
    """A plain function — like app.storage.local.get_resume_storage —
    so tests can monkeypatch it to point at the isolated test schema
    instead of the real database. Not a FastAPI dependency: Celery
    tasks aren't part of FastAPI's request/DI cycle."""
    return build_session_factory(get_settings().database_url)


async def _claim_resume(db: AsyncSession, resume_id: uuid.UUID, attempt: int) -> bool:
    """Atomically transitions queued -> processing, recording this as
    attempt number `attempt`. Only ever called on a task's *first* try
    (see extract_resume_text) — Celery already serializes a single
    task's own retries, so this only needs to guard against a
    genuinely separate, independently-triggered duplicate execution.
    Returns False if some other execution already claimed this resume
    — the caller treats that as a no-op, not an error."""
    # An UPDATE always yields a CursorResult at runtime (it has
    # .rowcount) — AsyncSession.execute()'s return type is just the
    # generic Result[Any] statically, since it can't know the statement
    # kind ahead of time.
    result = cast(
        "CursorResult[Any]",
        await db.execute(
            update(Resume)
            .where(Resume.id == resume_id, Resume.status == ResumeStatus.QUEUED.value)
            .values(status=ResumeStatus.PROCESSING.value, attempt_count=attempt)
        ),
    )
    await db.commit()
    return result.rowcount > 0


async def _mark_succeeded(db: AsyncSession, resume_id: uuid.UUID, text: str, attempt: int) -> None:
    await db.execute(
        update(Resume)
        .where(Resume.id == resume_id)
        .values(
            status=ResumeStatus.SUCCEEDED.value,
            extracted_text=text,
            error_message=None,
            attempt_count=attempt,
            processed_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _mark_failed(db: AsyncSession, resume_id: uuid.UUID, message: str, attempt: int) -> None:
    await db.execute(
        update(Resume)
        .where(Resume.id == resume_id)
        .values(
            status=ResumeStatus.FAILED.value,
            error_message=message,
            attempt_count=attempt,
            processed_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _extract_and_persist(
    resume_id: uuid.UUID, attempt: int, max_attempts: int, storage: ResumeStorage
) -> bool:
    """The actual extraction logic, fully async and independent of
    Celery. Idempotent: always fully overwrites extracted_text /
    error_message / status rather than appending, so re-running this
    for the same resume — a legitimate Celery retry, or any other
    re-invocation — is always safe.

    Returns True if the caller (extract_resume_text) should retry — a
    transient failure occurred and attempts remain. Returns False once
    a terminal state (succeeded, or failed — permanently, or because
    attempts are now exhausted) has already been written; there is
    nothing more for the caller to do either way.
    """
    factory = get_worker_session_factory()
    async with factory() as db:
        resume = await db.get(Resume, resume_id)
        if resume is None:
            logger.warning("extraction skipped: resume %s no longer exists", resume_id)
            return False

        if attempt == 1:
            claimed = await _claim_resume(db, resume_id, attempt)
            if not claimed:
                logger.info(
                    "extraction skipped: resume %s already claimed by another execution",
                    resume_id,
                )
                return False
        else:
            # A Celery-driven retry: Celery already serializes this
            # task's own retries (no concurrent-execution risk to guard
            # against here), so just record the new attempt number.
            await db.execute(
                update(Resume).where(Resume.id == resume_id).values(attempt_count=attempt)
            )
            await db.commit()

        extractor = _EXTRACTORS.get(resume.content_type)
        if extractor is None:
            # Unreachable via the real upload endpoint (which only ever
            # accepts these two content types) — defensive, not a real
            # runtime path.
            await _mark_failed(db, resume_id, "unsupported document type", attempt)
            return False

        # A storage *read* failure is the one failure mode worth
        # retrying — rare for today's local disk, but exactly what a
        # future S3-compatible backend (docs/project-brief.md) would
        # hit from a transient network blip.
        try:
            content = await storage.read(resume.storage_key)
        except OSError:
            logger.exception(
                "storage read failed for resume %s (attempt %s/%s)",
                resume_id,
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                await _mark_failed(
                    db, resume_id, "the document could not be read after multiple attempts", attempt
                )
                return False
            return True

        # A parsing failure is permanent: the same bytes fail the same
        # way every time, so retrying only delays the user-visible
        # failure for nothing.
        try:
            text = extractor(content)
        except ExtractionFailed:
            logger.exception("extraction failed for resume %s", resume_id)
            await _mark_failed(db, resume_id, "the document could not be read", attempt)
            return False

        await _mark_succeeded(db, resume_id, text, attempt)

        # Prompt 2.4: deterministic skill extraction over the text we
        # just stored. Deliberately best-effort and non-fatal — text
        # extraction genuinely succeeded, so a failure here must not
        # flip the resume back to "failed" or trigger a Celery retry of
        # work that is already done. The skills are simply missing until
        # the next run.
        try:
            await extract_skills_for_resume(db, resume_id)
        except Exception:
            logger.exception(
                "skill extraction failed for resume %s (text extraction still succeeded)",
                resume_id,
            )
        return False


@celery_app.task(  # type: ignore[untyped-decorator]  # celery ships no stubs — see the mypy override above
    bind=True, max_retries=get_settings().resume_extraction_max_retries
)
def extract_resume_text(self: Any, resume_id: str) -> None:
    """Celery entry point — see module docstring for the sync/async
    bridge. `resume_id` is a plain string, not uuid.UUID: Celery
    serializes task arguments (JSON by default), and UUID isn't
    natively JSON-serializable."""
    max_attempts = self.max_retries + 1
    attempt = self.request.retries + 1
    storage = get_resume_storage()
    should_retry = asyncio.run(
        _extract_and_persist(uuid.UUID(resume_id), attempt, max_attempts, storage)
    )
    if should_retry:
        raise self.retry()


def enqueue_extraction(resume_id: uuid.UUID) -> None:
    """Called once, right after a resume row is committed
    (app/api/v1/resume.py). Best-effort: if this fails (e.g. Redis is
    unreachable), the upload itself still succeeds — the file is safely
    stored either way — the resume is simply left "queued" with nothing
    to process it yet. A documented, accepted limitation for this
    prompt: no outbox/retry-the-enqueue-itself mechanism."""
    try:
        extract_resume_text.delay(str(resume_id))
    except Exception:
        logger.exception("failed to enqueue extraction for resume %s", resume_id)
