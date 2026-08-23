"""Unit tests for the deterministic skill matcher (app/skill_matching.py).

Pure functions, no database and no fixtures — which is the point of
keeping matching separate from persistence: the rules that decide
whether a resume "mentions Python" can be pinned down exhaustively and
read as a specification.

Covers the four things most likely to embarrass this feature in front of
a user: aliases resolving to the right skill, word boundaries (Java is
not JavaScript), false positives from ordinary English words, and
excerpts that are exact source spans.
"""

import uuid
from decimal import Decimal

import pytest

from app.skill_matching import (
    CONFIDENCE_BY_KIND,
    MatchKind,
    SkillTerm,
    compile_term,
    find_skill_matches,
)

PYTHON = uuid.uuid4()
JAVASCRIPT = uuid.uuid4()
JAVA = uuid.uuid4()
GO = uuid.uuid4()
NEXTJS = uuid.uuid4()
OOP = uuid.uuid4()
EXPRESS = uuid.uuid4()
CICD = uuid.uuid4()
UNIT_TESTING = uuid.uuid4()

TERMS = [
    SkillTerm(PYTHON, "Python", is_alias=False),
    SkillTerm(PYTHON, "py", is_alias=True),
    SkillTerm(JAVASCRIPT, "JavaScript", is_alias=False),
    SkillTerm(JAVASCRIPT, "js", is_alias=True),
    SkillTerm(JAVA, "Java", is_alias=False),
    SkillTerm(GO, "Go", is_alias=False),
    SkillTerm(GO, "golang", is_alias=True),
    SkillTerm(NEXTJS, "Next.js", is_alias=False),
    SkillTerm(OOP, "Object-Oriented Programming", is_alias=False),
    SkillTerm(EXPRESS, "Express", is_alias=False),
    SkillTerm(CICD, "CI/CD", is_alias=False),
    SkillTerm(UNIT_TESTING, "Unit Testing", is_alias=False),
]


def _matched_ids(text: str) -> set[uuid.UUID]:
    return {match.skill_id for match in find_skill_matches(text, TERMS)}


def _match_for(text: str, skill_id: uuid.UUID):
    for match in find_skill_matches(text, TERMS):
        if match.skill_id == skill_id:
            return match
    return None


# --- canonical names and aliases --------------------------------------


def test_matches_a_canonical_skill_name() -> None:
    assert PYTHON in _matched_ids("Experienced with Python and testing.")


def test_matches_an_alias_and_resolves_it_to_the_canonical_skill() -> None:
    match = _match_for("Strong js background", JAVASCRIPT)

    assert match is not None
    assert match.kind is MatchKind.ALIAS


@pytest.mark.parametrize("spelling", ["python", "PYTHON", "Python", "PyThOn"])
def test_matching_is_case_insensitive(spelling: str) -> None:
    assert PYTHON in _matched_ids(f"Built services in {spelling}.")


def test_a_canonical_match_outranks_an_alias_match_for_the_same_skill() -> None:
    """Confidence should reflect the strongest available signal, even
    when the weaker spelling appears first in the document."""
    match = _match_for("Wrote py scripts, then moved to Python full time.", PYTHON)

    assert match is not None
    assert match.kind is MatchKind.CANONICAL
    assert match.confidence == Decimal("0.90")


def test_one_match_per_skill_however_many_times_it_appears() -> None:
    matches = find_skill_matches("Python, Python, and more Python.", TERMS)

    assert [m.skill_id for m in matches] == [PYTHON]


# --- word boundaries --------------------------------------------------


def test_java_is_not_matched_inside_javascript() -> None:
    """The canonical false positive this whole boundary scheme exists
    to prevent."""
    matched = _matched_ids("Five years of JavaScript.")

    assert JAVASCRIPT in matched
    assert JAVA not in matched


def test_the_js_alias_is_not_matched_inside_javascript() -> None:
    match = _match_for("Five years of JavaScript.", JAVASCRIPT)

    assert match is not None
    assert match.kind is MatchKind.CANONICAL  # matched the name, not "js"


@pytest.mark.parametrize("text", ["Google Cloud experience", "Ongoing work", "I am going home"])
def test_go_is_not_matched_inside_a_longer_word(text: str) -> None:
    assert GO not in _matched_ids(text)


def test_a_skill_name_glued_to_other_letters_does_not_match() -> None:
    assert PYTHON not in _matched_ids("Pythonic code and pythonista culture")


# --- punctuation, hyphenation, phrases --------------------------------


@pytest.mark.parametrize("spelling", ["Next.js", "NextJS", "Next JS", "next-js", "nextjs"])
def test_separator_variants_all_resolve_to_one_skill(spelling: str) -> None:
    assert NEXTJS in _matched_ids(f"Shipped a {spelling} app.")


@pytest.mark.parametrize(
    "spelling",
    ["Object-Oriented Programming", "Object Oriented Programming", "object-oriented  programming"],
)
def test_hyphenation_variants_of_a_phrase_match(spelling: str) -> None:
    assert OOP in _matched_ids(f"Taught {spelling} to juniors.")


