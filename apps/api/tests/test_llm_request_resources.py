"""What a request may hold, and what may not break it (Prompt 7.1b).

Two guarantees about the OPTIONAL layer's cost to everything else.

H1 — A REQUEST WAITING ON A MODEL HOLDS NO DATABASE CONNECTION.
`get_db` yields one session per request and a session keeps its
connection checked out from its first query until it is closed, so
`/explanation` and `/roadmap` were holding a pooled connection across a
wait that can reach `explanation_timeout_seconds` (180). The engine's
default pool is 5 + 10 overflow, so fifteen concurrent generations would
have held every connection and made `/match`, `/gaps` and every other
endpoint queue behind `pool_timeout` and fail — the optional AI layer
taking down the deterministic product, which is the one thing this
architecture exists to prevent.

The tests below prove it BEHAVIOURALLY rather than by inspecting
session state: a pool of exactly ONE connection, a provider parked
inside `complete()`, and an ordinary database-backed request that has to
succeed while it is parked. If the connection were still checked out
that request could not be served at all, so the assertion fails for the
right reason rather than by reading an implementation detail.

M3 — A PROVIDER THAT CANNOT BE BUILT IS A REJECTED NARRATIVE, NOT A 500.
`get_explanation_provider` and `get_roadmap_provider` raise on an
unknown `EXPLANATION_PROVIDER`, which is correct and is NOT relaxed —
tests/test_explanation_adapter.py and tests/test_llm_provider_config.py
still pin that, and this file does not restate it. What changed is where
the raise lands: it happened inside the request handler, so one typo in
one environment variable destroyed a score, a gap list and a whole
learning plan that were computed without a model and never needed one.

These tests drive the REAL factory with a REAL bad setting rather than a
stub that raises, because the failure being fixed is a misconfiguration
and a stub would not prove the misconfiguration path reaches the
handler.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_db
from app.explanation.prompt import ExplanationRequest
from app.explanation.provider import MockExplanationProvider
from app.explanation.runtime import UNAVAILABLE_PROVIDER_NAME
from app.explanation.validate import RejectionReason
from app.main import app
from app.rate_limit import _request_log
from app.roadmap.provider import MockRoadmapProvider
from app.settings import get_settings
from tests.conftest import _SEARCH_PATH_CONNECT_ARGS, isolated_schema_override
from tests.test_roadmap_api import _create_job, _give_skill, _new_user, _seed_taxonomy

_JOBS = "/api/v1/saved-jobs"
_ROADMAP = "/api/v1/roadmap"

_JOB = "We need AWS and Docker. Python is required. Kubernetes preferred."

# How long a connection checkout may wait before the pool gives up. Short
# on purpose: if the fix regresses, the concurrent request below fails in
# seconds with a pool timeout instead of hanging the suite.
_POOL_TIMEOUT = 5.0

# Every await in these tests is bounded, so a regression is a failure
# rather than a hang.
_STEP_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


@pytest.fixture
def anyio_backend() -> str:
    """asyncio only — the same single backend the other async test
    modules in this suite declare."""
    return "asyncio"


@pytest.fixture
def prepared(client: TestClient) -> tuple[str, str]:
    """A candidate with a saved job, built SYNCHRONOUSLY.

    Deliberately a sync fixture rather than setup inside the async test:
    the seeding helpers call `asyncio.run` internally, which cannot start
    inside an already-running event loop — the same constraint
    tests/test_extraction.py documents. Running it here means the async
    test body starts with the world already built and touches only the
    one engine it means to measure.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested")
    return token, _create_job(client, token, _JOB)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- H1: the pool is free while the provider generates -----------------


class _ParkedProvider:
    """A provider that stops inside `complete()` until it is released.

    Stands in for a local model mid-generation: the request is alive,
    the handler is awaiting, and the question is what it is holding
    while it waits.

    It returns the MOCK'S OWN OUTPUT once released, so the request ends
    in the ordinary `generated` state and the deterministic half of the
    response can be compared against a normal run. A provider that
    failed would prove the connection was released and nothing about
    the answer surviving.
    """

    def __init__(self, render: Any) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._render = render

    @property
    def name(self) -> str:
        return "mock"

    async def complete(self, request: ExplanationRequest) -> str:
        self.entered.set()
        await self.release.wait()
        return str(self._render(request))


def _single_connection_engine() -> Any:
    """An engine whose pool holds exactly ONE connection.

    `max_overflow=0` is what makes the test mean something: with any
    overflow at all a second request would simply open another
    connection and the assertion would pass whether or not the first one
    let go.

    NOT NullPool, which the rest of the suite uses. NullPool opens a
    connection per session and would make contention impossible to
    observe — the very thing being measured here.
    """
    return create_async_engine(
        get_settings().database_url,
        connect_args=_SEARCH_PATH_CONNECT_ARGS,
        pool_size=1,
        max_overflow=0,
        pool_timeout=_POOL_TIMEOUT,
    )


