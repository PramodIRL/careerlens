"""The dormant resume qualification extractor, and the authoritative
profile (Prompt 5.1b, revised by the eligibility UX refactor).

THE EXTRACTOR IS NO LONGER WIRED TO THE RESUME WORKER. Normal resume
processing is resume -> skills/evidence and nothing else; the profile is
user-declared and authoritative. `app/qualifications/extract.py` stays
on disk, tested and callable, for a future explicit "import from resume"
feature — these tests call it directly, which is now the only way it
runs.

The load-bearing tests here:

  * uploading a resume does NOT populate qualifications
  * only CONFIRMED facts are authoritative for eligibility
  * a user's value survives a later extraction
  * `/match` and `/gaps` are byte-identical to before this slice

Runs the REAL Celery task in eager mode, the same boundary
tests/test_demo_end_to_end.py documents: no Redis broker is involved,
so this proves the extraction pipeline rather than the transport in
front of it.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import build_session_factory, get_db
from app.main import app
from app.models.candidate_qualification import CandidateQualification
from app.qualifications.extract import extract_qualifications_for_resume
from app.rate_limit import _request_log
from app.settings import get_settings
from app.storage import get_resume_storage
from app.storage.local import LocalResumeStorage
from app.worker import celery_app, extract_resume_text
from scripts.sample_resumes import SAMPLE_RESUMES
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_RESUMES = "/api/v1/resumes"
_SKILLS = "/api/v1/candidate-skills"
_QUALIFICATIONS = "/api/v1/qualifications"
_JOBS = "/api/v1/saved-jobs"

_SAMPLE = next(s for s in SAMPLE_RESUMES if s.slug == "campus-graduate")

# The bars the fictional job sets. Chosen so the extracted facts clear
# every one of them, which is what makes "eligibility updates by itself"
# an observable outcome rather than a hopeful one.
# The candidate's declared profile — one set of values, reused by every
# test below and by the manual walkthrough.
_PROFILE = {
    "cgpa": "8.20",
    "cgpa_scale": "10.00",
    "class_10_percentage": "91.00",
    "class_12_percentage": "88.00",
    "highest_degree": "btech",
    "field_of_study": "computer_science",
    "graduation_year": 2026,
    "years_experience": "0.00",
}

_JOB_DESCRIPTION = (
    "Graduate Engineer at Nowhere Systems (fictional).\n\n"
    "Minimum CGPA 7.5. Minimum 80% in Class 12. "
    "B.Tech in Computer Science.\n\n"
    "Python is required. Docker is preferred."
)


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _use_temp_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
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
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture(autouse=True)
def _stub_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """See tests/test_demo_end_to_end.py: under eager mode the real
    enqueue would run inside the TestClient's loop, where the task's own
    `asyncio.run` refuses to start."""
    monkeypatch.setattr("app.api.v1.resume.enqueue_extraction", lambda resume_id: None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


def _isolated_session_factory() -> async_sessionmaker:  # type: ignore[type-arg]
    return build_session_factory(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )


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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_user(client: TestClient) -> str:
    email = f"{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"])


def _upload_and_process(client: TestClient, token: str) -> str:
    """The real path: upload through the endpoint, then run the real
    Celery task over it."""
    upload = client.post(
        _RESUMES,
        headers=_headers(token),
        files={"file": (_SAMPLE.filename, _SAMPLE.build(), _SAMPLE.content_type)},
    )
    assert upload.status_code == 201, upload.text
    resume_id = str(upload.json()["id"])
    extract_resume_text.delay(resume_id)
    return resume_id


def _reextract(resume_id: str) -> None:
    """Re-run qualification extraction over an ALREADY-PROCESSED resume.

    Called directly rather than by re-delivering the Celery task:
    `_claim_resume` only transitions queued -> processing, so a second
    `extract_resume_text.delay(...)` on a succeeded resume is a
    deliberate no-op — and a test built on it would assert nothing while
    appearing to pass.
    """
    _run(lambda session: extract_qualifications_for_resume(session, uuid.UUID(resume_id)))


def _create_job(client: TestClient, token: str) -> str:
    response = client.post(
        _JOBS,
        headers=_headers(token),
        json={
            "company": "Nowhere Systems",
            "title": "Graduate Engineer",
            "description": _JOB_DESCRIPTION,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _declare(client: TestClient, token: str, body: dict[str, Any]) -> None:
    response = client.patch(_QUALIFICATIONS, headers=_headers(token), json=body)
    assert response.status_code == 200, response.text


def _eligibility(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    response = client.get(f"{_JOBS}/{job_id}/eligibility", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _qualifications(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get(_QUALIFICATIONS, headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- the extractor is dormant -------------------------------------------


def test_uploading_a_resume_does_not_populate_qualifications(
    client: TestClient,
) -> None:
    """Normal resume processing is resume -> skills/evidence, full stop.

    The qualification profile is user-declared and authoritative, so the
    worker must not write to it. This is the regression guard for the
    removed hook: re-add it and this fails.
    """
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    _upload_and_process(client, token)

    payload = _qualifications(client, token)
    assert payload["cgpa"] is None
    assert payload["highest_degree"] is None
    assert payload["facts"] == {}


def test_the_resume_still_extracts_skills(client: TestClient) -> None:
    """Removing the qualification hook left skill extraction alone."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    _upload_and_process(client, token)

    skills = client.get(_SKILLS, headers=_headers(token)).json()
    assert {"Python", "SQL", "Git", "Docker"} <= {row["skill_name"] for row in skills}


