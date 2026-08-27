"""Deterministic eligibility parsing (Prompt 5.1a).

The load-bearing tests here, which should not be softened:

  * an UNSTATED CGPA scale stays unstated — never inferred from the
    number
  * one percentage stated for two school classes produces TWO rows
  * "or related field" is recorded, never expanded
  * ambiguous and negated phrasings produce NOTHING rather than a guess
  * a number that merely looks like a threshold is not one

No database and no fixtures — this module is pure text in, values out.
"""

from decimal import Decimal

import pytest

from app.eligibility.classify import (
    DEFAULT_CGPA_SCALE,
    LEVEL_PREFERRED,
    LEVEL_REQUIRED,
    ParsedRequirement,
    parse_eligibility_requirements,
    strictest,
)
from app.schemas.eligibility import Comparator
from app.schemas.eligibility import EligibilityRequirementType as T


def _by_type(text: str) -> dict[str, ParsedRequirement]:
    grouped: dict[str, list[ParsedRequirement]] = {}
    for parsed in parse_eligibility_requirements(text):
        grouped.setdefault(parsed.requirement_type.value, []).append(parsed)
    return {key: strictest(group) for key, group in grouped.items()}


# --- CGPA and the scale rule --------------------------------------------


def test_a_stated_fractional_scale_is_recorded() -> None:
    parsed = _by_type("CGPA: 8.2/10 required.")[T.CGPA.value]
    assert parsed.numeric_value == Decimal("8.2")
    assert parsed.value_scale == Decimal("10")
    assert parsed.comparator is Comparator.GTE


def test_a_four_point_scale_is_recorded_as_four() -> None:
    """The number is below 4, so nothing about it forces a reading —
    the posting saying "/4.0" is the only reason we know."""
    parsed = _by_type("Minimum GPA of 3.6/4.0.")[T.CGPA.value]
    assert parsed.numeric_value == Decimal("3.6")
    assert parsed.value_scale == Decimal("4.0")


def test_a_scale_stated_in_words_is_recorded() -> None:
    parsed = _by_type("CGPA of at least 7.0 out of 10.")[T.CGPA.value]
    assert parsed.value_scale == Decimal("10")


def test_an_unstated_scale_defaults_to_ten() -> None:
    """The product rule: explicit scale wins, otherwise 10.

    The default is STORED on the row rather than applied silently at
    comparison time, so the API response says "7.5/10" and a reader can
    see exactly what was assumed on their behalf.
    """
    parsed = _by_type("Minimum CGPA 7.5.")[T.CGPA.value]
    assert parsed.numeric_value == Decimal("7.5")
    assert parsed.value_scale == DEFAULT_CGPA_SCALE == Decimal("10")


def test_an_explicit_scale_is_never_overridden_by_the_default() -> None:
    parsed = _by_type("Minimum GPA of 3.6/4.0.")[T.CGPA.value]
    assert parsed.value_scale == Decimal("4.0")


def test_a_scale_that_cannot_hold_its_own_value_is_dropped() -> None:
    """A misparse, not a discovery."""
    assert T.CGPA.value not in _by_type("CGPA 8.2/4.")


def test_an_implausible_bare_figure_is_dropped_rather_than_defaulted() -> None:
    """ "CGPA 85" is somebody writing a percentage. Defaulting it to a
    10-point scale would store a bar nobody could ever meet."""
    assert T.CGPA.value not in _by_type("CGPA 85 required.")


def test_a_bare_number_is_not_a_cgpa() -> None:
    """The keyword is mandatory. Without it this is just a number in a
    sentence, and reading a requirement into it invents one."""
    assert _by_type("We have 7.5 million users.") == {}


# --- school percentages -------------------------------------------------


def test_one_percentage_for_two_classes_produces_two_rows() -> None:
    """Prompt 5.1's worked example. TWO independently resolvable rows,
    because a candidate can clear one and fail the other and deserves to
    be told which."""
    parsed = _by_type("75% in Class 10 and Class 12.")
    assert parsed[T.CLASS_10_PERCENTAGE.value].numeric_value == Decimal("75")
    assert parsed[T.CLASS_12_PERCENTAGE.value].numeric_value == Decimal("75")


def test_two_percentages_for_two_classes_pair_in_order() -> None:
    parsed = _by_type("Class X: 92%, Class XII: 88%.")
    assert parsed[T.CLASS_10_PERCENTAGE.value].numeric_value == Decimal("92")
    assert parsed[T.CLASS_12_PERCENTAGE.value].numeric_value == Decimal("88")


