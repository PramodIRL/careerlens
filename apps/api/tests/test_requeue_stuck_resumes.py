"""Tests for the one-off backfill script that re-enqueues resumes stuck
in "queued" (scripts/requeue_stuck_resumes.py).

The gap under test: Prompt 2.2's migration renames legacy "uploaded"
rows to "queued" with pure SQL, so no Celery message is ever created for
them and the worker never picks them up. These tests prove the script
closes that gap, that rerunning it is harmless, and that it never
touches a resume that is already claimed or finished.

Runs under Celery's `task_always_eager` mode exactly like
tests/test_extraction.py — `enqueue_extraction` then executes the whole
extraction inline, so each test can assert on the resulting database row
rather than on broker state. Test functions are plain sync `def`s for
the same reason that file documents: the task calls `asyncio.run(...)`
internally, which refuses to start inside an already-running event loop.

Document builders, the flaky-storage double, and the malformed fixtures
are imported from tests/test_extraction.py rather than duplicated —
building a structurally valid PDF by hand is ~30 lines, and two copies
would inevitably drift.
"""

import asyncio
import uuid
from collections.abc import Coroutine, Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import build_session_factory
from app.models.resume import Resume
from app.models.user import User
from app.schemas.resume import ResumeStatus
from app.settings import get_settings
from app.storage.local import LocalResumeStorage
from app.worker import celery_app, enqueue_extraction
from scripts.requeue_stuck_resumes import requeue_stuck_resumes
from tests.conftest import TEST_SCHEMA
from tests.test_extraction import (
    _DOCX_CONTENT_TYPE,
    _MALFORMED_PDF,
    _PDF_CONTENT_TYPE,
    _build_minimal_docx,
    _build_minimal_pdf,
    _FlakyStorage,
)

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _isolated_session_factory() -> async_sessionmaker:  # type: ignore[type-arg]
    return build_session_factory(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )


@pytest.fixture(autouse=True)
def _celery_eager() -> Generator[None, None, None]:
    # Same singleton reset as tests/test_extraction.py — celery_app is
    # shared process-wide, so leaving eager mode on would bleed into any
    # other test file that imports it.
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture(autouse=True)
def _use_isolated_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both sides need redirecting to the isolated test schema: the script
    # builds its own session factory to run the query, and the worker
    # builds one to persist the extraction result.
    monkeypatch.setattr(
        "scripts.requeue_stuck_resumes.build_session_factory",
        lambda _url: _isolated_session_factory(),
    )
    monkeypatch.setattr("app.worker.get_worker_session_factory", _isolated_session_factory)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalResumeStorage:
    local_storage = LocalResumeStorage(tmp_path)
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: local_storage)
    return local_storage


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Records every resume id the script enqueues, then delegates to the
    real enqueue_extraction — so a test can assert both on what was
    enqueued and on the extraction outcome it produced."""
    calls: list[uuid.UUID] = []

    def _spy(resume_id: uuid.UUID) -> None:
        calls.append(resume_id)
        enqueue_extraction(resume_id)

    monkeypatch.setattr("scripts.requeue_stuck_resumes.enqueue_extraction", _spy)
    return calls


async def _create_resume_async(
    storage: LocalResumeStorage,
    content: bytes,
    content_type: str,
    filename: str,
    resume_status: ResumeStatus,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    resume_id = uuid.uuid4()
    extension = ".pdf" if content_type == _PDF_CONTENT_TYPE else ".docx"
    storage_key = f"{user_id}/{resume_id}{extension}"
    await storage.save(storage_key, content)

    factory = _isolated_session_factory()
    # Two commits, not one flush — same FK-ordering wrinkle documented in
    # tests/test_extraction.py (no ORM relationship between the models).
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
                status=resume_status.value,
            )
        )
        await db.commit()
    return resume_id


def _create_resume(
    storage: LocalResumeStorage,
    content: bytes,
    *,
    content_type: str = _PDF_CONTENT_TYPE,
    filename: str = "resume.pdf",
    resume_status: ResumeStatus = ResumeStatus.QUEUED,
) -> uuid.UUID:
    """Inserts a User + a Resume in the given status directly into the
    isolated schema. A "queued" row with no Celery message behind it is
    exactly the state Prompt 2.2's migration leaves a legacy Prompt 2.1
    resume in — the gap this script exists to close."""
    return _run(_create_resume_async(storage, content, content_type, filename, resume_status))


async def _fetch_resume_async(resume_id: uuid.UUID) -> Resume:
    async with _isolated_session_factory()() as db:
        resume = await db.get(Resume, resume_id)
        assert resume is not None
        return resume


def _fetch_resume(resume_id: uuid.UUID) -> Resume:
    return _run(_fetch_resume_async(resume_id))


def test_requeues_a_migrated_queued_resume(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    pdf_bytes = _build_minimal_pdf("Migrated Legacy Resume")
    resume_id = _create_resume(storage, pdf_bytes)

    count = requeue_stuck_resumes()

    assert count == 1
    assert enqueued == [resume_id]
    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.SUCCEEDED.value
    assert resume.extracted_text is not None
    assert "Migrated Legacy Resume" in resume.extracted_text
    assert resume.error_message is None
    assert resume.attempt_count == 1


def test_requeues_a_queued_docx_resume(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    docx_bytes = _build_minimal_docx("Legacy Docx Resume")
    resume_id = _create_resume(
        storage, docx_bytes, content_type=_DOCX_CONTENT_TYPE, filename="resume.docx"
    )

    assert requeue_stuck_resumes() == 1

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.SUCCEEDED.value
    assert resume.extracted_text is not None
    assert "Legacy Docx Resume" in resume.extracted_text


def test_running_the_script_twice_does_not_double_process(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    """The rerun-safety guarantee: the second run finds nothing still
    "queued" (the first run's extraction already moved it to a terminal
    state), so it is a no-op — and even if it had re-enqueued, the
    worker's atomic claim would make that harmless."""
    pdf_bytes = _build_minimal_pdf("Idempotent Backfill")
    resume_id = _create_resume(storage, pdf_bytes)

    assert requeue_stuck_resumes() == 1
    first = _fetch_resume(resume_id)
    assert first.status == ResumeStatus.SUCCEEDED.value

    second_count = requeue_stuck_resumes()

    assert second_count == 0
    assert enqueued == [resume_id]  # not enqueued a second time
    second = _fetch_resume(resume_id)
    assert second.status == ResumeStatus.SUCCEEDED.value
    assert second.attempt_count == first.attempt_count == 1
    assert second.extracted_text == first.extracted_text
    assert second.processed_at == first.processed_at


