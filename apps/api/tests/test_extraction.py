"""Tests for asynchronous resume text extraction (Prompt 2.2): the
`extract_resume_text` Celery task and its `_extract_and_persist` async
core.

Runs entirely via Celery's `task_always_eager` mode — a real PDF/DOCX
is built in-memory and written through `LocalResumeStorage` to a
pytest `tmp_path`, `app.worker`'s DB session factory and storage lookup
are monkeypatched to the isolated test schema / that tmp_path, and
`extract_resume_text.delay(...)` then runs the task synchronously,
in-process, including its retry loop (`self.retry()` re-invokes the
task function immediately, up to `max_retries`) — no real broker, no
real worker process, no real Redis connection needed. Standard Celery
testing pattern; see docs/decisions.md.

Test functions are plain sync `def`s, not `async def`/`@pytest.mark.anyio`:
`extract_resume_text` itself calls `asyncio.run(...)` internally (see
app/worker.py's module docstring on the sync/async bridge), and
`asyncio.run()` refuses to start if an event loop is already running in
the current thread — which an `anyio`-driven async test would be. Each
async DB helper below is instead run via a small `_run()` wrapper, the
same `asyncio.run(...)`-per-call pattern tests/conftest.py's own
fixtures already use for the same reason."""

import asyncio
import io
import uuid
from collections.abc import Coroutine, Generator
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import build_session_factory
from app.models.resume import Resume
from app.models.user import User
from app.schemas.resume import ResumeStatus
from app.settings import get_settings
from app.storage.base import ResumeStorage
from app.storage.local import LocalResumeStorage
from app.worker import celery_app, extract_resume_text
from tests.conftest import TEST_SCHEMA

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}

_PDF_CONTENT_TYPE = "application/pdf"
_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _build_minimal_pdf(body_text: str) -> bytes:
    """A minimal but *structurally valid* single-page PDF containing
    `body_text`, with an accurate xref table pypdf can actually parse
    — needed to test real extraction, not just a "%PDF-" magic-byte
    stub (which is all Prompt 2.1's upload-validation tests needed)."""
    objects: list[bytes | None] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream_body = f"BT /F1 12 Tf 72 720 Td ({body_text}) Tj ET".encode()
    objects[3] = b"<< /Length %d >>\nstream\n" % len(stream_body) + stream_body + b"\nendstream"

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        assert obj is not None
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF"
    ).encode()
    return bytes(buf)


def _build_minimal_docx(body_text: str) -> bytes:
    document = Document()
    document.add_paragraph(body_text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# Correct magic bytes (passes Prompt 2.1's upload validation) but no
# valid internal structure at all — real, deterministic parse failures.
_MALFORMED_PDF = b"%PDF-1.4\nthis is not a real pdf structure at all, no xref\n"
_MALFORMED_DOCX = b"PK\x03\x04" + b"not a real docx, no valid zip/xml structure"


class _FlakyStorage:
    """Wraps a real ResumeStorage, failing the first `fail_times` reads
    with OSError before delegating normally — simulates a transient
    storage-read failure without needing a genuinely flaky one."""

    def __init__(self, inner: ResumeStorage, fail_times: int) -> None:
        self._inner = inner
        self._fail_times = fail_times
        self.read_calls = 0

    async def save(self, key: str, content: bytes) -> None:
        await self._inner.save(key, content)

    async def read(self, key: str) -> bytes:
        self.read_calls += 1
        if self.read_calls <= self._fail_times:
            raise OSError("simulated transient storage failure")
        return await self._inner.read(key)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)


def _isolated_session_factory() -> async_sessionmaker:  # type: ignore[type-arg]
    return build_session_factory(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )


@pytest.fixture(autouse=True)
def _celery_eager() -> Generator[None, None, None]:
    # celery_app is a module-level singleton (app/worker.py) shared by
    # the whole test process — reset afterward so this doesn't bleed
    # into any other test file that happens to import it.
    #
    # Deliberately *not* task_eager_propagates=True: Celery's eager mode
    # treats self.retry()'s internal Retry exception the same as any
    # other task exception when that's enabled, so a legitimate retry
    # would escape to the caller instead of being handled by Celery's
    # own eager retry loop. Every test here asserts on the resulting DB
    # row instead of on what .delay() itself returns/raises, so nothing
    # is lost by leaving propagation off.
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture(autouse=True)
def _use_isolated_worker_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.worker.get_worker_session_factory", _isolated_session_factory)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalResumeStorage:
    # A fresh, auto-cleaned pytest tmp_path — never the real
    # RESUME_STORAGE_DIR — same isolation approach as tests/test_resume.py.
    local_storage = LocalResumeStorage(tmp_path)
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: local_storage)
    return local_storage


