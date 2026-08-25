"""Phase 4 MVP acceptance test (Prompt 4.5).

Walks the ENTIRE product in one journey, over real HTTP, with fictional
data only:

    register -> upload resume -> extraction -> candidate skills+evidence
    -> connect GitHub -> ingest -> GitHub evidence -> save a job
    -> job requirement extraction -> /match -> /gaps
    -> confirm / reject / edit -> updated match and gaps

Every other test in this suite proves one slice. tests/
test_demo_end_to_end.py proves the Prompt 2.x slice joins up. THIS one
exists to prove the whole of docs/project-brief.md's MVP boundary joins
up — "user profile -> resume extraction -> GitHub evidence -> saved jobs
-> ranked matches -> score breakdown -> skill gaps" — and to be the
thing the Phase 4 release is signed off against.

THE SCORE IS ASSERTED AS AN EXACT NUMBER, hand-calculated in
docs/phase-4-acceptance.md and repeated in _EXPECTED_* below. Asserting
`score > 0` would pass on an implementation that scored everything at
1%, which is precisely the failure an "explainable matching" product
cannot ship. If a deliberate change moves these numbers, the version
string (`skill_match_v1`) is what makes changing them honest, and this
file is where the new arithmetic gets written down.

WHAT THIS TEST DOES *NOT* PROVE, stated as precisely as
tests/test_demo_end_to_end.py states its own boundaries:

  * NO REDIS BROKER. Celery runs in eager mode and both enqueue calls
    are stubbed to recorders, so this proves the resume-extraction and
    GitHub-ingestion PIPELINES, not that a message published by the API
    reaches a separate worker process. Only running the real stack shows
    that (`make start` + `make start-worker`), which is why
    docs/phase-4-acceptance.md exists as a manual walkthrough.
  * NO BROWSER. This is the server-side chain. The dashboard has its own
    component tests against mocked responses of exactly these shapes,
    and the manual walkthrough is what puts the two halves together.
  * NO NETWORK. scripts/sample_github.py answers every GitHub call from
    invented constants; nothing here can reach github.com.

Test functions are plain sync `def`s for the reason
tests/test_extraction.py documents: the Celery tasks call
`asyncio.run(...)` internally, which cannot start inside an
already-running event loop.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Coroutine, Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import build_session_factory, get_db
from app.github import get_github_client
from app.main import app
from app.matching.score import FORMULA_VERSION as MATCH_FORMULA_VERSION
from app.models.candidate_skill import CandidateSkill
from app.models.job_requirement import JobSkillRequirement
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.schemas.resume import ResumeStatus
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType
from app.settings import get_settings
from app.storage import get_resume_storage
from app.storage.local import LocalResumeStorage
from app.worker import celery_app, extract_resume_text, ingest_github_repositories
from scripts.sample_github import (
    FICTION_NOTICE as GITHUB_FICTION_NOTICE,
)
from scripts.sample_github import (
    SAMPLE_REPOSITORIES,
    SAMPLE_USER,
    SAMPLE_USER_ID,
    SAMPLE_USERNAME,
    SampleGitHubClient,
)
from scripts.sample_resumes import SAMPLE_RESUMES
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_RESUMES = "/api/v1/resumes"
_SKILLS = "/api/v1/candidate-skills"
_JOBS = "/api/v1/saved-jobs"
_GITHUB = "/api/v1/github-connection"

_SAMPLES_BY_SLUG = {sample.slug: sample for sample in SAMPLE_RESUMES}

# --------------------------------------------------------------------
# The fictional job. Every level the classifier recognises appears, and
# one requirement is stated as a CAPABILITY rather than a keyword
# ("Candidates should be able to write SQL") so the Prompt 4.2 follow-up
# is exercised by the acceptance run rather than only by its own tests.
# --------------------------------------------------------------------
_JOB_COMPANY = "Nowhere Systems"
_JOB_TITLE = "Backend Engineer"
_JOB_DESCRIPTION = (
    "Backend Engineer at Nowhere Systems (fictional).\n\n"
    "Python is required for this role. PostgreSQL is required. "
    "Candidates should be able to write SQL. Kubernetes is required.\n\n"
    "Docker experience is preferred. FastAPI is preferred. "
    "Familiarity with Linux is preferred.\n\n"
    "We also use Redis here."
)

# The edit applied in step 12. Chosen to exercise all three of Prompt
# 4.2's reconciliation operations at once: Kubernetes is DELETED, Docker
# is UPDATED IN PLACE from preferred to required, and Git is INSERTED.
_JOB_DESCRIPTION_EDITED = (
    "Backend Engineer at Nowhere Systems (fictional).\n\n"
    "Python is required for this role. PostgreSQL is required. "
    "Candidates should be able to write SQL. Docker is required. "
    "Git is required.\n\n"
    "FastAPI is preferred. Familiarity with Linux is preferred.\n\n"
    "We also use Redis here."
)

# (skill_name, requirement_level, job_excerpt). Asserted as a SET
# EQUALITY, never a subset: a subset check passes while the extractor
# invents a requirement the posting never states, which is the exact
# failure the Evidence-First rule exists to prevent.
_EXPECTED_REQUIREMENTS = {
    ("Python", "required", "Python is required for this role"),
    ("PostgreSQL", "required", "PostgreSQL is required"),
    ("SQL", "required", "Candidates should be able to write SQL"),
    ("Kubernetes", "required", "Kubernetes is required"),
    ("Docker", "preferred", "Docker experience is preferred"),
    ("FastAPI", "preferred", "FastAPI is preferred"),
    ("Linux", "preferred", "Familiarity with Linux is preferred"),
    ("Redis", "mentioned", "We also use Redis here"),
}

_EXPECTED_REQUIREMENTS_AFTER_EDIT = {
    ("Python", "required", "Python is required for this role"),
    ("PostgreSQL", "required", "PostgreSQL is required"),
    ("SQL", "required", "Candidates should be able to write SQL"),
    ("Docker", "required", "Docker is required"),
    ("Git", "required", "Git is required"),
    ("FastAPI", "preferred", "FastAPI is preferred"),
    ("Linux", "preferred", "Familiarity with Linux is preferred"),
    ("Redis", "mentioned", "We also use Redis here"),
}

# Ada Sample's resume (scripts/sample_resumes.py) never mentions Linux;
# only octofictional/deploy-notes does. Asserted explicitly because it
# is the one requirement in this run that ONLY repository evidence can
# satisfy — the "GitHub closed this gap" property.
_GITHUB_ONLY_SKILL = "Linux"

# Named in a fork's description AND its topics
# (octofictional/borrowed-toolkit), and credited by nothing, because
# forks are never credited. That makes "genuinely missing" a real
# property here rather than the trivial case of a word that never
# appears anywhere.
_GENUINELY_MISSING_SKILL = "Kubernetes"

# --------------------------------------------------------------------
# skill_match_v1, by hand. weights: required=3, preferred=2, mentioned=1
#
#   Python      required  3  confirmed        satisfied  +3
#   PostgreSQL  required  3  suggested        satisfied  +3
#   SQL         required  3  confirmed        satisfied  +3
#   Kubernetes  required  3  no row           MISSING     0
#   Docker      preferred 2  rejected         MISSING     0
#   FastAPI     preferred 2  confirmed        satisfied  +2
#   Linux       preferred 2  suggested        satisfied  +2
#   Redis       mentioned 1  suggested        satisfied  +1
#                                                       ----
#   earned                                               14
#   obtainable  3+3+3+3+2+2+2+1                          19
#   overall     round(14 / 19 * 100) = round(73.68)      74
# --------------------------------------------------------------------
_EXPECTED_EARNED = 14
_EXPECTED_OBTAINABLE = 19
_EXPECTED_SCORE = 74
_EXPECTED_REQUIRED_MATCHED = 3
_EXPECTED_REQUIRED_TOTAL = 4

# After rejecting Redis: earned 14 - 1 = 13, obtainable unchanged.
#   round(13 / 19 * 100) = round(68.42) = 68
_EXPECTED_SCORE_AFTER_REJECT = 68
_EXPECTED_EARNED_AFTER_REJECT = 13

# After the job edit. Kubernetes is gone, Docker is now required (and
# still rejected), Git is newly required (and suggested from both
# sources, so satisfied):
#   Python 3 + PostgreSQL 3 + SQL 3 + Docker 0 + Git 3
#   + FastAPI 2 + Linux 2 + Redis 0                       = 16
#   obtainable 3+3+3+3+3+2+2+1                            = 20
#   overall round(16 / 20 * 100)                          = 80
_EXPECTED_SCORE_AFTER_EDIT = 80
_EXPECTED_EARNED_AFTER_EDIT = 16
_EXPECTED_OBTAINABLE_AFTER_EDIT = 20

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+")


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _use_temp_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """One tmp_path shared by the API and the worker — same reasoning as
    tests/test_demo_end_to_end.py: they must agree on the storage key,
    and neither may write into the developer's real resume directory."""
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


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Generator[SampleGitHubClient, None, None]:
    """The fictional GitHub account, installed at BOTH seams the real
    client is resolved through.

    Two seams, because the two halves of the GitHub flow resolve their
    client differently: the connection endpoint takes it as a FastAPI
    dependency, and the ingestion task calls the factory directly. One
    shared instance so a test can assert what the whole pipeline asked
    for.
    """
    client = SampleGitHubClient()
    app.dependency_overrides[get_github_client] = lambda: client
    monkeypatch.setattr("app.worker.get_github_client", lambda: client)
    yield client
    app.dependency_overrides.pop(get_github_client, None)


