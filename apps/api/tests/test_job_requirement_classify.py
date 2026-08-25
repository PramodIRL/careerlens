"""Pure classification rules for job requirements (Prompt 4.2).

No database, no fixtures — text and an offset in, a level out. The same
separation tests/test_skill_matching.py has from
tests/test_skill_extraction.py.

The load-bearing tests here, which should not be softened:

  * the brief's explicit negative cases — "Python is not required" and
    "No Python experience necessary" must be MENTIONED, never required
  * "We require Python. Docker is a plus." must not bleed the
    requirement across the sentence boundary onto Docker
  * repeated occurrences take the STRONGEST level
  * the excerpt is a real slice of the source text
"""

import pytest

from app.job_requirements.classify import (
    RequirementLevel,
    classify_at,
    clause_around,
    clause_excerpt,
    strongest,
)


def _level(text: str, skill: str) -> str:
    """Classify the skill where it appears in `text`."""
    return classify_at(text, text.index(skill)).value


# --- the brief's worked examples --------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We require Python.", "required"),
        ("Python is required.", "required"),
        ("Python experience preferred.", "preferred"),
        ("Python is a plus.", "preferred"),
        ("Experience with Python.", "mentioned"),
        ("Knowledge of Python required.", "required"),
    ],
)
def test_the_briefs_worked_examples(text: str, expected: str) -> None:
    assert _level(text, "Python") == expected


# --- negation: checked BEFORE positive cues ---------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Python is not required.",
        "Python is not essential.",
        "Python is not necessary for this role.",
        "No Python experience necessary.",
        "No Python experience required.",
        "No prior Python experience needed.",
        "This role does not require Python.",
        "We don't require Python.",
        "You can succeed without Python.",
        "Python is not a requirement.",
    ],
)
def test_negated_wording_is_never_required(text: str) -> None:
    """THE COUNTER-EXAMPLES THE BRIEF CALLS OUT.

    Every one of these contains a required cue as a SUBSTRING
    ("required", "necessary", "require"), so any cue-first design
    classifies them exactly backwards. Negation running first is what
    makes them come out right.
    """
    assert _level(text, "Python") == "mentioned"


def test_negation_does_not_leak_into_a_neighbouring_clause() -> None:
    """The negation applies to its own clause, not the next one."""
    text = "Python is not required. Docker is required."
    assert _level(text, "Python") == "mentioned"
    assert _level(text, "Docker") == "required"


# --- clause scoping: no bleed across boundaries -----------------------


def test_a_requirement_does_not_bleed_across_a_sentence() -> None:
    """The brief's explicit case: Docker must NOT become required."""
    text = "We require Python. Docker is a plus."
    assert _level(text, "Python") == "required"
    assert _level(text, "Docker") == "preferred"


def test_a_requirement_does_not_bleed_across_a_comma() -> None:
    """A posting routinely packs separate claims into one sentence, so
    the clause — not the sentence — is the unit of a single claim."""
    text = "Python is required, Docker is a plus."
    assert _level(text, "Python") == "required"
    assert _level(text, "Docker") == "preferred"


def test_the_full_example_from_the_brief() -> None:
    """All on ONE LINE, which is what makes the matcher's line-scoped
    excerpt unusable for classification — it would mark all four
    required."""
    text = (
        "We require Python and PostgreSQL. Experience with FastAPI is preferred. Docker is a plus."
    )
    assert _level(text, "Python") == "required"
    assert _level(text, "PostgreSQL") == "required"
    assert _level(text, "FastAPI") == "preferred"
    assert _level(text, "Docker") == "preferred"


def test_bullet_lines_are_classified_independently() -> None:
    text = "Requirements:\n- Python is required\n- Docker is nice to have\n- We use Redis"
    assert _level(text, "Python") == "required"
    assert _level(text, "Docker") == "preferred"
    assert _level(text, "Redis") == "mentioned"


# --- cue coverage ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Python is required",
        "Python is essential",
        "You must have Python",
        "Python is mandatory",
        "You will need Python",
        "We need Python",
        "Proficiency in Python",
        "This role requires Python",
    ],
)
def test_required_wording(text: str) -> None:
    assert _level(text, "Python") == "required"