def test_the_dormant_extractor_still_works_when_called(client: TestClient) -> None:
    """Kept on disk and tested for a future explicit "import from
    resume" feature — not deleted just because the UX no longer runs
    it."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    resume_id = _upload_and_process(client, token)
    _reextract(resume_id)

    facts = _qualifications(client, token)["facts"]
    cgpa = facts["cgpa"]
    assert cgpa["status"] == "suggested"
    assert cgpa["source_type"] == "resume"
    assert cgpa["source_identifier"] == resume_id
    # Verbatim, and not rewritten to "8.2/10".
    assert cgpa["excerpt"] == "CGPA: 8.2"


def test_reprocessing_the_same_resume_writes_nothing(client: TestClient) -> None:
    """Idempotency, asserted on TIMESTAMPS — a count-only check passes
    while every run silently bumps updated_at."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    resume_id = _upload_and_process(client, token)
    _reextract(resume_id)

    async def _snapshot(session: AsyncSession) -> set[tuple[Any, ...]]:
        rows = (await session.scalars(select(CandidateQualification))).all()
        return {
            (r.id, r.fact_type, r.value_numeric, r.value_text, r.created_at, r.updated_at)
            for r in rows
        }

    before = _run(_snapshot)
    assert before

    for _ in range(2):
        _reextract(resume_id)

    assert _run(_snapshot) == before


# --- only the user's own word is authoritative ---------------------------


def test_a_suggested_fact_is_not_authoritative_for_eligibility(
    client: TestClient,
) -> None:
    """THE RULE THIS REFACTOR RESTS ON. A machine's reading nobody has
    accepted reads as UNKNOWN — it must not decide a verdict about
    somebody, in either direction."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    resume_id = _upload_and_process(client, token)
    _reextract(resume_id)
    job_id = _create_job(client, token)

    payload = _eligibility(client, token, job_id)
    assert payload["has_qualification_profile"] is False
    assert payload["flag"] == "unknown"
    assert payload["totals"]["satisfied"] == 0
    assert payload["totals"]["not_satisfied"] == 0


def test_confirming_the_profile_makes_it_authoritative(client: TestClient) -> None:
    """Saving the form claims the values, and only then do they count."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    job_id = _create_job(client, token)
    assert _eligibility(client, token, job_id)["has_qualification_profile"] is False

    _declare(client, token, _PROFILE)

    payload = _eligibility(client, token, job_id)
    assert payload["has_qualification_profile"] is True
    assert payload["flag"] == "eligible"
    assert payload["totals"]["satisfied"] == 4


def test_a_rejected_fact_reads_as_unknown(client: TestClient) -> None:
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    job_id = _create_job(client, token)
    _declare(client, token, _PROFILE)
    assert _eligibility(client, token, job_id)["flag"] == "eligible"

    assert client.delete(f"{_QUALIFICATIONS}/cgpa", headers=_headers(token)).status_code == 200

    payload = _eligibility(client, token, job_id)
    cgpa = next(r for r in payload["requirements"] if r["requirement_type"] == "cgpa")
    assert cgpa["state"] == "unknown"
    assert payload["flag"] == "unknown"


