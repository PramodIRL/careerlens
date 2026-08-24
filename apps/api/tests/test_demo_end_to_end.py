"""End-to-end acceptance test for the resume-to-skills demo (Prompt 2.5).

Walks the whole Prompt 2.1-2.4 chain in one test, over real HTTP calls
and a real fictional PDF: sign up -> log in -> upload -> extraction
completes -> view the evidence -> confirm one skill -> remove another ->
re-run extraction and prove both decisions survived. Every other test in
this suite covers one slice in isolation; this one exists to prove the
slices actually join up, and to be the thing a demo can be trusted
against.

WHAT THIS TEST DOES *NOT* PROVE. It runs Celery in eager mode, so
`extract_resume_text.delay(...)` executes inline, in-process. **No Redis
broker is involved at any point, and nothing here demonstrates that a
message published by the API is delivered to a separate worker
process.** The test substitutes "a worker picked the job up" for "the
broker delivered it": what it verifies is the extraction pipeline —
claim, extract, persist, match, write evidence — not the transport in
front of it. Broker delivery is covered only by running the real stack
(`make start` + `make start-worker`, see docs/demo.md); CI has no Redis
service at all (.github/workflows/api-ci.yml).

Two more boundaries worth being precise about:

  * The upload endpoint's own call to `enqueue_extraction` is stubbed to
    a recorder, and the test then invokes the task itself. Not cosmetic:
    under eager mode that call would run inside the TestClient's already
    -running event loop, and `extract_resume_text` calls `asyncio.run()`
    internally, which refuses to start there. The resulting RuntimeError
    is swallowed by `enqueue_extraction`'s deliberate best-effort
    `try/except` (app/worker.py), so the resume would silently sit at
    "queued" forever. The recorder keeps the "upload asks for
    extraction" link asserted, while the explicit call makes the worker
    step visible rather than accidental.
  * This is the server-side chain. It does not render the dashboard —
    the browser layer is covered by apps/web's component tests
    (resume-section.test.tsx, skills-section.test.tsx) against mocked
    responses of exactly the shape asserted here, plus the manual
    walkthrough in docs/demo.md.

Test functions are plain sync `def`s for the reason
tests/test_extraction.py documents: the Celery task calls
`asyncio.run(...)` internally, which cannot start inside an
already-running event loop.
"""

import asyncio
import re
import uuid
from collections.abc import Coroutine, Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import build_session_factory, get_db
from app.extraction import extract_docx_text, extract_pdf_text
from app.main import app
from app.rate_limit import _request_log
from app.schemas.resume import ResumeStatus
from app.schemas.skill import (
    CandidateSkillStatus,
    EvidenceSourceType,
    ExtractionMethod,
)
from app.settings import get_settings
from app.storage import get_resume_storage
from app.storage.local import LocalResumeStorage
from app.worker import celery_app, extract_resume_text
from scripts.sample_resumes import (
    FICTION_NOTICE,
    PDF_CONTENT_TYPE,
    SAMPLE_RESUMES,
    SampleResume,
)
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_RESUMES = "/api/v1/resumes"
_SKILLS = "/api/v1/candidate-skills"

_SAMPLES_BY_SLUG = {sample.slug: sample for sample in SAMPLE_RESUMES}

# Exactly what app/skill_matching.py finds in the backend-engineer
# sample. Asserted as a SET EQUALITY, not a subset, on purpose: a subset
# check would pass while the extractor invented a skill the document
# never mentions, which is the one failure docs/project-brief.md's
# Evidence-First rule exists to prevent. If a taxonomy change moves this
# number, that is a real signal about the demo, not test noise.
_EXPECTED_BACKEND_SKILLS = {
    "CI/CD",
    "Django",
    "Docker",
    "FastAPI",
    "Git",
    "PostgreSQL",
    "Python",
    "REST APIs",
    "Redis",
    "SQL",
    "Unit Testing",
    "pytest",
}