@pytest.mark.parametrize(
    "text",
    [
        "Python preferred",
        "Python is nice to have",
        "Python is a bonus",
        "Python is a plus",
        "Ideally you know Python",
        "Python is desirable",
        "Python would be great",
        "Python is an advantage",
    ],
)
def test_preferred_wording(text: str) -> None:
    assert _level(text, "Python") == "preferred"


@pytest.mark.parametrize(
    "text",
    [
        "Experience with Python",
        "Our stack is Python and Django",
        "The team writes Python daily",
        "Python",
    ],
)
def test_bare_mentions_stay_mentioned(text: str) -> None:
    """MENTIONED is the default, not a weak "required". Reading intent
    into the posting's silence is exactly the guessing to avoid."""
    assert _level(text, "Python") == "mentioned"


def test_an_ambiguous_window_with_both_cues_resolves_to_preferred() -> None:
    """Deterministic and documented: clause splitting already separates
    ordinary "X required, Y preferred" phrasing, so a window with BOTH
    is genuinely ambiguous — and under-claiming is the safer error,
    because Prompt 4.3 turns `required` into a skill gap."""
    text = "Python is required although preferred candidates also know it"
    assert _level(text, "Python") == "preferred"


# --- strongest-wins across occurrences ---------------------------------


def test_strongest_prefers_required() -> None:
    assert (
        strongest(
            [RequirementLevel.MENTIONED, RequirementLevel.REQUIRED, RequirementLevel.PREFERRED]
        )
        is RequirementLevel.REQUIRED
    )


def test_strongest_prefers_preferred_over_mentioned() -> None:
    assert (
        strongest([RequirementLevel.MENTIONED, RequirementLevel.PREFERRED])
        is RequirementLevel.PREFERRED
    )


def test_strongest_of_one_is_itself() -> None:
    assert strongest([RequirementLevel.MENTIONED]) is RequirementLevel.MENTIONED


# --- excerpts ----------------------------------------------------------


def test_the_excerpt_is_a_real_slice_of_the_source() -> None:
    text = "We require Python and PostgreSQL. Docker is a plus."
    excerpt = clause_excerpt(text, text.index("Docker"))
    assert excerpt == "Docker is a plus"
    # ...and it really is in the source, not generated prose.
    assert excerpt in text


def test_the_excerpt_collapses_whitespace_without_rewording() -> None:
    """Runs of spaces and tabs collapse to single spaces so a stored
    excerpt reads as one line — but no word is changed, added or
    reordered. A NEWLINE is not collapsed here: it terminates a clause
    (see test_a_newline_ends_a_clause), which is what lets bullet lines
    be classified independently."""
    text = "Python \t  is    required"
    assert clause_excerpt(text, text.index("Python")) == "Python is required"


def test_a_newline_ends_a_clause() -> None:
    """A deliberate consequence, and a known limitation worth naming: a
    header-then-list posting ("We require:\nPython") classifies Python
    as MENTIONED, because the header is a separate clause. Carrying a
    header's meaning onto following lines is the kind of inference that
    produces exactly the cross-clause bleed this design rejects, so the
    conservative reading wins."""
    text = "We require:\nPython"
    assert classify_at(text, text.index("Python")) is RequirementLevel.MENTIONED


def test_the_excerpt_is_capped() -> None:
    text = "Python " + "x" * 900 + " required"
    assert len(clause_excerpt(text, 0)) <= 500


def test_clause_bounds_are_the_narrowest_containing_span() -> None:
    text = "First clause. Python is required. Third clause."
    start, end = clause_around(text, text.index("Python"))
    assert text[start:end].strip() == "Python is required"


def test_text_with_no_clause_terminator_is_one_clause() -> None:
    text = "Python is required"
    start, end = clause_around(text, 0)
    assert (start, end) == (0, len(text))


# --- case handling -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["PYTHON IS REQUIRED", "Python Is Required", "python is REQUIRED"],
)
def test_classification_is_case_insensitive(text: str) -> None:
    assert classify_at(text, 0) is RequirementLevel.REQUIRED
