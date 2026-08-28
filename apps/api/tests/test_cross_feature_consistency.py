"""A change over here must not move something over there (Prompt 7.1d).

Four cases the per-slice suites cannot see, because each of them lives in
the gap BETWEEN two features:

  1. DELETING A RESUME reaches `/match`, `/gaps`, `/semantic`,
     `/explanation` and `/roadmap` at once.
     tests/test_resume_deletion_evidence.py proves the deletion itself is
     correct — which evidence goes, which candidate skills survive — and
     stops at the skill profile. Nothing re-read the five views that are
     DERIVED from those rows, so "the score, the gaps and the plan still
     agree with each other afterwards" was asserted nowhere.

  2. REORDERING JOBS must leave every match and gap byte-identical.
     tests/test_saved_job_order_api.py proves the reorder endpoint;
     tests/test_roadmap_api.py proves the roadmap reads the order. The
     claim in between — that `skill_match_v1` cannot notice a reorder at
     all — had no test, and it is the one a reviewer would doubt, since
     the roadmap demonstrably changes.

  3. ADDING A JOB must leave the jobs already saved untouched.

  4. DELETING A SELECTED JOB must leave no trace in the recomputed plan.

WHAT THESE ARE NOT. None of them re-checks a formula: the arithmetic of
`skill_match_v1`, `skill_gap_v1` and `roadmap_priority_v1` is pinned in
their own suites and is not restated here. These assert INVARIANCE and
CONSISTENCY across a mutation — a different question, and the only one
that can catch two correct features disagreeing.

The provider stays the deterministic mock throughout (pinned for the
whole suite by tests/conftest.py), so a narrative never varies between
the two halves of a comparison.
"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.rate_limit import _request_log
from app.roadmap.priority import JOB_RANK_WEIGHTS
from app.storage import get_resume_storage
from app.storage.local import LocalResumeStorage
from tests.conftest import isolated_schema_override
from tests.test_resume_deletion_evidence import (
    _attach_evidence,
    _upload,
)
from tests.test_resume_deletion_evidence import (
    _delete as _delete_resume,
)
from tests.test_roadmap_api import (
    _create_job,
    _give_skill,
    _headers,
    _items,
    _new_user,
    _roadmap,
    _seed_taxonomy,
)

_JOBS = "/api/v1/saved-jobs"

# Python is REQUIRED and is the skill the deleted resume will have been
# the only evidence for; Docker is required and confirmed, so it must
# survive the same deletion untouched.
_PYTHON_JOB = "Python is required. Docker is required. Kubernetes preferred."
_SECOND_JOB = "Docker is required. Redis is required. AWS preferred."
_THIRD_JOB = "AWS is required. PostgreSQL preferred."


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _use_temp_storage(tmp_path: Path) -> Generator[None, None, None]:
    """Resume uploads land in a temp directory, never the dev store.

    Redeclared rather than imported: an autouse fixture applies to the
    module that defines it, so tests/test_resume_deletion_evidence.py's
    copy does not reach here. The plain helpers ARE imported, which is
    the half worth sharing.
    """
    app.dependency_overrides[get_resume_storage] = lambda: LocalResumeStorage(tmp_path)
    yield
    app.dependency_overrides.pop(get_resume_storage, None)


@pytest.fixture(autouse=True)
def _stub_enqueue_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence is written directly rather than by the worker: these
    tests are about what happens AFTER the rows exist, and stubbing keeps
    them off a real broker."""
    monkeypatch.setattr("app.api.v1.resume.enqueue_extraction", lambda resume_id: None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


def _get(client: TestClient, token: str, job_id: str, suffix: str) -> dict[str, Any]:
    response = client.get(f"{_JOBS}/{job_id}/{suffix}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _bucket(gaps: dict[str, Any], name: str) -> set[str]:
    return {entry["skill_name"] for entry in gaps[name]}


def _assert_match_and_gaps_agree(match: dict[str, Any], gaps: dict[str, Any]) -> None:
    """The structural promise of `/match` and `/gaps`: two views of ONE
    resolution, so they cannot contradict each other.

    The same check tests/test_mvp_acceptance.py makes at a single point
    in time. What is new here is running it on BOTH sides of a mutation,
    which is where two correct features drift apart.
    """
    ordinary = (
        _bucket(gaps, "required_gaps")
        | _bucket(gaps, "preferred_gaps")
        | _bucket(gaps, "informational_gaps")
    )
    rejected = _bucket(gaps, "rejected_requirements")
    needs_confirmation = _bucket(gaps, "needs_confirmation")

    matched = {row["skill_name"] for row in match["matched_skills"]}
    missing = {row["skill_name"] for row in match["missing_skills"]}

    assert not (matched & ordinary), matched & ordinary
    assert not (matched & rejected), matched & rejected
    # The one correct overlap: a matched-but-unreviewed skill satisfies
    # the requirement and still prompts for review.
    assert needs_confirmation <= matched, needs_confirmation - matched
    for name in missing:
        assert sum([name in ordinary, name in rejected]) == 1, name


def _state_of(payload: dict[str, Any], skill_name: str) -> str | None:
    """The roadmap state of one skill, or None if it is not in the plan."""
    for item in _items(payload):
        if item["skill_name"] == skill_name:
            return str(item["state"])
    return None


def _item_named(payload: dict[str, Any], skill_name: str) -> dict[str, Any]:
    return next(item for item in _items(payload) if item["skill_name"] == skill_name)


def _affected_job_ids(payload: dict[str, Any]) -> set[str]:
    return {job["saved_job_id"] for item in _items(payload) for job in item["affected_jobs"]}


# --- 1. deleting a resume ---------------------------------------------


def test_deleting_a_resume_keeps_every_view_consistent(client: TestClient) -> None:
    """THE REAL USER FLOW, end to end: a candidate removes the document
    an extractor learned a skill from, and every derived view has to
    agree about what they now have.

    Two skills, chosen to exercise both halves of the deletion rule:

      Python    `suggested`, evidence ONLY from this resume. The skill is
                orphaned by the delete and disappears, so a job that
                requires it goes from matched to missing.
      Docker    `confirmed` by the user, evidence only from this resume.
                The evidence goes; the skill stays. This is the audit's
                "confirmed skill with no surviving evidence" case, and
                the interesting thing is that it must STILL MATCH — the
                user asserted it, and deleting a document does not
                un-assert it.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)

    for skill, status in (("Python", "suggested"), ("Docker", "confirmed")):
        _attach_evidence(
            user_id,
            skill,
            source_type="resume",
            source_identifier=resume_id,
            extraction_method="resume_alias_match",
            status=status,
            excerpt=f"Worked with {skill}",
        )
    job_id = _create_job(client, token, _PYTHON_JOB)

    before_match = _get(client, token, job_id, "match")
    before_gaps = _get(client, token, job_id, "gaps")
    before_roadmap = _roadmap(client, token)
    _assert_match_and_gaps_agree(before_match, before_gaps)

    # Both skills satisfy their requirement to begin with.
    assert {"Python", "Docker"} <= {row["skill_name"] for row in before_match["matched_skills"]}
    # Python is `suggested`, so it satisfies the requirement AND still
    # asks to be reviewed — which `roadmap_priority_v1` calls weak
    # evidence. Docker was confirmed, so it asks for nothing.
    assert "Python" in _bucket(before_gaps, "needs_confirmation")
    assert _state_of(before_roadmap, "Python") == "weak_evidence"
    assert "Docker" not in {item["skill_name"] for item in _items(before_roadmap)}

    assert _delete_resume(client, token, resume_id).status_code == 204

    after_match = _get(client, token, job_id, "match")
    after_gaps = _get(client, token, job_id, "gaps")
    after_semantic = _get(client, token, job_id, "semantic")
    after_explanation = _get(client, token, job_id, "explanation")
    after_roadmap = _roadmap(client, token)

    # THE INVARIANT, on the far side of the deletion.
    _assert_match_and_gaps_agree(after_match, after_gaps)

    # Python lost its only evidence and its skill with it.
    assert "Python" in {row["skill_name"] for row in after_match["missing_skills"]}
    assert "Python" in _bucket(after_gaps, "required_gaps")
    # Docker was the user's own assertion and survives, evidence or not.
    assert "Docker" in {row["skill_name"] for row in after_match["matched_skills"]}
    docker = next(row for row in after_match["matched_skills"] if row["skill_name"] == "Docker")
    assert docker["candidate_evidence"] == []
    assert docker["candidate_status"] == "confirmed"

    # The score fell, and every other view echoes the SAME number.
    assert after_match["overall_score"] < before_match["overall_score"]
    assert after_explanation["overall_score"] == after_match["overall_score"]
    assert after_explanation["status"] in {"generated", "rejected"}

    # Semantic still answers, and cites nothing that was deleted.
    assert after_semantic["evidence"] == []

    # THE TRANSITION THE WHOLE CHAIN HAS TO AGREE ON. Python was a skill
    # the candidate had evidence for but had not reviewed; it is now one
    # they do not have at all. `weak_evidence` asks for evidence,
    # `missing_required` asks them to go and learn it — very different
    # advice, and it followed from deleting one document.
    assert _state_of(before_roadmap, "Python") == "weak_evidence"
    assert _state_of(after_roadmap, "Python") == "missing_required"
    python_item = _item_named(after_roadmap, "Python")
    assert python_item["state_weight"] > _item_named(before_roadmap, "Python")["state_weight"]
    # Docker stays out of the plan: confirmed is confirmed, and losing
    # the document it came from is not a reason to go and learn it again.
    assert "Docker" not in {item["skill_name"] for item in _items(after_roadmap)}


# --- 2. reordering jobs ------------------------------------------------


def test_reordering_jobs_leaves_match_and_gaps_byte_identical(client: TestClient) -> None:
    """`skill_match_v1` CANNOT NOTICE A REORDER. The roadmap demonstrably
    can — it reads the order as its priority signal — which is exactly
    why the other half needs asserting rather than assuming.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "confirmed")
    first = _create_job(client, token, _PYTHON_JOB, company="Acme")
    second = _create_job(client, token, _SECOND_JOB, company="Globex")
    third = _create_job(client, token, _THIRD_JOB, company="Initech")

    before = {
        job_id: (_get(client, token, job_id, "match"), _get(client, token, job_id, "gaps"))
        for job_id in (first, second, third)
    }
    before_roadmap = _roadmap(client, token)

    reordered = client.put(
        f"{_JOBS}/order",
        headers=_headers(token),
        json={"job_ids": [third, first, second]},
    )
    assert reordered.status_code == 200, reordered.text
    assert [job["id"] for job in reordered.json()] == [third, first, second]

    for job_id, (match, gaps) in before.items():
        assert _get(client, token, job_id, "match") == match, job_id
        assert _get(client, token, job_id, "gaps") == gaps, job_id

    # The roadmap, on the other hand, is SUPPOSED to move: the ranks it
    # reports are the positions the user just chose.
    after_roadmap = _roadmap(client, token)
    ranks = {
        job["saved_job_id"]: job["priority_rank"]
        for item in _items(after_roadmap)
        for job in item["affected_jobs"]
    }
    assert ranks[third] == 1
    assert ranks[first] == 2
    assert ranks[second] == 3
    # The same jobs are still in play — only their order changed.
    assert _affected_job_ids(after_roadmap) == _affected_job_ids(before_roadmap)


# --- 3. adding a job ---------------------------------------------------


def test_adding_a_job_leaves_existing_results_unchanged(client: TestClient) -> None:
    """A new job is a new question, not a revision of the old answers."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "confirmed")
    first = _create_job(client, token, _PYTHON_JOB, company="Acme")

    before_match = _get(client, token, first, "match")
    before_gaps = _get(client, token, first, "gaps")
    before_roadmap = _roadmap(client, token)
    kubernetes_before = _item_named(before_roadmap, "Kubernetes")

    second = _create_job(client, token, _SECOND_JOB, company="Globex")

    # THE INVARIANT. The existing job's answers are the same bytes: what
    # the candidate matches has nothing to do with how many postings they
    # have saved.
    assert _get(client, token, first, "match") == before_match
    assert _get(client, token, first, "gaps") == before_gaps

    after_roadmap = _roadmap(client, token)
    assert after_roadmap["saved_job_count"] == 2
    assert _affected_job_ids(after_roadmap) == {first, second}
    # The new job brought its own demands with it.
    assert {"Redis", "AWS"} <= {item["skill_name"] for item in _items(after_roadmap)}

    # AND THE ROADMAP CHANGED FOR EXACTLY ONE REASON IT CAN NAME.
    # A new job lands at the TOP of the user's list (app/api/v1/
    # saved_job.py), so the job that was rank 1 is now rank 2 — and
    # `roadmap_priority_v1` weights recurrence by rank. Kubernetes is
    # still preferred by the same single job; only that job's rank moved,
    # so the state weight is untouched and the recurrence drops from
    # `job_weight(1)` to `job_weight(2)`.
    kubernetes_after = _item_named(after_roadmap, "Kubernetes")
    assert kubernetes_after["state"] == kubernetes_before["state"]
    assert kubernetes_after["state_weight"] == kubernetes_before["state_weight"]
    assert kubernetes_before["recurrence"] == JOB_RANK_WEIGHTS[0]
    assert kubernetes_after["recurrence"] == JOB_RANK_WEIGHTS[1]
    assert kubernetes_after["score"] == (
        kubernetes_after["state_weight"] + kubernetes_after["recurrence"]
    )
    # It is still the same single job asking for it.
    assert [job["saved_job_id"] for job in kubernetes_after["affected_jobs"]] == [first]


# --- 4. deleting a selected job ----------------------------------------


def test_deleting_a_selected_job_recomputes_the_roadmap(client: TestClient) -> None:
    """A deleted job leaves NO trace: not as an affected job, not as a
    weight, not in the count. The plan is derived on read precisely so
    this needs no cleanup pass."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "confirmed")
    first = _create_job(client, token, _PYTHON_JOB, company="Acme")
    second = _create_job(client, token, _SECOND_JOB, company="Globex")

    before = _roadmap(client, token)
    assert before["saved_job_count"] == 2
    assert _affected_job_ids(before) == {first, second}
    # Redis is asked for by the second job alone, so it must leave with it.
    assert "Redis" in {item["skill_name"] for item in _items(before)}

    assert client.delete(f"{_JOBS}/{second}", headers=_headers(token)).status_code == 204

    after = _roadmap(client, token)

    assert after["saved_job_count"] == 1
    assert after["selected_job_count"] == 1
    assert second not in _affected_job_ids(after)
    assert _affected_job_ids(after) == {first}
    assert "Redis" not in {item["skill_name"] for item in _items(after)}
    # What the surviving job asks for is still there, and still ranked.
    assert "Python" in {item["skill_name"] for item in _items(after)}
    assert all(item["why"] for item in _items(after))
    assert all(item["affected_jobs"] for item in _items(after))

    # The job itself is gone, and so is every view of it.
    assert client.get(f"{_JOBS}/{second}/match", headers=_headers(token)).status_code == 404
    # And the survivor is untouched by its neighbour's removal.
    assert _get(client, token, first, "match")["formula_version"] == "skill_match_v1"


def test_deleting_the_only_selected_job_is_an_empty_input_not_an_empty_plan(
    client: TestClient,
) -> None:
    """The boundary of case 4. "You have not saved any jobs" is a
    statement about the INPUT; "no gaps found" would be a claim about the
    person, and the two must not be confused as the list empties."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    only = _create_job(client, token, _PYTHON_JOB)
    assert _items(_roadmap(client, token))

    assert client.delete(f"{_JOBS}/{only}", headers=_headers(token)).status_code == 204

    after = _roadmap(client, token)
    assert after["has_selected_jobs"] is False
    assert after["saved_job_count"] == 0
    assert after["reason"] == "no_selected_jobs"
    assert _items(after) == []