def test_a_single_class_requirement_produces_one_row() -> None:
    parsed = _by_type("At least 80% in Class 12.")
    assert set(parsed) == {T.CLASS_12_PERCENTAGE.value}
    assert parsed[T.CLASS_12_PERCENTAGE.value].numeric_value == Decimal("80")


def test_a_mismatched_count_of_percentages_and_classes_is_left_unparsed() -> None:
    """Three numbers and two classes cannot be paired without guessing
    which belongs to which, so nothing is written."""
    assert _by_type("Class 10 and Class 12: 70%, 80% and 90%.") == {}


# --- experience ---------------------------------------------------------


def test_a_plus_suffix_is_a_minimum() -> None:
    parsed = _by_type("2+ years of experience.")[T.YEARS_EXPERIENCE.value]
    assert parsed.comparator is Comparator.GTE
    assert parsed.numeric_value == Decimal("2")


def test_years_without_an_experience_context_is_not_a_requirement() -> None:
    """ "a 4 year growth plan" has a number and the word "years" and
    asks nothing of the candidate."""
    assert _by_type("We offer a 4 year growth plan.") == {}


def test_the_tighter_bar_wins_among_statements_of_equal_strength() -> None:
    parsed = _by_type("Minimum 2 years of experience. At least 5 years of experience.")
    assert parsed[T.YEARS_EXPERIENCE.value].numeric_value == Decimal("5")


def test_a_hard_bar_outranks_a_stricter_preference() -> None:
    """A required 2 years and a preferred 5 years: the BINDING bar is
    the hard one, even though the preference asks for more. Taking the
    larger number would report a candidate with 3 years as failing a
    requirement the posting never made.
    """
    parsed = _by_type("At least 2 years of experience. 5 years of experience preferred.")
    assert parsed[T.YEARS_EXPERIENCE.value].numeric_value == Decimal("2")
    assert parsed[T.YEARS_EXPERIENCE.value].requirement_level == LEVEL_REQUIRED


# --- degree and field ---------------------------------------------------


def test_a_degree_and_a_field_are_separate_requirements() -> None:
    """Prompt 5.1's worked example. Two dimensions, two independently
    resolvable rows, one shared excerpt."""
    parsed = _by_type("B.Tech in Computer Science or related field.")
    assert parsed[T.HIGHEST_DEGREE.value].accepted_values == ("btech",)
    assert parsed[T.FIELD_OF_STUDY.value].accepted_values == ("computer_science",)


def test_or_related_field_is_recorded_not_expanded() -> None:
    """The list is NOT widened to guess at "related" — the flag is set
    so the resolver can decline to reject instead."""
    parsed = _by_type("B.Tech in Computer Science or related field.")
    assert parsed[T.FIELD_OF_STUDY.value].open_ended is True
    assert parsed[T.FIELD_OF_STUDY.value].accepted_values == ("computer_science",)


def test_alternatives_in_one_sentence_are_one_requirement() -> None:
    """ "B.Tech or B.E." is one bar accepting two degrees, not two bars
    the candidate must clear both of."""
    parsed = _by_type("B.Tech or B.E. in CSE / ECE.")
    assert set(parsed[T.HIGHEST_DEGREE.value].accepted_values) == {"btech", "be"}
    assert set(parsed[T.FIELD_OF_STUDY.value].accepted_values) == {
        "computer_science",
        "electronics",
    }
    assert parsed[T.FIELD_OF_STUDY.value].open_ended is False


def test_a_closed_list_is_not_marked_open_ended() -> None:
    assert _by_type("B.Tech required.")[T.HIGHEST_DEGREE.value].open_ended is False


@pytest.mark.parametrize("text", ["Please be on time.", "This role will be great."])
def test_the_english_word_be_is_not_a_bachelor_of_engineering(text: str) -> None:
    """A bare "be" is the commonest word in a job posting. Only "B.E."
    with its punctuation counts."""
    assert T.HIGHEST_DEGREE.value not in _by_type(text)


# --- graduation year ----------------------------------------------------


def test_a_single_graduation_year_is_an_equality() -> None:
    parsed = _by_type("Graduating in 2026.")[T.GRADUATION_YEAR.value]
    assert parsed.comparator is Comparator.EQ
    assert parsed.numeric_value == Decimal("2026")