async def _create_queued_resume_async(
    storage: ResumeStorage, content: bytes, content_type: str, filename: str
) -> uuid.UUID:
    user_id = uuid.uuid4()
    resume_id = uuid.uuid4()
    extension = ".pdf" if content_type == _PDF_CONTENT_TYPE else ".docx"
    storage_key = f"{user_id}/{resume_id}{extension}"
    await storage.save(storage_key, content)

    factory = _isolated_session_factory()
    # Two separate commits, not one flush with both objects added: with
    # no ORM relationship() declared between User and Resume (neither
    # model has one — see app/models/resume.py), SQLAlchemy's
    # unit-of-work does not reliably order this flush's two INSERTs by
    # their raw FK column alone, and can emit them in an order that
    # trips the FK constraint. Real request handlers never hit this —
    # register() and resume upload always operate on an already-committed
    # user — this is purely a test-setup-convenience wrinkle.
    async with factory() as db:
        db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
        await db.commit()
    async with factory() as db:
        db.add(
            Resume(
                id=resume_id,
                user_id=user_id,
                storage_key=storage_key,
                original_filename=filename,
                content_type=content_type,
                file_size_bytes=len(content),
                status=ResumeStatus.QUEUED.value,
            )
        )
        await db.commit()
    return resume_id


def _create_queued_resume(
    storage: ResumeStorage, content: bytes, content_type: str, filename: str = "resume.pdf"
) -> uuid.UUID:
    """Inserts a User + a "queued" Resume directly into the isolated
    test schema, and writes `content` through `storage` — bypassing
    the HTTP upload endpoint entirely, since these tests are about the
    worker, not upload validation (see tests/test_resume.py for that)."""
    return _run(_create_queued_resume_async(storage, content, content_type, filename))


async def _fetch_resume_async(resume_id: uuid.UUID) -> Resume:
    async with _isolated_session_factory()() as db:
        resume = await db.get(Resume, resume_id)
        assert resume is not None
        return resume


def _fetch_resume(resume_id: uuid.UUID) -> Resume:
    return _run(_fetch_resume_async(resume_id))


async def _set_status_async(
    resume_id: uuid.UUID, resume_status: ResumeStatus, attempt_count: int
) -> None:
    async with _isolated_session_factory()() as db:
        resume = await db.get(Resume, resume_id)
        assert resume is not None
        resume.status = resume_status.value
        resume.attempt_count = attempt_count
        await db.commit()


def _set_status(resume_id: uuid.UUID, resume_status: ResumeStatus, attempt_count: int) -> None:
    _run(_set_status_async(resume_id, resume_status, attempt_count))


def test_successful_pdf_extraction_stores_text_and_marks_succeeded(
    storage: LocalResumeStorage,
) -> None:
    pdf_bytes = _build_minimal_pdf("Alice Example - Backend Engineer")
    resume_id = _create_queued_resume(storage, pdf_bytes, _PDF_CONTENT_TYPE)

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.SUCCEEDED.value
    assert resume.extracted_text is not None
    assert "Alice Example" in resume.extracted_text
    assert resume.error_message is None
    assert resume.attempt_count == 1
    assert resume.processed_at is not None


def test_successful_docx_extraction_stores_text_and_marks_succeeded(
    storage: LocalResumeStorage,
) -> None:
    docx_bytes = _build_minimal_docx("Bob Example - Data Analyst")
    resume_id = _create_queued_resume(
        storage, docx_bytes, _DOCX_CONTENT_TYPE, filename="resume.docx"
    )

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.SUCCEEDED.value
    assert resume.extracted_text is not None
    assert "Bob Example" in resume.extracted_text
    assert resume.error_message is None


