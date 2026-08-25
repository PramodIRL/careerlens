"""Tests for job import from a PDF (Prompt 4.1b).

The PDF path reuses the resume pipeline's proven validation and its pure
`extract_pdf_text`, but deliberately NOT its storage or worker: the file
is parsed in-request and discarded. These tests pin both halves of that —
what it reuses, and what it must never do (persist anything, or extract
skills).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.resume import Resume
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.settings import get_settings

# The same builder the demo documents and extraction tests use, so a
# job PDF here is rendered by exactly one implementation.
from scripts.sample_resumes import build_pdf_bytes
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/job-imports/from-pdf"
_PDF_TYPE = "application/pdf"

_JOB_TEXT = (
    "Junior Backend Engineer at Fictional Widgets Ltd. "
    "Build and test internal web services. Write unit tests."
)


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


def _run[T](coro_factory: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def _inner() -> T:
        engine = create_async_engine(
            get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await coro_factory(session)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def _token(client: TestClient) -> str:
    email = f"{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(
    client: TestClient,
    token: str,
    *,
    filename: str = "job.pdf",
    content: bytes | None = None,
    content_type: str = _PDF_TYPE,
) -> Response:
    body = build_pdf_bytes(_JOB_TEXT) if content is None else content
    return client.post(
        _BASE, headers=_headers(token), files={"file": (filename, body, content_type)}
    )


def test_import_requires_authentication(client: TestClient) -> None:
    response = client.post(
        _BASE, files={"file": ("job.pdf", build_pdf_bytes(_JOB_TEXT), _PDF_TYPE)}
    )
    assert response.status_code == 401


def test_a_valid_pdf_returns_an_editable_draft(client: TestClient) -> None:
    token = _token(client)

    response = _upload(client, token)

    assert response.status_code == 200
    body = response.json()
    assert "Backend Engineer" in body["description"]
    assert body["source_url"] is None


def test_explicitly_labelled_fields_are_read(client: TestClient) -> None:
    """A posting that STATES its fields has them filled in — that is what
    "determine when confidently possible" means here."""
    token = _token(client)
    labelled = (
        "Company: Fictional Widgets Ltd\n"
        "Job Title: Junior Backend Engineer\n"
        "Location: Springfield, Fictionia\n"
        "Employment Type: Full-Time\n"
        "\n"
        "Build and test internal web services with Python and Django."
    )

    body = _upload(client, token, content=build_pdf_bytes(labelled)).json()

    assert body["company"] == "Fictional Widgets Ltd"
    assert body["title"] == "Junior Backend Engineer"
    assert body["location"] == "Springfield, Fictionia"
    assert body["employment_type"] == "full_time"
    assert any("please check them" in note for note in body["notes"])


def test_unlabelled_prose_leaves_every_field_blank(client: TestClient) -> None:
    """THE INVARIANT THIS EXTRACTOR EXISTS TO UPHOLD. A wrong guess is
    worse than a blank box: a blank asks to be filled, while a
    plausible-looking wrong value invites being accepted at a glance —
    and Prompt 4.2 will read the saved description for skills."""
    token = _token(client)
    prose = (
        "Backend Engineer Wanted\n\n"
        "We are a great company: truly the best one around.\n"
        "We build and test internal web services with Python every day."
    )

    body = _upload(client, token, content=build_pdf_bytes(prose)).json()

    assert body["company"] is None
    assert body["title"] is None
    assert body["location"] is None
    assert body["employment_type"] is None
    # ...and the note names exactly what is missing.
    assert any("Please add the company, job title, location" in n for n in body["notes"])


def test_an_unrecognised_employment_type_is_left_unset(client: TestClient) -> None:
    """Mapped onto the closed vocabulary or left None — never invented."""
    token = _token(client)
    text = (
        "Employment Type: Zero Hours Casual\n\n"
        "Build and test internal web services with Python and Django."
    )

    body = _upload(client, token, content=build_pdf_bytes(text)).json()

    assert body["employment_type"] is None


def test_a_non_pdf_extension_is_rejected(client: TestClient) -> None:
    token = _token(client)

    response = _upload(
        client,
        token,
        filename="job.docx",
        content=b"PK\x03\x04rest",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )

    assert response.status_code == 422
    assert "only .pdf" in response.json()["detail"]


def test_a_renamed_file_fails_the_magic_byte_check(client: TestClient) -> None:
    """A client's filename and Content-Type are both spoofable; only the
    first bytes say what a file really is. Reused from the resume
    pipeline rather than reimplemented."""
    token = _token(client)

    response = _upload(client, token, content=b"MZ\x90\x00 this is an executable")

    assert response.status_code == 422
    assert "does not match its declared type" in response.json()["detail"]


def test_a_mismatched_content_type_is_rejected(client: TestClient) -> None:
    token = _token(client)

    response = _upload(client, token, content_type="text/plain")

    assert response.status_code == 422


def test_an_empty_file_is_rejected(client: TestClient) -> None:
    token = _token(client)

    assert _upload(client, token, content=b"").status_code == 422


def test_an_oversized_pdf_is_rejected(client: TestClient) -> None:
    token = _token(client)
    limit = get_settings().job_pdf_max_size_bytes
    oversized = b"%PDF-" + b"x" * (limit + 1)

    response = _upload(client, token, content=oversized)

    assert response.status_code == 422
    assert "size limit" in response.json()["detail"]


def test_an_unparseable_pdf_fails_cleanly(client: TestClient) -> None:
    """Passes the magic-byte check but is not a real PDF. The user is
    told to paste manually rather than shown a stack trace."""
    token = _token(client)

    response = _upload(client, token, content=b"%PDF-1.4 not really a pdf")

    assert response.status_code == 422
    assert "manually" in response.json()["detail"]


def test_a_pdf_with_no_text_fails_cleanly(client: TestClient) -> None:
    """A scanned page is a real, common case: valid PDF, zero extractable
    text. It must fail honestly rather than return an empty draft."""
    token = _token(client)

    response = _upload(client, token, content=build_pdf_bytes(""))

    assert response.status_code == 422
    assert "manually" in response.json()["detail"]


def test_importing_a_pdf_persists_absolutely_nothing(client: TestClient) -> None:
    """No saved job, no RESUME row, no stored file, no candidate skills,
    no evidence. The PDF is a transport format for text the user is about
    to review — not a document the product owes them access to."""
    token = _token(client)

    assert _upload(client, token).status_code == 200

    async def _counts(session: AsyncSession) -> tuple[int, int, int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(SavedJob))) or 0,
            (await session.scalar(select(func.count()).select_from(Resume))) or 0,
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
        )

    assert _run(_counts) == (0, 0, 0, 0)


def test_a_long_pdf_description_is_clipped_not_rejected(client: TestClient) -> None:
    """A posting can carry more text than a saved job accepts. Clipping
    with a note beats failing the whole import — the user is about to
    edit it anyway, and nothing is silently lost."""
    from app.schemas.saved_job import MAX_DESCRIPTION_LENGTH

    token = _token(client)
    long_text = "Backend engineering role. " * 4000
    assert len(long_text) > MAX_DESCRIPTION_LENGTH

    response = _upload(client, token, content=build_pdf_bytes(long_text))

    assert response.status_code == 200
    body = response.json()
    assert len(body["description"]) <= MAX_DESCRIPTION_LENGTH
    assert any("shortened" in note for note in body["notes"])