def _override_with(factory: async_sessionmaker[Any]) -> None:
    async def _get_db() -> AsyncGenerator[Any, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db


async def _assert_pool_is_free_during_generation(
    *, token: str, url: str, provider: _ParkedProvider, baseline: dict[str, Any]
) -> dict[str, Any]:
    """Park a generation, then prove an ordinary request still works.

    Returns the parked request's own payload so the caller can check the
    deterministic half survived the release.
    """
    engine = _single_connection_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _override_with(factory)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
            parked = asyncio.create_task(ac.get(url, headers=_headers(token), timeout=None))

            # The handler is now inside the provider, which is exactly
            # where the old code was still holding the connection.
            await asyncio.wait_for(provider.entered.wait(), timeout=_STEP_TIMEOUT)

            # THE PROOF. One connection exists, and this request needs
            # it. Before the fix it would wait `pool_timeout` and then
            # fail, because the parked request had not let go.
            ordinary = await asyncio.wait_for(
                ac.get(_JOBS, headers=_headers(token), timeout=None), timeout=_STEP_TIMEOUT
            )
            assert ordinary.status_code == 200, ordinary.text
            assert len(ordinary.json()) == 1

            # The same statement read directly off the pool. Secondary
            # to the request above — which is the behaviour a user
            # actually has — but it says WHY that request succeeded, so
            # a regression reports the cause rather than a symptom.
            assert engine.pool.checkedout() == 0

            provider.release.set()
            response = await asyncio.wait_for(parked, timeout=_STEP_TIMEOUT)
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    assert response.status_code == 200, response.text
    payload = dict(response.json())
    # AND THE ANSWER SURVIVED THE RELEASE. Releasing the session early is
    # only safe if every value the response still needs was materialised
    # first; comparing against a run that never released proves it was.
    assert payload == baseline
    return payload


@pytest.mark.anyio
async def test_an_explanation_holds_no_connection_while_the_model_writes(
    prepared: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, job_id = prepared
    url = f"{_JOBS}/{job_id}/explanation"
    baseline = dict(client.get(url, headers=_headers(token)).json())
    assert baseline["status"] == "generated"

    provider = _ParkedProvider(MockExplanationProvider().complete_sync)
    monkeypatch.setattr(
        "app.api.v1.saved_job.get_explanation_provider", lambda *args, **kwargs: provider
    )

    payload = await _assert_pool_is_free_during_generation(
        token=token, url=url, provider=provider, baseline=baseline
    )
    assert payload["status"] == "generated"
    assert payload["overall_score"] == baseline["overall_score"]


@pytest.mark.anyio
async def test_a_roadmap_holds_no_connection_while_the_model_writes(
    prepared: tuple[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The longer of the two waits: a full narrative is the biggest
    generation this product asks for."""
    token, _ = prepared
    url = f"{_ROADMAP}?duration_days=28&hours_per_day=1"
    baseline = dict(client.get(url, headers=_headers(token)).json())
    assert baseline["narrative_status"] == "generated"

    provider = _ParkedProvider(MockRoadmapProvider().complete_sync)
    monkeypatch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda *args, **kwargs: provider)

    payload = await _assert_pool_is_free_during_generation(
        token=token, url=url, provider=provider, baseline=baseline
    )
    assert payload["narrative_status"] == "generated"
    assert payload["weeks"] == baseline["weeks"]


# --- M3: a provider that cannot be built ------------------------------


def _misconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `EXPLANATION_PROVIDER` at a name no factory knows.

    The REAL misconfiguration, not a stub that raises: the failure being
    fixed is an operator typo, and the value of this test is that the
    typo travels all the way from the environment through the real
    factory into the handler.

    Overrides conftest's own pin to "mock" for this test only; that
    fixture clears the settings cache again on teardown.
    """
    monkeypatch.setenv("EXPLANATION_PROVIDER", "gpt-4-turbo-preview")
    get_settings.cache_clear()


def test_a_provider_that_cannot_be_built_still_explains_the_match(
    client: TestClient, prepared: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 500 here would cost the user a score and a gap list that were
    computed without a model and never needed one."""
    token, job_id = prepared
    url = f"{_JOBS}/{job_id}/explanation"
    good = dict(client.get(url, headers=_headers(token)).json())

    _misconfigure(monkeypatch)
    response = client.get(url, headers=_headers(token))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "rejected"
    assert payload["reason"] == RejectionReason.PROVIDER_ERROR.value
    assert payload["provider"] == UNAVAILABLE_PROVIDER_NAME
    # NOTHING generated, and every deterministic field intact.
    assert payload["summary"] is None
    assert payload["strengths"] == []
    assert payload["gaps"] == []
    assert payload["next_steps"] == []
    assert payload["cited_evidence"] == []
    assert payload["overall_score"] == good["overall_score"]
    assert payload["has_requirements"] == good["has_requirements"]
    assert payload["match_formula_version"] == good["match_formula_version"]
    assert payload["gap_formula_version"] == good["gap_formula_version"]
    assert payload["semantic_formula_version"] == good["semantic_formula_version"]

    # And the endpoints that never wanted a model are untouched.
    assert client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).status_code == 200
    assert client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).status_code == 200


def test_a_provider_that_cannot_be_built_still_returns_the_whole_plan(
    client: TestClient, prepared: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan is decided before a provider is reached for, so a
    construction failure costs the wording and nothing else."""
    token, _ = prepared
    url = f"{_ROADMAP}?duration_days=28&hours_per_day=1"
    good = dict(client.get(url, headers=_headers(token)).json())

    _misconfigure(monkeypatch)
    response = client.get(url, headers=_headers(token))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["narrative_status"] == "rejected"
    assert payload["reason"] == RejectionReason.PROVIDER_ERROR.value
    assert payload["provider"] == UNAVAILABLE_PROVIDER_NAME
    assert payload["overview"] is None

    # THE WHOLE DETERMINISTIC PLAN, item for item and day for day.
    assert payload["scheduled_days"] == good["scheduled_days"]
    assert payload["unscheduled_days"] == good["unscheduled_days"]
    assert payload["coverage"] == good["coverage"]
    assert [week["week"] for week in payload["weeks"]] == [week["week"] for week in good["weeks"]]
    for week, expected in zip(payload["weeks"], good["weeks"], strict=True):
        assert [item["item_id"] for item in week["items"]] == [
            item["item_id"] for item in expected["items"]
        ]
        for item, was in zip(week["items"], expected["items"], strict=True):
            assert item["why"] == was["why"]
            assert item["score"] == was["score"]
            assert item["start_day"] == was["start_day"]
            assert item["end_day"] == was["end_day"]
            assert item["affected_jobs"] == was["affected_jobs"]
            assert [step["step_id"] for step in item["steps"]] == [
                step["step_id"] for step in was["steps"]
            ]
            assert [step["phase"] for step in item["steps"]] == [
                step["phase"] for step in was["steps"]
            ]
            # Only the WORDING is gone.
            assert item["task"] is None
            assert item["outcome"] is None


def test_an_empty_roadmap_survives_a_provider_that_cannot_be_built(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-saved-jobs branch names the provider before anything else
    happens, so it is the one place construction is reached earliest."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _misconfigure(monkeypatch)

    response = client.get(_ROADMAP, headers=_headers(token))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_selected_jobs"] is False
    # Still the statement about the INPUT, not about the provider: the
    # user has saved nothing, and that is the more useful thing to say.
    assert payload["reason"] == "no_selected_jobs"
    assert payload["provider"] == UNAVAILABLE_PROVIDER_NAME


def test_the_construction_failure_is_logged_for_the_operator(
    client: TestClient,
    prepared: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A misconfiguration nobody can see is a misconfiguration nobody
    fixes. The rejected response is deliberately generic; the LOG is
    where the bad value and the valid ones are named."""
    token, job_id = prepared
    _misconfigure(monkeypatch)

    with caplog.at_level("ERROR"):
        client.get(f"{_JOBS}/{job_id}/explanation", headers=_headers(token))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "gpt-4-turbo-preview" in logged
    assert "could not be constructed" in logged


def test_a_working_provider_is_unaffected(client: TestClient, prepared: tuple[str, str]) -> None:
    """The guard on all of the above: the ordinary path still generates.

    Without this, every M3 assertion here would still pass on an
    implementation that had quietly stopped calling a provider at all.
    """
    token, job_id = prepared

    explanation = client.get(f"{_JOBS}/{job_id}/explanation", headers=_headers(token)).json()
    roadmap = client.get(_ROADMAP, headers=_headers(token)).json()

    assert explanation["status"] == "generated"
    assert explanation["provider"] == "mock"
    assert explanation["summary"]
    assert roadmap["narrative_status"] == "generated"
    assert roadmap["provider"] == "mock"
    assert roadmap["overview"]


def test_a_misconfigured_provider_is_never_swapped_for_a_working_one(
    client: TestClient, prepared: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOT A FALLBACK. `None` means there is no provider — it does not
    mean "use the mock instead". Serving templated placeholder prose as
    though a model had written it is the one failure this feature
    cannot afford, and a construction failure must not become a back
    door to it.
    """
    token, job_id = prepared
    _misconfigure(monkeypatch)

    payload = client.get(f"{_JOBS}/{job_id}/explanation", headers=_headers(token)).json()

    assert payload["provider"] != "mock"
    assert payload["status"] == "rejected"
    # The mock's own first sentence, which must appear nowhere.
    assert payload["summary"] is None
    assert not payload["strengths"]


def test_a_nonexistent_job_is_still_a_404_when_the_provider_is_broken(
    client: TestClient, prepared: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership and existence are decided before any of this, and a
    broken provider must not turn a 404 into a 200 carrying an empty
    deterministic result."""
    token, _ = prepared
    _misconfigure(monkeypatch)

    response = client.get(f"{_JOBS}/{uuid.uuid4()}/explanation", headers=_headers(token))

    assert response.status_code == 404
