"""The candidate-to-job skill match formula (Prompt 4.3).

PURE: requirements and candidate states in, a score breakdown out. No
database, no network, no LLM, no model inference — so the arithmetic is
testable by hand, which is the point. Same split app/skill_matching.py
has from app/skill_extraction.py.

    skill_match_v1
    ==============
    weights:   required = 3, preferred = 2, mentioned = 1
    earned     = sum of weights of requirements the candidate satisfies
    obtainable = sum of weights of every requirement on the job
    overall    = round(earned / obtainable * 100)

That is the whole formula. It is deliberately simple enough to check on
paper, because a score nobody can reproduce by hand is a score nobody
can argue with — and docs/project-brief.md's Evidence-First rule means
every number here has to be defensible from stored rows.

WHAT THIS FORMULA DELIBERATELY DOES NOT DO:

  * It does NOT multiply by confidence. `confidence` answers "does this
    string denote this skill" — a MATCH-QUALITY number — not "how good
    is this candidate at it" (app/schemas/skill.py states this outright,
    and Prompt 3.4 refused to blend it for the same reason). Scaling a
    score by 0.75 because the alias "py" matched would silently assert
    the candidate is 75% of a Python developer. Confidence is surfaced
    per skill instead, where a human can read it for what it is.

  * It does NOT penalise or cap for missing REQUIRED skills. A ceiling
    was considered and deliberately deferred: the 3/2/1 weights are
    already an ordinal judgement, and stacking a second uncalibrated
    judgement on top would make v1 harder to reason about with no
    evidence that it helps. Required coverage is reported SEPARATELY and
    prominently (`required_matched` / `required_total` /
    `required_missing`) so a candidate missing a hard requirement can
    see it plainly, even when the weighted score looks healthy. If
    evidence later shows a penalty helps, that is skill_match_v2 — and
    the version string is what makes changing it honest.

  * It does NOT give partial credit within a skill. A requirement is
    satisfied or it is not. Fractional satisfaction would need a
    capability number that does not exist anywhere in this system.
"""

from dataclasses import dataclass, field
from enum import StrEnum

# Bumped whenever the arithmetic changes — including the weights. A
# score is only reproducible if you know which formula produced it, and
# a stored or screenshotted 77% is meaningless without this.
FORMULA_VERSION = "skill_match_v1"


class RequirementWeight(StrEnum):
    """Named only so the weights below cannot drift out of step with the
    requirement levels app/job_requirements/classify.py defines."""

    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"


# Ordinal, not measured. There is no calibration data yet, so these are
# a deliberate "a required skill matters about three times as much as a
# passing mention" judgement — simple, hand-checkable, and versioned.
WEIGHTS: dict[str, int] = {
    RequirementWeight.REQUIRED.value: 3,
    RequirementWeight.PREFERRED.value: 2,
    RequirementWeight.MENTIONED.value: 1,
}


@dataclass(frozen=True)
class RequirementInput:
    """One job requirement, reduced to what the formula needs.

    `satisfied` is decided by the caller (app/api/v1/saved_job.py) from
    the candidate's status, NOT here: whether a rejected skill counts is
    a product rule about the candidate side, while this module is only
    the arithmetic.
    """

    skill_id: str
    level: str
    satisfied: bool


@dataclass
class LevelBreakdown:
    """Matched-out-of-total for one requirement level, so the response
    can say "Required 4 / 5" without the client recomputing it."""

    matched: int = 0
    total: int = 0


@dataclass
class MatchScore:
    """Every number the API returns, each traceable to stored rows.

    `has_requirements` exists so a client can distinguish "this job asks
    for nothing we recognise" from "this candidate matches nothing". Both
    produce overall_score = 0, and they mean completely different things
    — the first is a statement about the JOB, the second about the
    CANDIDATE. Showing "0% match" for the first would be actively
    misleading.
    """

    formula_version: str = FORMULA_VERSION
    overall_score: int = 0
    earned_weight: int = 0
    obtainable_weight: int = 0
    has_requirements: bool = False
    required_matched: int = 0
    required_total: int = 0
    by_level: dict[str, LevelBreakdown] = field(default_factory=dict)
    weights: dict[str, int] = field(default_factory=lambda: dict(WEIGHTS))


def compute_score(requirements: list[RequirementInput]) -> MatchScore:
    """Score one job against one candidate.

    Deterministic and order-independent: the result is built from sums
    and counts, so shuffling the input cannot change any output. That is
    what makes two identical requests return byte-identical JSON without
    the caller having to sort anything first.
    """
    score = MatchScore()
    # Every level is present even at zero, so a client never has to
    # distinguish "no preferred skills" from "key missing".
    score.by_level = {level: LevelBreakdown() for level in WEIGHTS}

    if not requirements:
        # obtainable_weight stays 0 and has_requirements stays False —
        # see MatchScore on why that is not the same as a 0% candidate.
        return score

    score.has_requirements = True
    for requirement in requirements:
        # An unrecognised level contributes nothing rather than raising:
        # the vocabulary is stored as plain text precisely so it can
        # grow, and a score endpoint should not 500 because a newer
        # writer added a level this deployment has not learned yet.
        weight = WEIGHTS.get(requirement.level)
        if weight is None:  # pragma: no cover - defensive
            continue

        breakdown = score.by_level[requirement.level]
        breakdown.total += 1
        score.obtainable_weight += weight
        if requirement.level == RequirementWeight.REQUIRED.value:
            score.required_total += 1

        if requirement.satisfied:
            breakdown.matched += 1
            score.earned_weight += weight
            if requirement.level == RequirementWeight.REQUIRED.value:
                score.required_matched += 1

    if score.obtainable_weight:
        score.overall_score = round(score.earned_weight / score.obtainable_weight * 100)
    return score