@pytest.mark.parametrize("spelling", ["CI/CD", "CI-CD", "ci cd", "cicd"])
def test_slash_separated_terms_match_their_variants(spelling: str) -> None:
    assert CICD in _matched_ids(f"Owned the {spelling} pipeline.")


def test_a_multi_word_phrase_does_not_match_when_split_across_a_sentence() -> None:
    assert UNIT_TESTING not in _matched_ids("The unit. Testing was manual.")


# --- false positives from ordinary English ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I go to the office twice a week",
        "Go the extra mile for customers",
        "Willing to go above and beyond",
    ],
)
def test_go_in_prose_is_not_a_skill(text: str) -> None:
    assert GO not in _matched_ids(text)


@pytest.mark.parametrize(
    "text",
    [
        "Languages: Python, Go, Docker",
        "Skills\nGo\nKubernetes",
        "• Go",
        "Languages: Go",
        "Python | Go | SQL",
    ],
)
def test_go_in_list_context_is_a_skill(text: str) -> None:
    assert GO in _matched_ids(text)


def test_an_ambiguous_term_gets_the_lowest_confidence() -> None:
    match = _match_for("Languages: Python, Go, SQL", GO)

    assert match is not None
    assert match.kind is MatchKind.AMBIGUOUS_LIST_CONTEXT
    assert match.confidence == Decimal("0.60")


@pytest.mark.parametrize(
    "text", ["Express delivery of business value", "Able to express complex ideas simply"]
)
def test_express_in_prose_is_not_a_skill(text: str) -> None:
    assert EXPRESS not in _matched_ids(text)


def test_express_in_a_stack_list_is_a_skill() -> None:
    assert EXPRESS in _matched_ids("Backend: Node.js, Express, MongoDB")


def test_a_later_list_occurrence_is_found_when_the_first_is_prose() -> None:
    """An ambiguous term rejected in prose must not stop a genuine
    list-context occurrence later in the document from matching."""
    text = "I go to conferences.\nLanguages: Go, Python"

    assert GO in _matched_ids(text)


# --- excerpts ---------------------------------------------------------


def test_excerpt_is_the_line_containing_the_match() -> None:
    text = "SUMMARY\nBuilt a data pipeline in Python for a research team.\nEDUCATION"

    match = _match_for(text, PYTHON)

    assert match is not None
    assert match.excerpt == "Built a data pipeline in Python for a research team."


def test_excerpt_collapses_whitespace() -> None:
    match = _match_for("Used    Python\tdaily", PYTHON)

    assert match is not None
    assert match.excerpt == "Used Python daily"


def test_a_very_long_line_is_windowed_with_ellipses() -> None:
    text = "x " * 400 + "Python " + "y " * 400

    match = _match_for(text, PYTHON)

    assert match is not None
    assert "Python" in match.excerpt
    assert match.excerpt.startswith("…")
    assert match.excerpt.endswith("…")
    assert len(match.excerpt) <= 300


def test_excerpt_never_exceeds_the_column_budget() -> None:
    text = "Python " + "z" * 5000

    match = _match_for(text, PYTHON)

    assert match is not None
    assert len(match.excerpt) <= 300


def test_excerpt_is_a_verbatim_slice_of_the_source() -> None:
    """Evidence must be quoted, never authored — the excerpt has to
    appear in the original text (modulo whitespace collapsing)."""
    text = "Delivered a REST service with Python and PostgreSQL."

    match = _match_for(text, PYTHON)

    assert match is not None
    assert match.excerpt in " ".join(text.split())


# --- determinism and degenerate input ---------------------------------


def test_repeated_runs_return_identical_results() -> None:
    text = "Languages: Python, Go\nFrameworks: Next.js, Express\nCI/CD with Docker"

    first = find_skill_matches(text, TERMS)
    second = find_skill_matches(text, TERMS)

    assert [(m.skill_id, m.start, m.excerpt, m.confidence) for m in first] == [
        (m.skill_id, m.start, m.excerpt, m.confidence) for m in second
    ]


def test_results_are_ordered_by_position_in_the_document() -> None:
    matches = find_skill_matches("Python then JavaScript then Next.js", TERMS)

    assert [m.start for m in matches] == sorted(m.start for m in matches)


@pytest.mark.parametrize("text", ["", "   \n\t  "])
def test_empty_text_matches_nothing(text: str) -> None:
    assert find_skill_matches(text, TERMS) == []


def test_text_with_no_taxonomy_terms_matches_nothing() -> None:
    assert find_skill_matches("Barista and customer service professional.", TERMS) == []


def test_no_terms_matches_nothing() -> None:
    assert find_skill_matches("Python everywhere", []) == []


def test_every_match_kind_has_a_confidence() -> None:
    assert set(CONFIDENCE_BY_KIND) == set(MatchKind)


def test_compile_term_rejects_a_term_with_no_matchable_characters() -> None:
    with pytest.raises(ValueError, match="no matchable characters"):
        compile_term("  //  ")
