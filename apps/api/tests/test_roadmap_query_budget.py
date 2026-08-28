"""What one roadmap request costs the database (Prompt 7.1d, audit H2).

THIS FILE MEASURES. IT DOES NOT OPTIMISE, and the code it measures was
deliberately left alone.

WHAT WAS FOUND. `/roadmap` loops the selected jobs calling
`read_saved_job_match` and `read_saved_job_gaps` — the same handlers a
client calls, which is the property that stops the plan drifting from
what the user sees on each job. Each of those independently loads the
job's requirements, the skills behind them, the caller's candidate
skills and their evidence, so roughly half the per-job reads are the
same four queries run twice.

Measured against the real route, five saved jobs with identical
descriptions so each one costs the same:

    top_n = 1    13 SELECTs
    top_n = 2    21          (+8)
    top_n = 3    29          (+8)
    top_n = 4    37          (+8)
    top_n = 5    45          (+8)

    queries = FIXED + PER_JOB x top_n   =   5 + 8 x top_n

The FIXED five are the authenticated user, the saved-job list,
`load_evidence_for`'s two, and `load_taxonomy_names`. The PER_JOB eight
are match's four and gaps' four; `_get_owned_job`'s `db.get` costs
nothing inside the loop because the jobs are already in the session's
identity map from the initial list query.

GROWTH IS LINEAR, which is the finding that matters. The duplication is
a constant factor, not a compounding one, so it does not become a
different problem at scale — twenty saved jobs is 165 indexed lookups on
small tables inside a request whose dominant cost is a local model
generating for a minute or more. Halving it would mean the roadmap
computing match and gaps ITSELF instead of calling the handlers, trading
a documented consistency guarantee for a few milliseconds of a
ninety-second request. Not worth it, and this file is the evidence for
that judgement rather than a promise to revisit it.

WHY A CEILING AND NOT AN EQUALITY. `<=` lets a future optimisation land
without editing this file, while any regression — a new query in the
per-job path, or an N+1 introduced inside one of the handlers — pushes
the total past the bound and fails. The LINEARITY check below is
asserted exactly, because that is the property being protected: a change
that made growth quadratic could still sit under a generous ceiling at
top_n=5 and be catastrophic at fifty.
"""

from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_db
from app.main import app
from app.rate_limit import _request_log
from app.settings import get_settings
from tests.conftest import _SEARCH_PATH_CONNECT_ARGS, isolated_schema_override
from tests.test_roadmap_api import (
    _create_job,
    _give_skill,
    _headers,
    _new_user,
    _seed_taxonomy,
)

_ROADMAP = "/api/v1/roadmap"

# IDENTICAL FOR EVERY JOB, on purpose. This is a measurement, so the
# per-job delta has to be the per-job COST and not an artefact of one
# posting mentioning more skills than another.
_JOB = "We need AWS and Docker. Python is required. Kubernetes preferred. Redis is required."

# The measured budget. Both numbers are facts about the current
# implementation, recorded here so a change to either is a deliberate
# edit rather than a silent drift.
FIXED_QUERIES = 5
PER_JOB_QUERIES = 8

_SAVED_JOBS = 5


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


class _Counter:
    """SELECTs issued by the engine the route actually used.

    COUNTED THROUGH THE REAL `get_db` OVERRIDE, not by replaying the
    query shape on a second engine the way tests/test_job_match_api.py
    does. A replay can only confirm what its author already believed the
    handler does; the whole point of H2 was to find out what the loop
    really costs, so the listener goes on the engine the request runs
    against.

    Only SELECTs: the route is read-only, and counting the transaction
    control statements would make the number depend on pool behaviour
    rather than on the handler.
    """

    def __init__(self) -> None:
        self.engine = create_async_engine(
            get_settings().database_url,
            connect_args=_SEARCH_PATH_CONNECT_ARGS,
            poolclass=NullPool,
        )
        self.count = 0

        @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
        def _count(
            conn: Any,
            cursor: Any,
            statement: str,
            params: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                self.count += 1

        factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async def _get_db() -> AsyncGenerator[AsyncSession, None]:
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _get_db

    def measure(self, client: TestClient, token: str, top_n: int) -> int:
        self.count = 0
        response = client.get(
            _ROADMAP,
            headers=_headers(token),
            params={"top_n": top_n, "duration_days": 28, "hours_per_day": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["selected_job_count"] == top_n
        return self.count


def test_the_roadmaps_query_cost_is_linear_and_within_budget(client: TestClient) -> None:
    """The H2 measurement, as an assertion.

    Three points rather than two, because two can only ever draw a
    straight line: with 1, 3 and 5 the two gaps are independent
    measurements of the same slope, and a superlinear cost separates
    them.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    # One reviewed skill and one unreviewed one, so the per-job path
    # loads evidence rather than short-circuiting on an empty candidate
    # set — which would measure a cheaper route than a real user's.
    _give_skill(user_id, "Python", "suggested")
    _give_skill(user_id, "Docker", "confirmed")
    for index in range(_SAVED_JOBS):
        _create_job(client, token, _JOB, company=f"Company {index}")

    counter = _Counter()
    try:
        measured = {top_n: counter.measure(client, token, top_n) for top_n in (1, 3, 5)}
    finally:
        app.dependency_overrides.pop(get_db, None)

    # --- the budget ---------------------------------------------------
    # A CEILING, so an optimisation can land without touching this file.
    for top_n, queries in measured.items():
        budget = FIXED_QUERIES + PER_JOB_QUERIES * top_n
        assert queries <= budget, (
            f"top_n={top_n} issued {queries} SELECTs against a budget of {budget} "
            f"({FIXED_QUERIES} + {PER_JOB_QUERIES} x {top_n}). Something in the "
            f"per-job path grew; see this module's docstring for the breakdown."
        )

    # --- the property being protected ---------------------------------
    # EXACT, not a ceiling. Two additional jobs must cost the same as the
    # two before them. A change that made this compound could still sit
    # under the budget at five jobs and be ruinous at fifty, which is
    # precisely the failure a total-only check cannot see.
    first_gap = measured[3] - measured[1]
    second_gap = measured[5] - measured[3]
    assert first_gap == second_gap, (
        f"query growth is not linear: jobs 2-3 cost {first_gap} SELECTs and "
        f"jobs 4-5 cost {second_gap}. Measured totals: {measured}."
    )
    # And the slope is the one the budget was written for.
    assert first_gap == 2 * PER_JOB_QUERIES, (
        f"the per-job cost moved: {first_gap / 2} SELECTs per job against a "
        f"recorded {PER_JOB_QUERIES}. Measured totals: {measured}."
    )


def test_a_roadmap_over_no_jobs_costs_almost_nothing(client: TestClient) -> None:
    """The floor of the same measurement.

    Worth pinning separately: the empty case returns before the loop, so
    it must not pay the per-job cost at all. If it ever did, the reason
    would be work happening before the early return — which is exactly
    where provider construction sits (Prompt 7.1b).
    """
    _seed_taxonomy()
    token, _ = _new_user(client)

    counter = _Counter()
    try:
        counter.count = 0
        response = client.get(_ROADMAP, headers=_headers(token))
        assert response.status_code == 200, response.text
        assert response.json()["has_selected_jobs"] is False
        empty = counter.count
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert empty <= FIXED_QUERIES, (
        f"an empty roadmap issued {empty} SELECTs, above the {FIXED_QUERIES} "
        f"a request that selects no jobs should need."
    )