@pytest.fixture
def enqueued_resumes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """See tests/test_demo_end_to_end.py: under eager mode the real
    enqueue would run inside the TestClient's event loop, where the
    task's own `asyncio.run` refuses to start — and the resulting error
    is swallowed by the best-effort enqueue, leaving the resume queued
    forever. Recording the call keeps the link asserted while the test
    performs the worker step explicitly."""
    recorded: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.resume.enqueue_extraction",
        lambda resume_id: recorded.append(str(resume_id)),
    )
    return recorded


@pytest.fixture
def enqueued_ingestions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The same substitution for GitHub ingestion, for the same reason."""
    recorded: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.github_ingestion.enqueue_github_ingestion",
        lambda run_id: recorded.append(str(run_id)),
    )
    return recorded


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
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


def _query[T](coro_factory: Any) -> T:
    async def _inner() -> T:
        async with _isolated_session_factory()() as session:
            return await coro_factory(session)  # type: ignore[no-any-return]

    return asyncio.run(_inner())


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient) -> tuple[str, str]:
    """A fresh fictional account. example.com is reserved by RFC 2606."""
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201, register.text
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"]), str(register.json()["id"])


def _requirement_triples(payload: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """(skill, level, excerpt) for one /requirements response.

    Note the field is `excerpt` here and `job_excerpt` on /match and
    /gaps: the requirements resource is already scoped to one job, while
    the other two mix job-side and candidate-side text in one entry and
    have to say which is which.
    """
    return {(row["skill_name"], row["requirement_level"], row["excerpt"]) for row in payload}


def _bucket_names(gaps: dict[str, Any], bucket: str) -> list[str]:
    return [entry["skill_name"] for entry in gaps[bucket]]


def _set_status(client: TestClient, token: str, skill_id: str, status: str) -> dict[str, Any]:
    response = client.patch(
        f"{_SKILLS}/{skill_id}", headers=_headers(token), json={"status": status}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- the fictional-data guard -------------------------------------------


def test_every_github_fixture_is_fictional() -> None:
    """What keeps a real GitHub account out of the repo.

    The sibling of tests/test_demo_end_to_end.py's resume guard, and
    load-bearing for the same reason: somebody making the demo "more
    realistic" by pointing it at a colleague's account fails here rather
    than putting a real person's repositories into a dev database, a
    screenshot, and every machine that ever runs the demo.
    """
    assert SAMPLE_USER.login == SAMPLE_USERNAME
    assert SAMPLE_USER.id == SAMPLE_USER_ID
    # Outside the id range GitHub has issued, so it cannot collide with
    # a real account.
    assert SAMPLE_USER_ID >= 9_000_000
    assert SAMPLE_USER.public_repos == len(SAMPLE_REPOSITORIES)

    for sample in SAMPLE_REPOSITORIES:
        repo = sample.repository
        assert repo.full_name.startswith(f"{SAMPLE_USERNAME}/"), repo.full_name
        assert repo.full_name == f"{SAMPLE_USERNAME}/{repo.name}"
        # Every stored string is rendered as evidence somewhere, so all
        # of it has to be safe to put on a screen.
        assert repo.description is None or repo.description.isascii(), repo.full_name
        if sample.readme is not None:
            assert sample.readme.text.splitlines()[0] == GITHUB_FICTION_NOTICE, repo.full_name
            assert sample.readme.text.isascii(), repo.full_name
            # RFC 2606 again: fixture text must not name a reachable
            # mailbox.
            for email in _EMAIL_PATTERN.findall(sample.readme.text):
                assert email.endswith("@example.com"), f"{repo.full_name}: {email}"

    # The fixture must be able to answer without a network stack at all.
    assert not hasattr(SampleGitHubClient, "_client")


def test_the_github_fixture_is_stable_across_processes() -> None:
    """README shas must be CONTENT hashes, not `hash(text)`.

    Python randomises string hashing per process, so a `hash()`-derived
    sha differs on every run. app/github/ingestion.py skips the README
    write only when the sha is unchanged — so a per-process sha would
    make `make demo-github` rewrite `readme_text` every single time,
    silently breaking the "safe to re-run, writes nothing" property the
    demo script promises. In-process equality cannot catch that, so this
    recomputes the expected digest independently.
    """
    for sample in SAMPLE_REPOSITORIES:
        if sample.readme is None:
            continue
        expected = hashlib.sha1(
            sample.readme.text.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        assert sample.readme.sha == expected, sample.repository.full_name
        assert sample.readme.size_bytes == len(sample.readme.text)


# --- the acceptance journey ---------------------------------------------


def test_phase_4_mvp_acceptance(
    client: TestClient,
    github: SampleGitHubClient,
    enqueued_resumes: list[str],
    enqueued_ingestions: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole MVP, end to end, in the order docs/project-brief.md
    defines it. Each numbered step below matches a step in
    docs/phase-4-acceptance.md, so the live demo and this test cannot
    drift apart.
    """
    caplog.set_level(logging.DEBUG)
    _run(_seed_taxonomy_async())

    # --- 1. A new user starts with nothing ------------------------------
    token, user_id = _register(client)
    assert client.get(_RESUMES, headers=_headers(token)).json() == []
    assert client.get(_SKILLS, headers=_headers(token)).json() == []
    assert client.get(_JOBS, headers=_headers(token)).json() == []
    assert client.get(_GITHUB, headers=_headers(token)).json() is None

    # --- 2. Upload the fictional resume ---------------------------------
    sample = _SAMPLES_BY_SLUG["backend-engineer"]
    upload = client.post(
        _RESUMES,
        headers=_headers(token),
        files={"file": (sample.filename, sample.build(), sample.content_type)},
    )
    assert upload.status_code == 201, upload.text
    resume_id = upload.json()["id"]
    # The resume is visibly pending before any work happens — the state
    # the dashboard polls on.
    assert upload.json()["status"] == ResumeStatus.QUEUED.value
    assert enqueued_resumes == [resume_id]

    # --- 3. Extraction completes ----------------------------------------
    extract_resume_text.delay(resume_id)
    detail = client.get(f"{_RESUMES}/{resume_id}", headers=_headers(token))
    assert detail.status_code == 200
    assert detail.json()["status"] == ResumeStatus.SUCCEEDED.value
    assert detail.json()["error_message"] is None
    # Extracted text is never exposed by any resume endpoint (Prompt 2.1).
    assert "extracted_text" not in detail.json()

    # --- 4. Candidate skills, each carrying resume evidence -------------
    resume_skills = client.get(_SKILLS, headers=_headers(token)).json()
    resume_skill_names = {row["skill_name"] for row in resume_skills}
    assert resume_skill_names, "resume extraction produced no skills"
    assert all(row["status"] == CandidateSkillStatus.SUGGESTED.value for row in resume_skills)
    for row in resume_skills:
        assert row["evidence"], f"{row['skill_name']} has no evidence"
        for evidence in row["evidence"]:
            assert evidence["source_type"] == EvidenceSourceType.RESUME.value
            assert evidence["source_identifier"] == resume_id
            # Verbatim, never paraphrased — the Evidence-First rule as an
            # assertion.
            assert evidence["excerpt"] in sample.text

    # The resume alone cannot supply the GitHub-only skill. Asserted
    # BEFORE the import so the next step's effect is unambiguous.
    assert _GITHUB_ONLY_SKILL not in resume_skill_names

    # --- 5. Connect the fictional GitHub account ------------------------
    connect = client.put(_GITHUB, headers=_headers(token), json={"username": SAMPLE_USERNAME})
    assert connect.status_code == 201, connect.text
    assert connect.json()["username"] == SAMPLE_USERNAME
    # Stored from GitHub's own answer, never from the request body.
    assert connect.json()["github_user_id"] == SAMPLE_USER_ID

    # --- 6. Import repositories -----------------------------------------
    start = client.post(f"{_GITHUB}/ingestions", headers=_headers(token))
    assert start.status_code == 202, start.text
    run_id = start.json()["id"]
    assert enqueued_ingestions == [run_id]

    ingest_github_repositories.delay(run_id)

    latest = client.get(f"{_GITHUB}/ingestions/latest", headers=_headers(token)).json()
    assert latest["status"] == "succeeded", latest
    # The fork was excluded before any detail request was spent on it.
    assert latest["repositories_forks_excluded"] == 1
    assert latest["repositories_total"] == 3
    assert latest["repositories_completed"] == 3
    assert latest["error_message"] is None

    repositories = client.get(f"{_GITHUB}/repositories", headers=_headers(token)).json()
    assert {repo["full_name"] for repo in repositories} == {
        sample.repository.full_name for sample in SAMPLE_REPOSITORIES
    }
    # The pipeline really did fetch detail — not a fixture that answered
    # a listing and stopped.
    assert ("get_languages", f"{SAMPLE_USERNAME}/ledger-service") in github.calls
    assert ("get_readme", f"{SAMPLE_USERNAME}/deploy-notes") in github.calls
    # ...and never asked for the fork's detail.
    assert not any(
        call[1] == f"{SAMPLE_USERNAME}/borrowed-toolkit"
        for call in github.calls
        if call[0] in {"get_languages", "get_readme"}
    )

    # --- 7. GitHub evidence, attached to canonical candidate skills -----
    after_github = client.get(_SKILLS, headers=_headers(token)).json()
    by_name = {row["skill_name"]: row for row in after_github}

    assert _GITHUB_ONLY_SKILL in by_name, "the GitHub import added no new skill"
    github_only = by_name[_GITHUB_ONLY_SKILL]
    assert github_only["evidence"]
    assert all(
        evidence["source_type"] == EvidenceSourceType.GITHUB.value
        for evidence in github_only["evidence"]
    ), "Linux should be backed by repository evidence alone"
    assert {evidence["source_identifier"] for evidence in github_only["evidence"]} == {
        f"{SAMPLE_USERNAME}/deploy-notes"
    }

    # A skill both sources support carries BOTH provenances on one row —
    # the point of matching on canonical skill_id rather than by name.
    python_sources = {evidence["source_type"] for evidence in by_name["Python"]["evidence"]}
    assert python_sources == {
        EvidenceSourceType.RESUME.value,
        EvidenceSourceType.GITHUB.value,
    }

    # The fork named Kubernetes in its description AND its topics, and
    # was credited for neither.
    assert _GENUINELY_MISSING_SKILL not in by_name

    # --- 8. Re-importing is idempotent ----------------------------------
    evidence_before = _query(lambda s: s.scalar(select(func.count()).select_from(SkillEvidence)))
    second_run = client.post(f"{_GITHUB}/ingestions", headers=_headers(token))
    assert second_run.status_code == 202, second_run.text
    ingest_github_repositories.delay(second_run.json()["id"])
    evidence_after = _query(lambda s: s.scalar(select(func.count()).select_from(SkillEvidence)))
    assert evidence_after == evidence_before, "a second import duplicated evidence"

    # --- 9. Save the fictional job --------------------------------------
    save = client.post(
        _JOBS,
        headers=_headers(token),
        json={
            "company": _JOB_COMPANY,
            "title": _JOB_TITLE,
            "description": _JOB_DESCRIPTION,
        },
    )
    assert save.status_code == 201, save.text
    job_id = save.json()["id"]

    requirements = client.get(f"{_JOBS}/{job_id}/requirements", headers=_headers(token))
    assert requirements.status_code == 200
    assert _requirement_triples(requirements.json()) == _EXPECTED_REQUIREMENTS
    # Every excerpt is a verbatim slice of the description the user
    # saved — the job-side half of Evidence-First.
    for row in requirements.json():
        assert row["excerpt"] in _JOB_DESCRIPTION, row["excerpt"]

    # --- 10. Put the candidate into the four resolution states ----------
    # Confirm three, reject one, leave three suggested, and leave
    # Kubernetes with no row at all.
    for name in ("Python", "SQL", "FastAPI"):
        _set_status(client, token, by_name[name]["id"], CandidateSkillStatus.CONFIRMED.value)
    _set_status(client, token, by_name["Docker"]["id"], CandidateSkillStatus.REJECTED.value)

    # --- 11. The match, against the hand calculation --------------------
    match = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token))
    assert match.status_code == 200, match.text
    scored = match.json()

    assert scored["formula_version"] == MATCH_FORMULA_VERSION
    assert scored["has_requirements"] is True
    assert scored["weights"] == {"required": 3, "preferred": 2, "mentioned": 1}
    assert scored["earned_weight"] == _EXPECTED_EARNED
    assert scored["obtainable_weight"] == _EXPECTED_OBTAINABLE
    assert scored["overall_score"] == _EXPECTED_SCORE
    assert scored["required_matched"] == _EXPECTED_REQUIRED_MATCHED
    assert scored["required_total"] == _EXPECTED_REQUIRED_TOTAL
    assert scored["by_level"] == {
        "required": {"matched": 3, "total": 4},
        "preferred": {"matched": 2, "total": 3},
        "mentioned": {"matched": 1, "total": 1},
    }
    assert {row["skill_name"] for row in scored["matched_skills"]} == {
        "Python",
        "PostgreSQL",
        "SQL",
        "FastAPI",
        "Linux",
        "Redis",
    }
    assert {row["skill_name"] for row in scored["missing_skills"]} == {"Kubernetes", "Docker"}
    assert [row["skill_name"] for row in scored["required_missing"]] == [_GENUINELY_MISSING_SKILL]
    # The arithmetic the response reports is the arithmetic it did.
    assert (
        round(scored["earned_weight"] / scored["obtainable_weight"] * 100)
        == scored["overall_score"]
    )

    # --- 12. The gaps, bucket by bucket ---------------------------------
    gaps = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()

    assert _bucket_names(gaps, "required_gaps") == [_GENUINELY_MISSING_SKILL]
    assert _bucket_names(gaps, "preferred_gaps") == []
    assert _bucket_names(gaps, "informational_gaps") == []
    assert _bucket_names(gaps, "needs_confirmation") == ["Linux", "PostgreSQL", "Redis"]
    assert _bucket_names(gaps, "rejected_requirements") == ["Docker"]
    assert gaps["totals"] == {
        "required_gaps": 1,
        "preferred_gaps": 0,
        "informational_gaps": 0,
        "needs_confirmation": 3,
        "rejected_requirements": 1,
        "satisfied": 3,
        "total_requirements": 8,
    }
    # The buckets account for every requirement exactly once.
    assert (
        gaps["totals"]["required_gaps"]
        + gaps["totals"]["preferred_gaps"]
        + gaps["totals"]["informational_gaps"]
        + gaps["totals"]["needs_confirmation"]
        + gaps["totals"]["rejected_requirements"]
        + gaps["totals"]["satisfied"]
        == gaps["totals"]["total_requirements"]
    )
    # A genuinely missing skill carries an EMPTY evidence list, never an
    # invented "no evidence found" sentence.
    assert gaps["required_gaps"][0]["candidate_evidence"] == []
    assert gaps["required_gaps"][0]["candidate_status"] is None
    # The rejected requirement still shows the evidence the user
    # disowned, so the decision stays inspectable.
    assert gaps["rejected_requirements"][0]["candidate_evidence"]

    # --- 13. /match and /gaps agree on every requirement ----------------
    _assert_match_and_gaps_agree(scored, gaps)

    # --- 14. Confirming a suggested skill -------------------------------
    # NOTE: the score deliberately does NOT move. skill_match_v1 already
    # counts a `suggested` skill as satisfied (it has real evidence;
    # only the user's review is missing), so confirming changes the
    # REVIEW state, not the match. Asserted rather than glossed over —
    # a demo that promised a score bump here would be promising a bug.
    _set_status(client, token, by_name["PostgreSQL"]["id"], CandidateSkillStatus.CONFIRMED.value)
    after_confirm = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).json()
    gaps_after_confirm = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()

    assert after_confirm["overall_score"] == _EXPECTED_SCORE
    assert gaps_after_confirm["totals"]["needs_confirmation"] == 2
    assert gaps_after_confirm["totals"]["satisfied"] == 4
    assert "PostgreSQL" not in _bucket_names(gaps_after_confirm, "needs_confirmation")
    _assert_match_and_gaps_agree(after_confirm, gaps_after_confirm)

    # --- 15. Rejecting a skill DOES move the score ----------------------
    _set_status(client, token, by_name["Redis"]["id"], CandidateSkillStatus.REJECTED.value)
    after_reject = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).json()
    gaps_after_reject = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()

    assert after_reject["earned_weight"] == _EXPECTED_EARNED_AFTER_REJECT
    assert after_reject["obtainable_weight"] == _EXPECTED_OBTAINABLE
    assert after_reject["overall_score"] == _EXPECTED_SCORE_AFTER_REJECT
    assert _bucket_names(gaps_after_reject, "rejected_requirements") == ["Docker", "Redis"]
    assert "Redis" not in {row["skill_name"] for row in after_reject["matched_skills"]}
    _assert_match_and_gaps_agree(after_reject, gaps_after_reject)

    # --- 16. Editing the job reconciles requirements downstream ---------
    edit = client.patch(
        f"{_JOBS}/{job_id}",
        headers=_headers(token),
        json={"description": _JOB_DESCRIPTION_EDITED},
    )
    assert edit.status_code == 200, edit.text

    edited_requirements = client.get(
        f"{_JOBS}/{job_id}/requirements", headers=_headers(token)
    ).json()
    assert _requirement_triples(edited_requirements) == _EXPECTED_REQUIREMENTS_AFTER_EDIT
    # Desired-state reconciliation: the deleted requirement is gone, not
    # left behind as a stale row.
    assert _GENUINELY_MISSING_SKILL not in {row["skill_name"] for row in edited_requirements}

    after_edit = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).json()
    gaps_after_edit = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()

    assert after_edit["earned_weight"] == _EXPECTED_EARNED_AFTER_EDIT
    assert after_edit["obtainable_weight"] == _EXPECTED_OBTAINABLE_AFTER_EDIT
    assert after_edit["overall_score"] == _EXPECTED_SCORE_AFTER_EDIT
    # The hard requirement that was blocking the candidate is gone, so
    # required coverage is now complete except for the rejected Docker.
    assert after_edit["required_matched"] == 4
    assert after_edit["required_total"] == 5
    assert _bucket_names(gaps_after_edit, "required_gaps") == []
    assert _bucket_names(gaps_after_edit, "rejected_requirements") == ["Docker", "Redis"]
    assert _bucket_names(gaps_after_edit, "needs_confirmation") == ["Git", "Linux"]
    assert gaps_after_edit["totals"]["total_requirements"] == 8
    _assert_match_and_gaps_agree(after_edit, gaps_after_edit)

    # --- 17. Nothing above leaked the resume into the logs --------------
    # The document's own body lines, not just its filename: a log record
    # containing them would put a candidate's resume text into wherever
    # logs are shipped.
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for line in ("Languages: Python, SQL", "ada.sample@example.com", "555-0142"):
        assert line not in logged, f"resume content leaked into logs: {line!r}"


