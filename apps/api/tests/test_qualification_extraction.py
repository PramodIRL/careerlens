"""Deterministic qualification extraction from resume text (Prompt 5.1b).

Pure parsing — no database. The load-bearing tests here:

  * the label is mandatory, so ordinary resume numbers never become
    qualifications
  * an unstated CGPA scale stays unstated in the EXCERPT, while the
    comparison still defaults to 10 elsewhere
  * experience is 0 only when the resume says so outright, and is never
    summed from employment history
"""

from decimal import Decimal

import pytest

from app.qualifications.extract import parse_qualifications
from app.schemas.qualification import QualificationFactType as F


def _facts(text: str) -> dict[str, object]:
    return {
        fact.fact_type.value: (
            fact.value_numeric if fact.value_numeric is not None else fact.value_text
        )
        for fact in parse_qualifications(text)
    }


def _fact(text: str, fact_type: F):
    return next((f for f in parse_qualifications(text) if f.fact_type is fact_type), None)


# --- CGPA ---------------------------------------------------------------


def test_a_stated_scale_is_recorded() -> None:
    fact = _fact("CGPA: 8.2/10", F.CGPA)
    assert fact is not None
    assert fact.value_numeric == Decimal("8.2")
    assert fact.value_scale == Decimal("10")


def test_an_unstated_scale_stays_unstated_on_the_row() -> None:
    """The default of 10 is a COMPARISON rule, applied in
    app/eligibility/resolve.py. Writing a fabricated "/10" here would
    make the stored evidence claim the resume said something it did
    not."""
    fact = _fact("CGPA: 8.2", F.CGPA)
    assert fact is not None
    assert fact.value_numeric == Decimal("8.2")
    assert fact.value_scale is None
    assert fact.excerpt == "CGPA: 8.2"


def test_a_four_point_scale_is_recorded_as_four() -> None:
    fact = _fact("CGPA: 3.6/4.0", F.CGPA)
    assert fact is not None
    assert fact.value_scale == Decimal("4.0")


def test_out_of_is_read_as_a_scale() -> None:
    fact = _fact("CGPA 8.2 out of 10", F.CGPA)
    assert fact is not None
    assert fact.value_scale == Decimal("10")


# --- school marks -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Class X: 91%", "Class 10: 91%", "10th - 91%", "SSC: 91 percent"],
)
def test_class_10_forms(text: str) -> None:
    assert _facts(text)[F.CLASS_10_PERCENTAGE.value] == Decimal("91")


@pytest.mark.parametrize(
    "text",
    ["Class XII: 88%", "Class 12: 88%", "12th - 88%", "HSC: 88%", "Higher Secondary: 88%"],
)
def test_class_12_forms(text: str) -> None:
    assert _facts(text)[F.CLASS_12_PERCENTAGE.value] == Decimal("88")


def test_both_school_percentages_are_read_independently() -> None:
    facts = _facts("Class X: 91%\nClass XII: 88%")
    assert facts[F.CLASS_10_PERCENTAGE.value] == Decimal("91")
    assert facts[F.CLASS_12_PERCENTAGE.value] == Decimal("88")


# --- degree and field ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B.Tech in Computer Science", "btech"),
        ("BTech, Computer Science", "btech"),
        ("Bachelor of Technology", "btech"),
        ("B.E. Mechanical", "be"),
        ("M.Tech, Data Science", "mtech"),
        ("M.E. in Structural Engineering", "me"),
        ("MBA, Finance", "mba"),
        ("MCA", "mca"),
        ("PhD candidate", "phd"),
    ],
)
def test_degree_normalization(text: str, expected: str) -> None:
    assert _facts(text)[F.HIGHEST_DEGREE.value] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B.Tech in Computer Science", "computer_science"),
        ("B.Tech CSE", "computer_science"),
        ("B.Tech in Computer Engineering", "computer_engineering"),
        ("B.Tech, Information Technology", "information_technology"),
        ("B.Tech in Information Science", "information_science"),
        ("B.Tech in Artificial Intelligence and Machine Learning", "ai_ml"),
        ("B.Tech AI/ML", "ai_ml"),
        ("B.Tech in Data Science", "data_science"),
        ("B.Tech ECE", "electronics"),
        ("B.Tech in Electronics and Communication", "electronics"),
        ("B.Tech Electrical Engineering", "electrical"),
        ("B.Tech Mechanical Engineering", "mechanical"),
        ("B.Tech in Mechatronics", "mechatronics"),
        ("B.Tech Civil Engineering", "civil"),
        ("B.Tech Chemical Engineering", "chemical"),
        ("B.Tech Aerospace Engineering", "aerospace"),
        ("B.Tech Automobile Engineering", "automobile"),
        ("B.Tech in Instrumentation", "instrumentation"),
        ("B.Tech in Industrial IoT", "industrial_iot"),
        ("B.Tech Biotechnology", "biotechnology"),
    ],
)
def test_field_normalization(text: str, expected: str) -> None:
    assert _facts(text)[F.FIELD_OF_STUDY.value] == expected