def test_a_user_value_survives_a_later_extraction(client: TestClient) -> None:
    """A confirmed value outranks the extractor permanently, so a future
    "import from resume" cannot overwrite it."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    resume_id = _upload_and_process(client, token)
    _declare(client, token, {"cgpa": "9.10", "cgpa_scale": "10.00"})

    _reextract(resume_id)

    payload = _qualifications(client, token)
    assert float(payload["cgpa"]) == 9.1
    assert payload["facts"]["cgpa"]["status"] == "confirmed"


def test_a_partial_profile_is_unknown_never_a_failure(client: TestClient) -> None:
    """Missing data is not a negative answer. The job asks for four
    things, the candidate has asserted three."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    job_id = _create_job(client, token)
    partial = {key: value for key, value in _PROFILE.items() if key != "class_12_percentage"}
    _declare(client, token, partial)

    payload = _eligibility(client, token, job_id)
    assert payload["flag"] == "unknown"
    assert payload["totals"]["not_satisfied"] == 0
    states = {r["requirement_type"]: r["state"] for r in payload["requirements"]}
    assert states["class_12_percentage"] == "unknown"
    assert states["cgpa"] == "satisfied"


def test_the_profile_is_not_duplicated_per_job(client: TestClient) -> None:
    """One profile serves every saved job — no per-job qualification
    rows, however many jobs are saved."""
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    _declare(client, token, _PROFILE)
    for _ in range(3):
        _create_job(client, token)

    async def _fact_types(session: AsyncSession) -> list[str]:
        rows = (await session.scalars(select(CandidateQualification))).all()
        return [row.fact_type for row in rows]

    fact_types = _run(_fact_types)
    # Seven facts, not seven-per-job: `cgpa_scale` rides on the cgpa row
    # rather than being a fact of its own.
    assert sorted(fact_types) == sorted(set(fact_types))
    assert len(fact_types) == 7


# --- boundaries with Phase 4 --------------------------------------------


def test_reading_eligibility_stays_read_only(client: TestClient) -> None:
    _run(lambda s: seed_skill_taxonomy(s))
    token = _new_user(client)
    job_id = _create_job(client, token)
    _declare(client, token, _PROFILE)

    async def _snapshot(session: AsyncSession) -> set[tuple[Any, ...]]:
        rows = (await session.scalars(select(CandidateQualification))).all()
        return {(r.id, r.value_numeric, r.status, r.updated_at) for r in rows}

    before = _run(_snapshot)
    for _ in range(3):
        _eligibility(client, token, job_id)
    assert _run(_snapshot) == before


def test_the_skill_score_is_unaffected_by_qualifications(client: TestClient) -> None:
    """This refactor touches neither formula."""
    _run(lambda s: seed_skill_taxonomy(s))
    skills_only = "Python is required. Docker is preferred."

    plain_token = _new_user(client)
    plain_job = client.post(
        _JOBS,
        headers=_headers(plain_token),
        json={"company": "N", "title": "T", "description": skills_only},
    ).json()["id"]

    loaded_token = _new_user(client)
    loaded_job = client.post(
        _JOBS,
        headers=_headers(loaded_token),
        json={"company": "N", "title": "T", "description": skills_only},
    ).json()["id"]
    _declare(client, loaded_token, _PROFILE)

    plain_match = client.get(f"{_JOBS}/{plain_job}/match", headers=_headers(plain_token)).json()
    loaded_match = client.get(f"{_JOBS}/{loaded_job}/match", headers=_headers(loaded_token)).json()
    assert plain_match["formula_version"] == "skill_match_v1"
    assert plain_match["obtainable_weight"] == loaded_match["obtainable_weight"] == 5

    plain_gaps = client.get(f"{_JOBS}/{plain_job}/gaps", headers=_headers(plain_token)).json()
    assert plain_gaps["formula_version"] == "skill_gap_v1"
    for payload in (plain_match, plain_gaps, loaded_match):
        assert not any("qualif" in key or "eligib" in key for key in payload)