def _assert_match_and_gaps_agree(scored: dict[str, Any], gaps: dict[str, Any]) -> None:
    """The structural promise of Prompt 4.4: /match and /gaps are two
    views of ONE resolution, so they cannot contradict each other.

    A skill /match calls matched must appear in no ordinary gap bucket,
    and one it calls missing must land in exactly one bucket.
    """
    ordinary = {
        name
        for bucket in ("required_gaps", "preferred_gaps", "informational_gaps")
        for name in _bucket_names(gaps, bucket)
    }
    rejected = set(_bucket_names(gaps, "rejected_requirements"))
    needs_confirmation = set(_bucket_names(gaps, "needs_confirmation"))

    matched = {row["skill_name"] for row in scored["matched_skills"]}
    missing = {row["skill_name"] for row in scored["missing_skills"]}

    assert not (matched & ordinary), matched & ordinary
    assert not (matched & rejected), matched & rejected
    # A matched-but-unreviewed skill is the ONE overlap that is correct:
    # it satisfies the requirement and still prompts for review.
    assert needs_confirmation <= matched, needs_confirmation - matched
    # Every missing skill lands in exactly one bucket.
    for name in missing:
        landings = [name in ordinary, name in rejected]
        assert sum(landings) == 1, f"{name} landed in {sum(landings)} buckets"