# The demo's two decisions. Ada's document lists Django under SKILLS but
# every experience bullet is FastAPI — so "confirm FastAPI, reject
# Django" is the honest correction a real candidate would make, which is
# what docs/demo.md walks through.
_SKILL_TO_CONFIRM = "FastAPI"
_SKILL_TO_REJECT = "Django"

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+")
# Any digit run long enough to be a phone number, in the shapes a resume
# writes them.
_PHONE_PATTERN = re.compile(r"\b\d{3}[-.\s]?\d{4,}\b")


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _use_temp_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """One tmp_path shared by the API and the worker.

    They must agree: the upload endpoint writes the file, and the worker
    reads it back by the same storage key. Pointing them at two
    different directories would fail in a confusing way (a storage read
    error, retried, then "could not be read"), and pointing either at
    the real RESUME_STORAGE_DIR would leave test uploads in the dev
    developer's own resume directory.
    """
    storage = LocalResumeStorage(tmp_path)
    app.dependency_overrides[get_resume_storage] = lambda: storage
    monkeypatch.setattr("app.worker.get_resume_storage", lambda: storage)
    yield
    app.dependency_overrides.pop(get_resume_storage, None)


@pytest.fixture(autouse=True)
def _use_isolated_worker_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.worker.get_worker_session_factory", _isolated_session_factory)


@pytest.fixture(autouse=True)
def _celery_eager() -> Generator[None, None, None]:
    # Same singleton reset as tests/test_extraction.py — celery_app is
    # module-level and shared by the whole test process.
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records what the upload endpoint asked to have extracted — see
    this module's docstring for why the real enqueue is not used here."""
    recorded: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.resume.enqueue_extraction",
        lambda resume_id: recorded.append(str(resume_id)),
    )
    return recorded


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    # Registering/logging in draws on the shared register/login/refresh
    # budget (app/rate_limit.py); without this, earlier tests' requests
    # count against this one. Same fixture as tests/test_auth.py.
    _request_log.clear()