def test_a_batch_range_becomes_a_between() -> None:
    parsed = _by_type("2025 - 2026 batch only.")[T.GRADUATION_YEAR.value]
    assert parsed.comparator is Comparator.BETWEEN
    assert parsed.numeric_value == Decimal("2025")
    assert parsed.numeric_max == Decimal("2026")


def test_a_year_with_no_graduation_context_is_ignored() -> None:
    """A posting is full of four-digit numbers and none of them
    constrain who may apply."""
    assert _by_type("Founded in 2011, we serve millions.") == {}


def test_or_later_is_a_floor_not_an_equality() -> None:
    """The reported bug: "2024 or later" was stored as `eq 2024`, so a
    candidate graduating in 2027 was reported NOT ELIGIBLE."""
    parsed = _by_type("Graduation year 2024 or later.")[T.GRADUATION_YEAR.value]
    assert parsed.comparator is Comparator.GTE
    assert parsed.numeric_value == Decimal("2024")


def test_or_earlier_is_a_ceiling() -> None:
    parsed = _by_type("Graduation year 2024 or earlier.")[T.GRADUATION_YEAR.value]
    assert parsed.comparator is Comparator.LTE
    assert parsed.numeric_value == Decimal("2024")


def test_an_alternate_inclusive_phrasing_is_also_a_floor() -> None:
    """The cue vocabulary is a list, so at least one member beyond the
    two headline phrasings needs to be pinned down."""
    parsed = _by_type("Must graduate in 2022 or after.")[T.GRADUATION_YEAR.value]
    assert parsed.comparator is Comparator.GTE
    assert parsed.numeric_value == Decimal("2022")


def test_a_bare_year_stays_an_equality() -> None:
    """Guards the fix against over-reaching: a clause naming one year
    with no direction cue IS an exact batch requirement."""
    parsed = _by_type("Graduation year 2024.")[T.GRADUATION_YEAR.value]
    assert parsed.comparator is Comparator.EQ
    assert parsed.numeric_value == Decimal("2024")


def test_a_negated_graduation_clause_still_produces_nothing() -> None:
    """Direction cues must not resurrect a clause negation suppresses."""
    assert _by_type("No specific graduation year, 2024 or later or otherwise.") == {}


# --- negation and levels ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "No minimum CGPA required.",
        "No specific CGPA criteria.",
        "We hire regardless of CGPA 7.5 cutoffs.",
    ],
)
def test_a_negated_bar_produces_nothing(text: str) -> None:
    """ "We do not screen on CGPA" is the ABSENCE of a bar, not a bar set
    to zero — so no row at all, not a relaxed one."""
    assert T.CGPA.value not in _by_type(text)


def test_a_preference_cue_downgrades_the_level() -> None:
    assert _by_type("CGPA 7.5 preferred.")[T.CGPA.value].requirement_level == LEVEL_PREFERRED


def test_a_plain_threshold_is_a_hard_requirement() -> None:
    assert _by_type("Minimum CGPA 7.5.")[T.CGPA.value].requirement_level == LEVEL_REQUIRED


def test_a_hard_statement_outranks_a_preference_for_the_same_bar() -> None:
    parsed = _by_type("CGPA 7.0 preferred. Minimum CGPA 6.5 required.")
    assert parsed[T.CGPA.value].requirement_level == LEVEL_REQUIRED
    assert parsed[T.CGPA.value].numeric_value == Decimal("6.5")


# --- windowing and determinism ------------------------------------------


def test_a_bar_is_not_contaminated_by_a_neighbouring_sentence() -> None:
    text = "Python is required. Minimum CGPA 7.5. Docker is a plus."
    parsed = _by_type(text)[T.CGPA.value]
    assert parsed.requirement_level == LEVEL_REQUIRED
    assert parsed.excerpt == "Minimum CGPA 7.5"


def test_the_excerpt_is_a_verbatim_slice_of_the_description() -> None:
    """Evidence-First as an assertion: a reader can find this text on
    the page by eye. Nothing is summarised or reworded."""
    text = "Candidates need a minimum CGPA of 7.5/10 to apply."
    for parsed in parse_eligibility_requirements(text):
        assert parsed.excerpt in text


def test_parsing_is_deterministic() -> None:
    text = (
        "Minimum CGPA 7.5/10. 75% in Class 10 and Class 12. "
        "B.Tech in Computer Science. 2+ years of experience."
    )
    assert parse_eligibility_requirements(text) == parse_eligibility_requirements(text)


def test_an_empty_description_yields_nothing() -> None:
    assert parse_eligibility_requirements("") == []