# --- read-only boundary --------------------------------------------------


def _acceptance_state(
    client: TestClient, github: SampleGitHubClient, enqueued: list[str], ingestions: list[str]
) -> tuple[str, str]:
    """Set up the same fictional world as the journey test, up to the
    point where a job exists — so the guard tests below check the real
    acceptance state rather than a simplified stand-in."""
    _run(_seed_taxonomy_async())
    token, _ = _register(client)

    sample = _SAMPLES_BY_SLUG["backend-engineer"]
    upload = client.post(
        _RESUMES,
        headers=_headers(token),
        files={"file": (sample.filename, sample.build(), sample.content_type)},
    )
    extract_resume_text.delay(upload.json()["id"])

    client.put(_GITHUB, headers=_headers(token), json={"username": SAMPLE_USERNAME})
    run = client.post(f"{_GITHUB}/ingestions", headers=_headers(token))
    ingest_github_repositories.delay(run.json()["id"])

    save = client.post(
        _JOBS,
        headers=_headers(token),
        json={
            "company": _JOB_COMPANY,
            "title": _JOB_TITLE,
            "description": _JOB_DESCRIPTION,
        },
    )
    assert save.status_code == 201, save.text
    return token, str(save.json()["id"])


def test_match_and_gaps_are_read_only(
    client: TestClient,
    github: SampleGitHubClient,
    enqueued_resumes: list[str],
    enqueued_ingestions: list[str],
) -> None:
    """GET /match and GET /gaps persist NOTHING.

    Asserted on ids AND timestamps across every table the two endpoints
    read, not on row counts alone: a count-only check passes while an
    in-place UPDATE silently bumps `updated_at` on a candidate skill, and
    a score or gap quietly written to a row is exactly the cache-with-no-
    invalidation both endpoints were designed to avoid.
    """
    token, job_id = _acceptance_state(client, github, enqueued_resumes, enqueued_ingestions)

    async def _snapshot(session: AsyncSession) -> set[tuple[Any, ...]]:
        rows: set[tuple[Any, ...]] = set()
        for cs in (await session.scalars(select(CandidateSkill))).all():
            rows.add(("cs", cs.id, cs.skill_id, cs.status, cs.created_at, cs.updated_at))
        for ev in (await session.scalars(select(SkillEvidence))).all():
            rows.add(("ev", ev.id, ev.excerpt, ev.confidence, ev.created_at, ev.updated_at))
        for req in (await session.scalars(select(JobSkillRequirement))).all():
            rows.add(
                ("req", req.id, req.requirement_level, req.excerpt, req.created_at, req.updated_at)
            )
        for job in (await session.scalars(select(SavedJob))).all():
            rows.add(("job", job.id, job.description, job.created_at, job.updated_at))
        return rows

    before = _query(_snapshot)
    assert before, "nothing to compare — the fixture wrote no rows"

    for _ in range(3):
        assert client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).status_code == 200
        assert client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).status_code == 200

    assert _query(_snapshot) == before

    # And no table was invented to hold a score or a gap.
    async def _derived_tables(session: AsyncSession) -> list[str]:
        from sqlalchemy import text

        rows = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema "
                "AND (table_name LIKE '%gap%' OR table_name LIKE '%score%' "
                "OR table_name LIKE '%match%')"
            ),
            {"schema": TEST_SCHEMA},
        )
        return [row[0] for row in rows]

    assert _query(_derived_tables) == []