def test_a_redundant_enqueue_of_an_in_flight_resume_is_harmless(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    """The case a rerun cannot reach on its own, but two operators (or a
    rerun racing a still-unconsumed message from the first run) could:
    the same still-"queued" resume enqueued twice. The worker's atomic
    claim is what makes this safe — the second execution finds the row no
    longer "queued" and no-ops without writing anything."""
    pdf_bytes = _build_minimal_pdf("Double Enqueued")
    resume_id = _create_resume(storage, pdf_bytes)

    requeue_stuck_resumes()
    first = _fetch_resume(resume_id)

    # Re-enqueue the exact same resume directly, standing in for a second
    # message that was already in flight when the first one was claimed.
    enqueue_extraction(resume_id)
    second = _fetch_resume(resume_id)

    assert second.status == ResumeStatus.SUCCEEDED.value
    assert second.attempt_count == first.attempt_count == 1
    assert second.extracted_text == first.extracted_text
    assert second.processed_at == first.processed_at


def test_does_not_touch_a_processing_resume(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    pdf_bytes = _build_minimal_pdf("Already Processing")
    resume_id = _create_resume(storage, pdf_bytes, resume_status=ResumeStatus.PROCESSING)

    count = requeue_stuck_resumes()

    assert count == 0
    assert enqueued == []
    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.PROCESSING.value
    assert resume.extracted_text is None


@pytest.mark.parametrize("terminal_status", [ResumeStatus.SUCCEEDED, ResumeStatus.FAILED])
def test_does_not_enqueue_succeeded_or_failed_resumes(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID], terminal_status: ResumeStatus
) -> None:
    pdf_bytes = _build_minimal_pdf("Already Finished")
    resume_id = _create_resume(storage, pdf_bytes, resume_status=terminal_status)

    count = requeue_stuck_resumes()

    assert count == 0
    assert enqueued == []
    resume = _fetch_resume(resume_id)
    assert resume.status == terminal_status.value
    assert resume.extracted_text is None  # untouched — never re-extracted


def test_enqueues_only_the_queued_resume_out_of_a_mixed_set(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    queued_id = _create_resume(storage, _build_minimal_pdf("Queued One"))
    for status in (ResumeStatus.PROCESSING, ResumeStatus.SUCCEEDED, ResumeStatus.FAILED):
        _create_resume(storage, _build_minimal_pdf("Not Queued"), resume_status=status)

    count = requeue_stuck_resumes()

    assert count == 1
    assert enqueued == [queued_id]


def test_returns_zero_when_nothing_is_queued(enqueued: list[uuid.UUID]) -> None:
    count = requeue_stuck_resumes()

    assert count == 0
    assert enqueued == []


def test_a_requeued_malformed_document_still_fails_safely(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID]
) -> None:
    """Going through the script must not bypass or alter the existing
    permanent-failure handling — same expectations as
    tests/test_extraction.py's direct-invocation equivalent."""
    resume_id = _create_resume(storage, _MALFORMED_PDF)

    assert requeue_stuck_resumes() == 1

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.FAILED.value
    assert resume.error_message == "the document could not be read"
    assert resume.extracted_text is None
    assert resume.attempt_count == 1  # permanent failures are never retried


def test_a_requeued_resume_still_follows_transient_retry_behavior(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = _FlakyStorage(storage, fail_times=1)
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: flaky)
    resume_id = _create_resume(storage, _build_minimal_pdf("Retried Via Backfill"))

    assert requeue_stuck_resumes() == 1

    resume = _fetch_resume(resume_id)
    assert resume.status == ResumeStatus.SUCCEEDED.value
    assert resume.extracted_text is not None
    assert "Retried Via Backfill" in resume.extracted_text
    assert flaky.read_calls == 2
    assert resume.attempt_count == 2  # failed once, succeeded on attempt 2


def test_a_requeued_resume_still_fails_safely_when_retries_are_exhausted(
    storage: LocalResumeStorage, enqueued: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = _FlakyStorage(storage, fail_times=999)
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: flaky)
    resume_id = _create_resume(storage, _build_minimal_pdf("Never Readable"))

    assert requeue_stuck_resumes() == 1

    resume = _fetch_resume(resume_id)
    max_attempts = get_settings().resume_extraction_max_retries + 1
    assert resume.status == ResumeStatus.FAILED.value
    assert resume.error_message == "the document could not be read after multiple attempts"
    assert resume.attempt_count == max_attempts
    assert flaky.read_calls == max_attempts
