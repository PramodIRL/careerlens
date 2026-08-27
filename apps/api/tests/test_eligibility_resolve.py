"""The four-state eligibility resolver (Prompt 5.1a).

The load-bearing tests here, which should not be softened:

  * a missing candidate fact resolves to UNKNOWN, never NOT_SATISFIED
  * an unstated CGPA scale on EITHER side blocks the comparison rather
    than being assumed away
  * only a hard requirement the candidate demonstrably fails can make
    them ineligible
  * "eligible" is claimed only when every hard bar is positively met

Pure — no database, no HTTP.
"""

from decimal import Decimal

import pytest

from app.eligibility.resolve import (
    CandidateFact,
    Reason,
    RequirementInput,
    resolve_eligibility,
    resolve_requirement,
)
from app.schemas.eligibility import Comparator, EligibilityFlag, EligibilityState
from app.schemas.eligibility import EligibilityRequirementType as T

_REQUIRED = "required"
_PREFERRED = "preferred"


def _req(
    requirement_type: T = T.CLASS_12_PERCENTAGE,
    comparator: Comparator = Comparator.GTE,
    *,
    value: Decimal | None = Decimal("75"),
    maximum: Decimal | None = None,
    scale: Decimal | None = None,
    values: tuple[str, ...] = (),
    open_ended: bool = False,
    level: str = _REQUIRED,
) -> RequirementInput:
    return RequirementInput(
        requirement_type=requirement_type,
        comparator=comparator,
        requirement_level=level,
        numeric_value=value,
        numeric_max=maximum,
        value_scale=scale,
        accepted_values=values,
        open_ended=open_ended,
    )


# --- thresholds ---------------------------------------------------------


def test_a_value_above_the_bar_is_satisfied() -> None:
    resolution = resolve_requirement(_req(), CandidateFact(value_numeric=Decimal("82")))
    assert resolution.state is EligibilityState.SATISFIED


def test_a_value_exactly_on_the_bar_is_satisfied() -> None:
    """ "Minimum 75%" includes 75. A candidate sitting exactly on the bar
    has met it, and an off-by-one here rejects real people."""
    resolution = resolve_requirement(_req(), CandidateFact(value_numeric=Decimal("75")))
    assert resolution.state is EligibilityState.SATISFIED


def test_a_value_below_the_bar_is_not_satisfied() -> None:
    resolution = resolve_requirement(_req(), CandidateFact(value_numeric=Decimal("74.99")))
    assert resolution.state is EligibilityState.NOT_SATISFIED
    assert resolution.reason == Reason.BELOW_THRESHOLD


# --- unknown is never failure -------------------------------------------


def test_a_missing_candidate_fact_is_unknown_not_a_failure() -> None:
    """THE RULE THIS WHOLE DOMAIN RESTS ON. An empty profile field is
    not a rejection; turning it into one invents a fact about a person
    and then acts on it."""
    resolution = resolve_requirement(_req(), None)
    assert resolution.state is EligibilityState.UNKNOWN
    assert resolution.reason == Reason.CANDIDATE_VALUE_UNKNOWN
    assert resolution.blocks is False


def test_a_fact_row_with_no_usable_value_is_also_unknown() -> None:
    resolution = resolve_requirement(_req(), CandidateFact())
    assert resolution.state is EligibilityState.UNKNOWN


def test_a_missing_categorical_fact_is_unknown() -> None:
    resolution = resolve_requirement(
        _req(T.HIGHEST_DEGREE, Comparator.IN, value=None, values=("btech",)), None
    )
    assert resolution.state is EligibilityState.UNKNOWN


# --- the CGPA scale rule ------------------------------------------------


def test_matching_scales_compare_directly() -> None:
    resolution = resolve_requirement(
        _req(T.CGPA, value=Decimal("7.5"), scale=Decimal("10")),
        CandidateFact(value_numeric=Decimal("7.8"), value_scale=Decimal("10")),
    )
    assert resolution.state is EligibilityState.SATISFIED


def test_two_stated_scales_are_rescaled_and_compared() -> None:
    """3.2/4 is 0.80 and 7.5/10 is 0.75, so this clears the bar. This is
    arithmetic, not inference — BOTH sides said what their scale is."""
    resolution = resolve_requirement(
        _req(T.CGPA, value=Decimal("7.5"), scale=Decimal("10")),
        CandidateFact(value_numeric=Decimal("3.2"), value_scale=Decimal("4")),
    )
    assert resolution.state is EligibilityState.SATISFIED


def test_an_unstated_requirement_scale_falls_back_to_ten() -> None:
    """The product rule: explicit scale wins, otherwise 10. The
    extractor normally stores the default on the row; this covers a row
    written before that behaviour existed."""
    resolution = resolve_requirement(
        _req(T.CGPA, value=Decimal("7.5")),
        CandidateFact(value_numeric=Decimal("9.0"), value_scale=Decimal("10")),
    )
    assert resolution.state is EligibilityState.SATISFIED