def _isolated_session_factory() -> async_sessionmaker:  # type: ignore[type-arg]
    return build_session_factory(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _seed_taxonomy_async() -> None:
    async with _isolated_session_factory()() as db:
        await seed_skill_taxonomy(db)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _extracted_text(sample: SampleResume) -> str:
    extract = extract_pdf_text if sample.content_type == PDF_CONTENT_TYPE else extract_docx_text
    return extract(sample.build())


def test_every_sample_resume_is_fictional() -> None:
    """The guard that keeps a real resume out of the repo.

    scripts/sample_resumes.py claims its data is invented; this is what
    makes the claim enforceable. Someone pasting a genuine resume in to
    "make the demo more realistic" fails CI here rather than shipping a
    real person's name, address and phone number into a demo, a
    screenshot and every dev database that ever loads it.
    """
    for sample in SAMPLE_RESUMES:
        assert sample.text.splitlines()[0] == FICTION_NOTICE, sample.slug
        assert sample.text.isascii(), sample.slug

        # RFC 2606 reserves example.com precisely so documentation and
        # test data cannot reach a real mailbox.
        for email in _EMAIL_PATTERN.findall(sample.text):
            assert email.endswith("@example.com"), f"{sample.slug}: {email}"

        # 555-01xx is the range reserved for fiction, so a sample number
        # cannot ring a real phone.
        for phone in _PHONE_PATTERN.findall(sample.text):
            assert phone.startswith("555-01"), f"{sample.slug}: {phone}"

        # The document a demo actually uploads is the rendered file, not
        # this string — so check the notice survives the round trip.
        assert FICTION_NOTICE in _extracted_text(sample), sample.slug


def test_demo_flow_end_to_end(client: TestClient, enqueued: list[str]) -> None:
    """Sign up -> upload -> extraction -> evidence -> confirm -> remove.

    The exact flow docs/demo.md walks a viewer through, asserted step by
    step. See this module's docstring for what "extraction" does and does
    not cover (no Redis broker).
    """
    _run(_seed_taxonomy_async())

    # 1. Sign up, then log in with the same credentials.
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201

    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    token = login.json()["access_token"]

    # A brand-new account starts with nothing: no resumes, no skills.
    assert client.get(_RESUMES, headers=_headers(token)).json() == []
    assert client.get(_SKILLS, headers=_headers(token)).json() == []

    # 2. Upload the fictional resume through the real endpoint.
    sample = _SAMPLES_BY_SLUG["backend-engineer"]
    upload = client.post(
        _RESUMES,
        headers=_headers(token),
        files={"file": (sample.filename, sample.build(), sample.content_type)},
    )
    assert upload.status_code == 201
    resume_id = upload.json()["id"]
    assert upload.json()["status"] == ResumeStatus.QUEUED.value
    # The upload asked for extraction — the link this test then performs
    # by hand (see the module docstring).
    assert enqueued == [resume_id]

    # 3. The worker processes the queued job.
    extract_resume_text.delay(resume_id)

    # 4. Extraction completed — this GET is exactly what the dashboard's
    #    ResumeSection polls while a resume is pending.
    detail = client.get(f"{_RESUMES}/{resume_id}", headers=_headers(token))
    assert detail.status_code == 200
    assert detail.json()["status"] == ResumeStatus.SUCCEEDED.value
    assert detail.json()["error_message"] is None
    # Extracted text is never exposed by any resume endpoint (Prompt 2.1).
    assert "extracted_text" not in detail.json()

    # 5. Skills are suggested, and every one of them shows its evidence.
    listed = client.get(_SKILLS, headers=_headers(token))
    assert listed.status_code == 200
    skills = listed.json()

    assert {skill["skill_name"] for skill in skills} == _EXPECTED_BACKEND_SKILLS
    assert all(skill["status"] == CandidateSkillStatus.SUGGESTED.value for skill in skills)

    for skill in skills:
        assert skill["evidence"], f"{skill['skill_name']} has no evidence"
        for evidence in skill["evidence"]:
            assert evidence["source_type"] == EvidenceSourceType.RESUME.value
            assert evidence["source_identifier"] == resume_id
            assert evidence["extraction_method"] == ExtractionMethod.RESUME_ALIAS_MATCH.value
            assert 0 < evidence["confidence"] <= 1
            # The excerpt is a VERBATIM slice of the uploaded document,
            # not a paraphrase or a summary — a reader can find it on the
            # page by eye. This is the Evidence-First rule as an
            # assertion rather than a promise.
            assert evidence["excerpt"] in sample.text

    by_name = {skill["skill_name"]: skill for skill in skills}

    # 6. Confirm one skill.
    confirm = client.patch(
        f"{_SKILLS}/{by_name[_SKILL_TO_CONFIRM]['id']}",
        headers=_headers(token),
        json={"status": CandidateSkillStatus.CONFIRMED.value},
    )
    assert confirm.status_code == 200
    assert confirm.json()["status"] == CandidateSkillStatus.CONFIRMED.value

    # 7. Remove another. "Removed" is Prompt 2.4's rejection tombstone,
    #    not a DELETE: a hard delete would let step 8's re-run faithfully
    #    re-suggest the skill and silently discard the user's decision.
    reject = client.patch(
        f"{_SKILLS}/{by_name[_SKILL_TO_REJECT]['id']}",
        headers=_headers(token),
        json={"status": CandidateSkillStatus.REJECTED.value},
    )
    assert reject.status_code == 200
    assert reject.json()["status"] == CandidateSkillStatus.REJECTED.value
    # The tombstone keeps its evidence, so the UI can still explain why
    # the skill was ever suggested.
    assert reject.json()["evidence"]

    after = {
        skill["skill_name"]: skill for skill in client.get(_SKILLS, headers=_headers(token)).json()
    }
    assert after[_SKILL_TO_CONFIRM]["status"] == CandidateSkillStatus.CONFIRMED.value
    assert after[_SKILL_TO_REJECT]["status"] == CandidateSkillStatus.REJECTED.value

    # 8. Re-running extraction must not undo either decision — the
    #    property the whole Prompt 2.4 override design rests on, checked
    #    here across the HTTP boundary rather than at the unit level.
    extract_resume_text.delay(resume_id)

    final = {
        skill["skill_name"]: skill for skill in client.get(_SKILLS, headers=_headers(token)).json()
    }
    assert set(final) == _EXPECTED_BACKEND_SKILLS
    assert final[_SKILL_TO_CONFIRM]["status"] == CandidateSkillStatus.CONFIRMED.value
    assert final[_SKILL_TO_REJECT]["status"] == CandidateSkillStatus.REJECTED.value
    assert all(
        skill["status"] == CandidateSkillStatus.SUGGESTED.value
        for name, skill in final.items()
        if name not in {_SKILL_TO_CONFIRM, _SKILL_TO_REJECT}
    )
    # And no evidence was duplicated by the second run.
    for name, skill in final.items():
        assert len(skill["evidence"]) == len(by_name[name]["evidence"])