def test_malformed_pdf_fails_permanently_with_a_safe_message(storage: LocalResumeStorage) -> None:
    resume_id = _create_queued_resume(storage, _MALFORMED_PDF, _PDF_CONTENT_TYPE)

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.FAILED.value
    assert resume.error_message == "the document could not be read"
    assert resume.extracted_text is None
    # Permanent failures are never retried — the same bytes fail the
    # same way every time, so wasting attempts on them only delays the
    # user-visible failure for nothing.
    assert resume.attempt_count == 1


def test_malformed_docx_fails_permanently_with_a_safe_message(storage: LocalResumeStorage) -> None:
    resume_id = _create_queued_resume(
        storage, _MALFORMED_DOCX, _DOCX_CONTENT_TYPE, filename="resume.docx"
    )

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.FAILED.value
    assert resume.error_message == "the document could not be read"


def test_scanned_pdf_with_no_extractable_text_fails_permanently(
    storage: LocalResumeStorage,
) -> None:
    """A structurally valid PDF with no text content at all — the
    deterministic ("no LLM, no OCR") equivalent of a scanned/image-only
    resume. A real, expected limitation, not a bug — see
    app/extraction.py."""
    empty_pdf = _build_minimal_pdf("")
    resume_id = _create_queued_resume(storage, empty_pdf, _PDF_CONTENT_TYPE)

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.FAILED.value
    assert resume.error_message == "the document could not be read"


def test_transient_storage_failure_retries_and_then_succeeds(
    storage: LocalResumeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = _FlakyStorage(storage, fail_times=1)  # fails once, succeeds on the retry
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: flaky)
    pdf_bytes = _build_minimal_pdf("Retry Test Resume")
    resume_id = _create_queued_resume(storage, pdf_bytes, _PDF_CONTENT_TYPE)

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.SUCCEEDED.value
    assert resume.extracted_text is not None
    assert "Retry Test Resume" in resume.extracted_text
    assert flaky.read_calls == 2
    assert resume.attempt_count == 2  # failed once, succeeded on attempt 2


def test_transient_storage_failure_exhausts_retries_and_fails_safely(
    storage: LocalResumeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = _FlakyStorage(storage, fail_times=999)  # never recovers
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: flaky)
    pdf_bytes = _build_minimal_pdf("Always Fails")
    resume_id = _create_queued_resume(storage, pdf_bytes, _PDF_CONTENT_TYPE)

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    max_attempts = get_settings().resume_extraction_max_retries + 1
    assert resume.status == ResumeStatus.FAILED.value
    assert resume.error_message == "the document could not be read after multiple attempts"
    assert resume.extracted_text is None
    assert resume.attempt_count == max_attempts
    assert flaky.read_calls == max_attempts


def test_a_resume_already_being_processed_is_not_processed_again(
    storage: LocalResumeStorage,
) -> None:
    """The atomic claim (queued -> processing) prevents duplicate
    concurrent processing — simulated here by putting the resume in
    "processing" *before* the task runs, standing in for another
    execution that's already claimed it."""
    pdf_bytes = _build_minimal_pdf("Already Processing")
    resume_id = _create_queued_resume(storage, pdf_bytes, _PDF_CONTENT_TYPE)
    _set_status(resume_id, ResumeStatus.PROCESSING, attempt_count=1)

    extract_resume_text.delay(str(resume_id))

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.PROCESSING.value  # untouched
    assert resume.extracted_text is None


def test_rerunning_extraction_on_an_already_succeeded_resume_is_idempotent(
    storage: LocalResumeStorage,
) -> None:
    pdf_bytes = _build_minimal_pdf("Idempotency Test")
    resume_id = _create_queued_resume(storage, pdf_bytes, _PDF_CONTENT_TYPE)

    extract_resume_text.delay(str(resume_id))
    first = _fetch_resume(resume_id)
    assert first.status == ResumeStatus.SUCCEEDED.value

    # Re-run the exact same public entry point again — the same claim
    # guard that blocks a genuinely concurrent duplicate (tested above)
    # also makes a later re-run on an already-terminal resume a safe
    # no-op, not a re-processing/corruption risk.
    extract_resume_text.delay(str(resume_id))
    second = _fetch_resume(resume_id)

    assert second.status == ResumeStatus.SUCCEEDED.value
    assert second.extracted_text == first.extracted_text
    assert second.attempt_count == first.attempt_count
    assert second.processed_at == first.processed_at