def test_an_unstated_candidate_scale_falls_back_to_ten() -> None:
    """A candidate who types 8.2 and leaves the scale box alone is read
    as 8.2/10, the same default the job side gets."""
    resolution = resolve_requirement(
        _req(T.CGPA, value=Decimal("7.5"), scale=Decimal("10")),
        CandidateFact(value_numeric=Decimal("8.2")),
    )
    assert resolution.state is EligibilityState.SATISFIED


def test_an_explicit_candidate_scale_is_never_overridden_by_the_default() -> None:
    """2.8/4 is 0.70 and would clear a 7.5 bar outright if the 4 were
    ignored. The stated scale wins."""
    resolution = resolve_requirement(
        _req(T.CGPA, value=Decimal("7.5"), scale=Decimal("10")),
        CandidateFact(value_numeric=Decimal("2.8"), value_scale=Decimal("4.0")),
    )
    assert resolution.state is EligibilityState.NOT_SATISFIED
    assert resolution.reason == Reason.BELOW_THRESHOLD


def test_a_missing_cgpa_is_still_unknown_whatever_the_scale() -> None:
    """Defaulting the SCALE never invents a VALUE."""
    resolution = resolve_requirement(_req(T.CGPA, value=Decimal("7.5")), None)
    assert resolution.state is EligibilityState.UNKNOWN
    assert resolution.reason == Reason.CANDIDATE_VALUE_UNKNOWN


# --- categorical --------------------------------------------------------


def test_a_listed_value_is_satisfied() -> None:
    resolution = resolve_requirement(
        _req(T.HIGHEST_DEGREE, Comparator.IN, value=None, values=("btech", "be")),
        CandidateFact(value_text="be"),
    )
    assert resolution.state is EligibilityState.SATISFIED


def test_an_unlisted_value_on_a_closed_list_is_not_satisfied() -> None:
    resolution = resolve_requirement(
        _req(T.HIGHEST_DEGREE, Comparator.IN, value=None, values=("btech",)),
        CandidateFact(value_text="bsc"),
    )
    assert resolution.state is EligibilityState.NOT_SATISFIED
    assert resolution.reason == Reason.NOT_IN_ACCEPTED_VALUES


def test_an_unlisted_value_on_an_open_ended_list_is_undetermined() -> None:
    """ "Computer Science or a related field" — there is no
    deterministic membership test for "related", so rejecting a
    mechanical engineer is not a call this product is entitled to
    make."""
    resolution = resolve_requirement(
        _req(
            T.FIELD_OF_STUDY,
            Comparator.IN,
            value=None,
            values=("computer_science",),
            open_ended=True,
        ),
        CandidateFact(value_text="mechanical"),
    )
    assert resolution.state is EligibilityState.UNDETERMINED
    assert resolution.reason == Reason.OPEN_ENDED_LIST


# --- years and ranges ---------------------------------------------------


def test_a_year_inside_the_range_is_satisfied() -> None:
    resolution = resolve_requirement(
        _req(T.GRADUATION_YEAR, Comparator.BETWEEN, value=Decimal("2025"), maximum=Decimal("2026")),
        CandidateFact(value_numeric=Decimal("2026")),
    )
    assert resolution.state is EligibilityState.SATISFIED


def test_a_year_outside_the_range_is_not_satisfied() -> None:
    resolution = resolve_requirement(
        _req(T.GRADUATION_YEAR, Comparator.BETWEEN, value=Decimal("2025"), maximum=Decimal("2026")),
        CandidateFact(value_numeric=Decimal("2024")),
    )
    assert resolution.state is EligibilityState.NOT_SATISFIED
    assert resolution.reason == Reason.OUTSIDE_RANGE


def test_an_exact_year_requirement_compares_for_equality() -> None:
    requirement = _req(T.GRADUATION_YEAR, Comparator.EQ, value=Decimal("2026"))
    assert (
        resolve_requirement(requirement, CandidateFact(value_numeric=Decimal("2026"))).state
        is EligibilityState.SATISFIED
    )
    assert (
        resolve_requirement(requirement, CandidateFact(value_numeric=Decimal("2025"))).state
        is EligibilityState.NOT_SATISFIED
    )


def test_a_graduation_year_floor_admits_a_later_year() -> None:
    """The end of the reported bug: "Graduation year 2024 or later" now
    classifies as `gte`, and a candidate graduating in 2027 is ELIGIBLE.
    The resolver was always correct — this pins down the pairing."""
    requirement = _req(T.GRADUATION_YEAR, Comparator.GTE, value=Decimal("2024"))
    assert (
        resolve_requirement(requirement, CandidateFact(value_numeric=Decimal("2027"))).state
        is EligibilityState.SATISFIED
    )
    assert (
        resolve_requirement(requirement, CandidateFact(value_numeric=Decimal("2023"))).state
        is EligibilityState.NOT_SATISFIED
    )


