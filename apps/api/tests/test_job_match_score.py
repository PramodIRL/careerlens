"""The skill_match_v1 formula, hand-calculated (Prompt 4.3).

Pure arithmetic — no database, no fixtures. Every expected number below
is written out longhand in its own test, because a score nobody can
reproduce on paper is a score nobody can argue with. No test here
asserts merely "score > 0".

    weights:   required = 3, preferred = 2, mentioned = 1
    earned     = sum of weights of SATISFIED requirements
    obtainable = sum of weights of ALL requirements
    overall    = round(earned / obtainable * 100)
"""

import itertools

import pytest

from app.matching.score import (
    FORMULA_VERSION,
    WEIGHTS,
    RequirementInput,
    compute_score,
)


def _req(level: str, satisfied: bool, skill: str = "s") -> RequirementInput:
    return RequirementInput(skill_id=skill, level=level, satisfied=satisfied)


# --- the version -------------------------------------------------------


def test_the_formula_is_versioned() -> None:
    """A screenshotted 77% is meaningless without knowing which
    arithmetic produced it."""
    assert FORMULA_VERSION == "skill_match_v1"
    assert compute_score([]).formula_version == "skill_match_v1"


def test_the_weights_are_the_documented_ordinals() -> None:
    assert WEIGHTS == {"required": 3, "preferred": 2, "mentioned": 1}


# --- worked examples, calculated by hand -------------------------------


def test_two_required_matched_one_preferred_missing_is_75() -> None:
    """Candidate: Python confirmed, PostgreSQL confirmed, Docker absent.
    Job:       Python required, PostgreSQL required, Docker preferred.

        earned     = 3 + 3     = 6
        obtainable = 3 + 3 + 2 = 8
        overall    = round(6/8 * 100) = 75
    """
    score = compute_score(
        [
            _req("required", True, "python"),
            _req("required", True, "postgres"),
            _req("preferred", False, "docker"),
        ]
    )

    assert score.earned_weight == 6
    assert score.obtainable_weight == 8
    assert score.overall_score == 75
    assert (score.required_matched, score.required_total) == (2, 2)


def test_five_preferred_matched_and_one_required_missing_is_77() -> None:
    """THE CASE THE FORMULA CHOICE TURNS ON.

    skill_match_v1 applies NO ceiling and NO penalty, so this scores on
    weighted coverage alone:

        earned     = 5 x 2         = 10
        obtainable = (5 x 2) + 3   = 13
        overall    = round(10/13 * 100) = round(76.92) = 77

    The missing hard requirement is not hidden — it is reported
    separately as required_matched 0 of 1, which is what makes the gap
    unmistakable without bending the arithmetic.
    """
    score = compute_score(
        [_req("preferred", True, f"p{i}") for i in range(5)] + [_req("required", False, "k8s")]
    )

    assert score.earned_weight == 10
    assert score.obtainable_weight == 13
    assert score.overall_score == 77
    assert (score.required_matched, score.required_total) == (0, 1)


def test_everything_matched_is_100() -> None:
    """earned = 3 + 2 + 1 = 6; obtainable = 6; 6/6 -> 100."""
    score = compute_score(
        [_req("required", True), _req("preferred", True), _req("mentioned", True)]
    )

    assert (score.earned_weight, score.obtainable_weight) == (6, 6)
    assert score.overall_score == 100


def test_nothing_matched_is_0() -> None:
    """earned = 0; obtainable = 6; 0/6 -> 0."""
    score = compute_score(
        [_req("required", False), _req("preferred", False), _req("mentioned", False)]
    )

    assert score.earned_weight == 0
    assert score.obtainable_weight == 6
    assert score.overall_score == 0
    assert score.has_requirements is True


def test_one_required_of_two_is_50() -> None:
    """earned = 3; obtainable = 6; 3/6 -> 50."""
    score = compute_score([_req("required", True, "a"), _req("required", False, "b")])

    assert score.overall_score == 50


def test_only_mentioned_skills_matched_scores_low() -> None:
    """A job whose hard requirements are all unmet scores by weight
    alone: earned = 1 + 1 = 2, obtainable = 3 + 1 + 1 = 5 -> 40."""
    score = compute_score(
        [
            _req("required", False, "a"),
            _req("mentioned", True, "b"),
            _req("mentioned", True, "c"),
        ]
    )

    assert (score.earned_weight, score.obtainable_weight) == (2, 5)
    assert score.overall_score == 40