def test_repeated_requests_are_deterministic(
    client: TestClient,
    github: SampleGitHubClient,
    enqueued_resumes: list[str],
    enqueued_ingestions: list[str],
) -> None:
    """Two identical requests return byte-identical JSON.

    A demo that shows a different number on a refresh is not
    explainable, whatever the number is.
    """
    token, job_id = _acceptance_state(client, github, enqueued_resumes, enqueued_ingestions)

    for path in ("match", "gaps", "requirements"):
        first = client.get(f"{_JOBS}/{job_id}/{path}", headers=_headers(token))
        second = client.get(f"{_JOBS}/{job_id}/{path}", headers=_headers(token))
        assert first.status_code == 200
        assert first.content == second.content, path


# --- security / ownership ------------------------------------------------


def test_another_user_cannot_reach_the_job_its_match_or_its_gaps(
    client: TestClient,
    github: SampleGitHubClient,
    enqueued_resumes: list[str],
    enqueued_ingestions: list[str],
) -> None:
    """The cross-user check the acceptance package needs.

    403, not 404: saved-job ids are random UUIDs rather than enumerable
    integers, so naming the resource is not a guessing surface — and the
    honest status is what the rest of the API already returns.
    """
    _, job_id = _acceptance_state(client, github, enqueued_resumes, enqueued_ingestions)
    intruder_token, _ = _register(client)

    for path in ("", "/requirements", "/match", "/gaps"):
        response = client.get(f"{_JOBS}/{job_id}{path}", headers=_headers(intruder_token))
        assert response.status_code == 403, f"{path}: {response.status_code}"

    # The intruder's own view stays empty — nothing leaked sideways.
    assert client.get(_JOBS, headers=_headers(intruder_token)).json() == []
    assert client.get(_SKILLS, headers=_headers(intruder_token)).json() == []