@pytest.mark.parametrize("text", ["Please contact me at the address above.", "Ping me on Slack."])
def test_ordinary_english_never_becomes_a_degree(text: str) -> None:
    """A bare "me" and a bare "be" are the commonest words in a resume.
    Only the punctuated abbreviations count."""
    assert F.HIGHEST_DEGREE.value not in _facts(text)


# --- graduation year ----------------------------------------------------


def test_a_year_on_a_degree_line_is_the_graduation_year() -> None:
    assert _facts("B.Tech in Computer Science, Invented Institute (2026)")[
        F.GRADUATION_YEAR.value
    ] == Decimal("2026")


def test_a_range_on_a_degree_line_takes_the_later_endpoint() -> None:
    assert _facts("B.Tech, Invented Institute (2022 - 2026)")[F.GRADUATION_YEAR.value] == Decimal(
        "2026"
    )


def test_expected_graduation_is_recognised() -> None:
    assert _facts("Expected graduation: 2027")[F.GRADUATION_YEAR.value] == Decimal("2027")


# --- experience ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 years of experience in backend development", Decimal("2")),
        ("3+ years experience", Decimal("3")),
        ("Experience: 5 years", Decimal("5")),
        ("Fresher", Decimal("0")),
        ("Fresher - seeking a first role.", Decimal("0")),
        ("No prior experience", Decimal("0")),
        ("No experience", Decimal("0")),
    ],
)
def test_experience_statements(text: str, expected: Decimal) -> None:
    assert _facts(text)[F.YEARS_EXPERIENCE.value] == expected


def test_experience_is_never_summed_from_employment_history() -> None:
    """Two jobs on a page is not two years. Computing it would be
    inference dressed as extraction, so the fact stays UNKNOWN."""
    resume = (
        "EXPERIENCE\n"
        "Backend Engineer, Fictional Widgets (2023 - 2025)\n"
        "Junior Developer, Imaginary Interfaces (2021 - 2023)\n"
    )
    assert F.YEARS_EXPERIENCE.value not in _facts(resume)


# --- false positives ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("7.5 years of tenure at the firm", F.CGPA),
        ("Call 555-0142 or 555-0199", F.CGPA),
        ("Served 7.5 million users", F.CGPA),
        ("Reduced latency by 8.2 ms", F.CGPA),
        ("CGPA 85 listed by mistake", F.CGPA),
        ("Acme Corp, founded 1998", F.GRADUATION_YEAR),
        ("Employment: 2021 - 2023 at Fictional Widgets", F.GRADUATION_YEAR),
        ("Led 3 teams across 2 offices", F.YEARS_EXPERIENCE),
        ("Improved coverage to 91%", F.CLASS_10_PERCENTAGE),
    ],
)
def test_ordinary_resume_numbers_are_not_qualifications(text: str, forbidden: F) -> None:
    """The label is the whole signal. Without it these are just numbers
    in a document, and reading requirements into them invents facts
    about a person."""
    assert forbidden.value not in _facts(text)


def test_a_resume_with_no_education_block_yields_nothing() -> None:
    assert parse_qualifications("Backend engineer. Built things with Python.") == []


def test_parsing_is_deterministic() -> None:
    resume = "B.Tech in Computer Science (2026)\nCGPA: 8.2\nClass X: 91%\nFresher"
    assert parse_qualifications(resume) == parse_qualifications(resume)


def test_every_fact_quotes_a_verbatim_resume_line() -> None:
    """Evidence-First: a reader can find each excerpt on the page by
    eye. Nothing is summarised or reworded."""
    resume = "EDUCATION\nB.Tech in Computer Science (2026)\nCGPA: 8.2\nClass X: 91%\n"
    collapsed = " ".join(resume.split())
    for fact in parse_qualifications(resume):
        assert fact.excerpt in collapsed, fact.excerpt
