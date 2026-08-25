"""The single place that decides whether a candidate satisfies a job
requirement (Prompt 4.4).

WHY THIS MODULE EXISTS. Prompt 4.3 answered "how well do I match?" and
Prompt 4.4 answers "what am I missing, and why?" — two views of the same
underlying judgement. Implementing that judgement twice would eventually
let the two disagree: the match panel saying "matched" while the gap
panel says "missing" is precisely the contradiction that destroys trust
in an explainability feature.

So the resolution lives here, once, and BOTH endpoints call it. Their
agreement is structural rather than something two test suites have to
keep verifying independently.

PURE: rows in, resolutions out. No database, no scoring, no I/O — the
same split app/skill_matching.py has from app/skill_extraction.py. The
`skill_match_v1` formula stays in app/matching/score.py and is NOT
duplicated here; 4.4 does not calculate scores.

MATCHING IS ON CANONICAL `skill_id`, never on skill names. Both sides
resolved aliases against the same curated taxonomy at extraction time.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from app.schemas.skill import CandidateSkillStatus


class ResolutionState(StrEnum):
    """What the candidate's side says about one job requirement.

    FOUR STATES, NEVER COLLAPSED TO TWO. "We could not find this skill",
    "you told us this is not yours", and "we found evidence you have not
    reviewed" are different things to tell a person, and flattening them
    into a single "missing" is exactly the loss of meaning this slice
    exists to prevent.
    """

    # status = confirmed. The user's own assertion. Satisfies the
    # requirement even with no evidence rows left — the existing,
    # tested semantics after a deleted resume or disconnected GitHub.
    SATISFIED = "satisfied"
    # status = suggested. Real, persisted evidence exists; only the
    # user's review is missing. It SATISFIES the requirement for scoring
    # (Prompt 4.3 counts it) but is NOT an ordinary gap — it is a
    # prompt to review.
    NEEDS_CONFIRMATION = "needs_confirmation"
    # status = rejected. A persistent tombstone. Never satisfies,
    # however much stale evidence still hangs off it — and never
    # silently folded in with "we could not find this".
    REJECTED = "rejected"
    # No candidate_skills row at all.
    MISSING = "missing"


# The two states that satisfy a requirement for SCORING purposes.
# Kept here, beside the state definitions, so Prompt 4.3's scoring and
# Prompt 4.4's bucketing can never drift apart on what "counts".
_SATISFYING = frozenset({ResolutionState.SATISFIED, ResolutionState.NEEDS_CONFIRMATION})


@dataclass(frozen=True)
class RequirementResolution:
    """One job requirement, resolved against the candidate.

    Carries everything both endpoints need to explain themselves, so
    neither has to re-derive anything from raw rows.
    """

    skill_id: uuid.UUID
    skill_name: str
    requirement_level: str
    job_excerpt: str
    state: ResolutionState
    # None when no candidate_skills row exists.
    candidate_status: str | None

    @property
    def satisfied(self) -> bool:
        """Whether this counts as met — the ONE definition both the
        score and the gap buckets are built on."""
        return self.state in _SATISFYING

    @property
    def is_gap(self) -> bool:
        """Whether this belongs in an ordinary level-based gap bucket.

        `needs_confirmation` is deliberately excluded: evidence exists,
        so calling it a gap would tell the user to go learn something
        they have already demonstrated. `rejected` is excluded too — it
        gets its own bucket so the user's decision is visible rather
        than reported back as an absence.
        """
        return self.state is ResolutionState.MISSING


def resolve_state(candidate_status: str | None) -> ResolutionState:
    """Map a candidate skill's review status onto a resolution state.

    The whole candidate-side policy, in one place and one direction. A
    missing row and an unrecognised status both resolve to MISSING:
    failing closed is right for a status vocabulary stored as plain text
    that is allowed to grow.
    """
    if candidate_status == CandidateSkillStatus.CONFIRMED.value:
        return ResolutionState.SATISFIED
    if candidate_status == CandidateSkillStatus.SUGGESTED.value:
        return ResolutionState.NEEDS_CONFIRMATION
    if candidate_status == CandidateSkillStatus.REJECTED.value:
        return ResolutionState.REJECTED
    return ResolutionState.MISSING


def resolve_requirements(
    requirements: list[tuple[uuid.UUID, str, str, str]],
    candidate_statuses: dict[uuid.UUID, str],
) -> list[RequirementResolution]:
    """Resolve every requirement against the candidate.

    `requirements` is (skill_id, skill_name, requirement_level,
    job_excerpt) — plain values rather than ORM rows, so this stays pure
    and testable without a database.

    Order is preserved; each caller sorts for its own presentation.
    """
    return [
        RequirementResolution(
            skill_id=skill_id,
            skill_name=skill_name,
            requirement_level=level,
            job_excerpt=excerpt,
            state=resolve_state(candidate_statuses.get(skill_id)),
            candidate_status=candidate_statuses.get(skill_id),
        )
        for skill_id, skill_name, level, excerpt in requirements
    ]