def test_unauthenticated_requests_are_rejected(
    client: TestClient,
    github: SampleGitHubClient,
    enqueued_resumes: list[str],
    enqueued_ingestions: list[str],
) -> None:
    _, job_id = _acceptance_state(client, github, enqueued_resumes, enqueued_ingestions)

    for path in ("", "/requirements", "/match", "/gaps"):
        assert client.get(f"{_JOBS}/{job_id}{path}").status_code == 401, path
    for path in (_RESUMES, _SKILLS, _JOBS, _GITHUB):
        assert client.get(path).status_code == 401, path


def test_no_user_id_spoofing_path_exists(client: TestClient) -> None:
    """Ownership is structural: `user_id` comes only from the JWT.

    `extra="forbid"` on the request models means a client that tries to
    name an owner gets a 422 rather than having the field silently
    dropped — the difference between "rejected" and "ignored" matters,
    because "ignored" is one refactor away from "honoured".
    """
    _run(_seed_taxonomy_async())
    token, user_id = _register(client)
    victim_token, victim_id = _register(client)

    spoofed = client.post(
        _JOBS,
        headers=_headers(token),
        json={
            "company": _JOB_COMPANY,
            "title": _JOB_TITLE,
            "description": _JOB_DESCRIPTION,
            "user_id": victim_id,
        },
    )
    assert spoofed.status_code == 422, spoofed.text

    # A job saved normally belongs to the caller, and a query parameter
    # naming someone else changes nothing.
    mine = client.post(
        _JOBS,
        headers=_headers(token),
        json={
            "company": _JOB_COMPANY,
            "title": _JOB_TITLE,
            "description": _JOB_DESCRIPTION,
        },
    )
    assert mine.status_code == 201
    assert client.get(f"{_JOBS}?user_id={victim_id}", headers=_headers(victim_token)).json() == []