def test_a_fractional_score_is_rounded_to_a_whole_percent() -> None:
    """earned = 1, obtainable = 1 + 3 = 4 -> 25 exactly."""
    score = compute_score([_req("mentioned", True, "a"), _req("required", False, "b")])

    assert (score.earned_weight, score.obtainable_weight) == (1, 4)
    assert score.overall_score == 25


def test_an_exact_half_uses_pythons_bankers_rounding() -> None:
    """Pinned rather than left to chance.

        earned     = 1
        obtainable = 1 + 2 + 2 + 3 = 8
        raw        = 12.5

    Python's round() rounds half to EVEN, so this is 12, not 13. The
    exact tie is vanishingly rare in practice, but a score endpoint
    should not have an undocumented behaviour at any input — and if this
    ever needs to be half-up instead, that is a formula change and gets
    a new version string.
    """
    score = compute_score(
        [
            _req("mentioned", True, "a"),
            _req("preferred", False, "b"),
            _req("preferred", False, "c"),
            _req("required", False, "d"),
        ]
    )

    assert (score.earned_weight, score.obtainable_weight) == (1, 8)
    assert score.overall_score == 12


# --- the no-requirements case ------------------------------------------


def test_a_job_with_no_requirements_is_not_a_zero_percent_candidate() -> None:
    """`has_requirements` exists so a client can distinguish "this job
    asks for nothing we recognise" from "this candidate matches
    nothing". Both give overall_score 0 and they mean entirely different
    things."""
    score = compute_score([])

    assert score.overall_score == 0
    assert score.obtainable_weight == 0
    assert score.has_requirements is False
    assert (score.required_matched, score.required_total) == (0, 0)


def test_a_job_with_requirements_reports_has_requirements() -> None:
    assert compute_score([_req("mentioned", False)]).has_requirements is True


# --- level breakdown ---------------------------------------------------


def test_the_level_breakdown_counts_matched_out_of_total() -> None:
    score = compute_score(
        [
            _req("required", True, "a"),
            _req("required", False, "b"),
            _req("preferred", True, "c"),
            _req("mentioned", False, "d"),
        ]
    )

    assert (score.by_level["required"].matched, score.by_level["required"].total) == (1, 2)
    assert (score.by_level["preferred"].matched, score.by_level["preferred"].total) == (1, 1)
    assert (score.by_level["mentioned"].matched, score.by_level["mentioned"].total) == (0, 1)


def test_every_level_is_present_even_at_zero() -> None:
    """A client never has to distinguish "no preferred skills" from "key
    missing"."""
    score = compute_score([_req("required", True)])

    assert set(score.by_level) == {"required", "preferred", "mentioned"}
    assert score.by_level["preferred"].total == 0


def test_the_weights_are_echoed_so_the_maths_can_be_checked() -> None:
    assert compute_score([_req("required", True)]).weights == {
        "required": 3,
        "preferred": 2,
        "mentioned": 1,
    }


# --- determinism -------------------------------------------------------


def test_the_score_is_independent_of_requirement_order() -> None:
    """Built from sums and counts, so shuffling the input cannot change
    any output — which is what makes two identical requests return
    byte-identical JSON."""
    requirements = [
        _req("required", True, "a"),
        _req("preferred", False, "b"),
        _req("mentioned", True, "c"),
    ]

    scores = {
        compute_score(list(order)).overall_score for order in itertools.permutations(requirements)
    }

    assert scores == {67}  # earned 4, obtainable 6 -> round(66.67) = 67


def test_repeated_computation_is_identical() -> None:
    requirements = [_req("required", True), _req("preferred", False)]
    first = compute_score(requirements)
    second = compute_score(requirements)

    assert (first.overall_score, first.earned_weight, first.obtainable_weight) == (
        second.overall_score,
        second.earned_weight,
        second.obtainable_weight,
    )


@pytest.mark.parametrize("level", ["required", "preferred", "mentioned"])
def test_a_single_matched_requirement_of_any_level_is_100(level: str) -> None:
    """Coverage is a ratio, so the level cannot matter when it is the
    only requirement."""
    assert compute_score([_req(level, True)]).overall_score == 100


def test_an_unrecognised_level_is_ignored_rather_than_crashing() -> None:
    """The level vocabulary is stored as plain text so it can grow. A
    score endpoint must not 500 because a newer writer added a level
    this deployment has not learned yet."""
    score = compute_score([_req("required", True, "a"), _req("aspirational", True, "b")])

    assert (score.earned_weight, score.obtainable_weight) == (3, 3)
    assert score.overall_score == 100