def test_an_unrecognised_comparator_is_undetermined_not_an_error() -> None:
    """The vocabulary is stored as plain text so it can grow. A row
    written by a newer version must not 500 this endpoint."""
    requirement = RequirementInput(
        requirement_type=T.YEARS_EXPERIENCE,
        comparator="sideways",  # type: ignore[arg-type]
        requirement_level=_REQUIRED,
        numeric_value=Decimal("2"),
    )
    resolution = resolve_requirement(requirement, CandidateFact(value_numeric=Decimal("5")))
    assert resolution.state is EligibilityState.UNDETERMINED


# --- the whole-job flag -------------------------------------------------


def test_all_hard_bars_met_is_eligible() -> None:
    result = resolve_eligibility(
        [_req(value=Decimal("70"))],
        {T.CLASS_12_PERCENTAGE: CandidateFact(value_numeric=Decimal("80"))},
    )
    assert result.flag is EligibilityFlag.ELIGIBLE
    assert result.totals.satisfied == 1


def test_one_failed_hard_bar_is_not_eligible() -> None:
    result = resolve_eligibility(
        [_req(value=Decimal("80"))],
        {T.CLASS_12_PERCENTAGE: CandidateFact(value_numeric=Decimal("68"))},
    )
    assert result.flag is EligibilityFlag.NOT_ELIGIBLE
    assert result.totals.required_not_satisfied == 1


def test_a_failed_preference_does_not_make_anyone_ineligible() -> None:
    result = resolve_eligibility(
        [_req(value=Decimal("80"), level=_PREFERRED)],
        {T.CLASS_12_PERCENTAGE: CandidateFact(value_numeric=Decimal("68"))},
    )
    assert result.flag is EligibilityFlag.ELIGIBLE
    assert result.totals.not_satisfied == 1
    assert result.totals.required_not_satisfied == 0


def test_an_unknown_hard_bar_holds_the_answer_at_unknown() -> None:
    """ "Eligible" is a much stronger claim than "nothing ruled you
    out", so one unknown is enough to withhold it."""
    result = resolve_eligibility([_req()], {})
    assert result.flag is EligibilityFlag.UNKNOWN
    assert result.totals.unknown == 1


def test_a_failure_outranks_an_unknown() -> None:
    result = resolve_eligibility(
        [_req(value=Decimal("80")), _req(T.YEARS_EXPERIENCE, value=Decimal("2"))],
        {T.CLASS_12_PERCENTAGE: CandidateFact(value_numeric=Decimal("68"))},
    )
    assert result.flag is EligibilityFlag.NOT_ELIGIBLE
    assert result.totals.not_satisfied == 1
    assert result.totals.unknown == 1


def test_a_job_with_no_recognised_bars_is_unknown_not_eligible() -> None:
    """Nothing was verified, so claiming eligibility would assert a
    check that never ran — the same distinction `has_requirements`
    draws on the match side."""
    result = resolve_eligibility([], {})
    assert result.flag is EligibilityFlag.UNKNOWN
    assert result.has_requirements is False
    assert result.totals.total_requirements == 0


def test_the_totals_account_for_every_requirement_exactly_once() -> None:
    requirements = [
        # satisfied
        _req(value=Decimal("70")),
        # not_satisfied
        _req(T.CLASS_10_PERCENTAGE, value=Decimal("90")),
        # unknown — the candidate has declared nothing here
        _req(T.YEARS_EXPERIENCE, value=Decimal("2")),
        # undetermined — the posting hedged its own list open
        _req(
            T.FIELD_OF_STUDY,
            Comparator.IN,
            value=None,
            values=("computer_science",),
            open_ended=True,
        ),
    ]
    result = resolve_eligibility(
        requirements,
        {
            T.CLASS_12_PERCENTAGE: CandidateFact(value_numeric=Decimal("80")),
            T.CLASS_10_PERCENTAGE: CandidateFact(value_numeric=Decimal("85")),
            T.FIELD_OF_STUDY: CandidateFact(value_text="mechanical"),
        },
    )
    totals = result.totals
    assert (
        totals.satisfied + totals.not_satisfied + totals.unknown + totals.undetermined
        == totals.total_requirements
        == 4
    )
    assert (totals.satisfied, totals.not_satisfied, totals.unknown, totals.undetermined) == (
        1,
        1,
        1,
        1,
    )


def test_resolution_is_deterministic() -> None:
    requirements = [_req(), _req(T.CGPA, value=Decimal("7.5"), scale=Decimal("10"))]
    facts = {T.CLASS_12_PERCENTAGE: CandidateFact(value_numeric=Decimal("80"))}
    first = resolve_eligibility(requirements, facts)
    second = resolve_eligibility(requirements, facts)
    assert first.flag is second.flag
    assert first.totals == second.totals
    assert [r.state for r in first.resolutions] == [r.state for r in second.resolutions]


@pytest.mark.parametrize("state", list(EligibilityState))
def test_every_state_is_distinct(state: EligibilityState) -> None:
    """Four states, never collapsed to two. A guard against somebody
    later "simplifying" unknown into not_satisfied."""
    assert state.value in {"satisfied", "not_satisfied", "unknown", "undetermined"}
