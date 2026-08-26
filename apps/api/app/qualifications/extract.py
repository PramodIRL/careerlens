"""Deterministic qualification extraction from resume text (Prompt 5.1b).

Turns the document a candidate already uploaded into the facts job
postings set bars against, so they never have to type them in.

PURE PARSING, SEPARATE FROM PERSISTENCE — the same split every other
extractor in this codebase has. `parse_qualifications` takes text and
returns values; `extract_qualifications_for_resume` decides what to
write.

DELIBERATELY NOT SHARED WITH app/eligibility/classify.py. That module
reads THRESHOLDS out of a job posting — "minimum CGPA 7.5" — and spends
most of its logic on negation and preference cues. This one reads VALUES
out of a resume — "CGPA: 8.2" — where the label is the whole signal and
there is nothing to negate. Same words, opposite job; fusing them would
mean a parser that has to know which kind of document it is holding.

NO LLM, NO EMBEDDINGS, NO SEMANTIC INFERENCE, NO NETWORK. Compiled
regex against a controlled vocabulary, and nothing else.

THE LABEL IS MANDATORY, EVERYWHERE. A resume is full of numbers —
phone numbers, dates, revenue figures, "7.5 million users", "3 years" in
a project description. None of them are qualifications, and the only
thing separating "CGPA: 8.2" from "8.2" is the word CGPA. Every pattern
here requires its label, which is why the false-positive tests pass
rather than by luck.

WHAT IT REFUSES TO DO. It never sums employment date ranges into a
years-of-experience figure, and never infers experience from job titles:
two jobs on a page is not two years, and computing it would be inference
dressed as extraction. Experience is 0 only when the resume says
"fresher" or "no experience" outright, and otherwise stays UNKNOWN.
"""

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate_qualification import CandidateQualification
from app.models.resume import Resume
from app.schemas.qualification import (
    NUMERIC_FACTS,
    Degree,
    FieldOfStudy,
    QualificationExtractionMethod,
    QualificationFactType,
)
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType

# How cleanly the pattern matched — NOT how good the qualification is.
# An explicit label next to its value is the strong case; a value read
# from a looser but still labelled construction is the weaker one.
_CONFIDENCE_LABELLED = Decimal("0.90")
_CONFIDENCE_CONTEXTUAL = Decimal("0.75")

_MAX_EXCERPT_CHARS = 500
_NUMBER = r"\d{1,3}(?:\.\d{1,2})?"


@dataclass(frozen=True)
class ParsedFact:
    """One qualification read off a resume, with the line it came from."""

    fact_type: QualificationFactType
    value_numeric: Decimal | None = None
    value_text: str | None = None
    value_scale: Decimal | None = None
    excerpt: str = ""
    confidence: Decimal = _CONFIDENCE_LABELLED


@dataclass
class QualificationSummary:
    """Per-run counts, returned for logging and asserted on in tests. A
    re-run over an unchanged resume must report zeroes for everything
    except `facts_found`."""

    facts_found: int = 0
    facts_written: int = 0
    facts_skipped_user_owned: int = 0


def _line_excerpt(text: str, position: int) -> str:
    """The verbatim LINE the value sits on, whitespace-collapsed.

    A line, not a sentence: resumes are laid out as labelled rows
    ("CGPA: 8.2"), not prose, and a sentence window would swallow the
    whole education block.
    """
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end == -1:
        end = len(text)
    return " ".join(text[start:end].split())[:_MAX_EXCERPT_CHARS]


# --- CGPA ---------------------------------------------------------------
#
# The label is mandatory in both orders a resume writes it. An optional
# "/10" or "out of 10" is captured when present and left absent when
# not: the excerpt must keep what the document actually said.
_CGPA = re.compile(
    rf"\b(?:cgpa|gpa|c\.g\.p\.a)\b[\s:\-–]*({_NUMBER})"
    rf"(?:\s*(?:/|out\s+of)\s*({_NUMBER}))?",
    re.IGNORECASE,
)

# --- School marks -------------------------------------------------------
#
# Each board label paired with its own percentage on the same line.
# "Class X: 91%" and "SSC - 91%" are the shapes that actually appear.
_CLASS_10_LABEL = r"(?:class\s*(?:10|x)\b|10th|tenth|ssc|matriculation|secondary\s+school)"
_CLASS_12_LABEL = (
    r"(?:class\s*(?:12|xii)\b|12th|twelfth|hsc|higher\s+secondary|intermediate|senior\s+secondary)"
)
_PERCENT = rf"({_NUMBER})\s*(?:%|percent|per\s?cent)"
_CLASS_10 = re.compile(rf"{_CLASS_10_LABEL}[^\n%]{{0,40}}?{_PERCENT}", re.IGNORECASE)
_CLASS_12 = re.compile(rf"{_CLASS_12_LABEL}[^\n%]{{0,40}}?{_PERCENT}", re.IGNORECASE)

# --- Degree -------------------------------------------------------------
#
# Longest spellings first so "bachelor of technology" is not consumed by
# a shorter alternative. Every abbreviation needs its punctuation or a
# full word: a bare "be" and a bare "me" are ordinary English, and
# "ME" in "CONTACT ME" must never become a Master of Engineering.
_DEGREE_TERMS: tuple[tuple[str, Degree], ...] = (
    (r"bachelor(?:'s)?\s+of\s+technology", Degree.BTECH),
    (r"bachelor(?:'s)?\s+of\s+engineering", Degree.BE),
    (r"bachelor(?:'s)?\s+of\s+science", Degree.BSC),
    (r"bachelor(?:'s)?\s+of\s+computer\s+applications", Degree.BCA),
    (r"master(?:'s)?\s+of\s+technology", Degree.MTECH),
    (r"master(?:'s)?\s+of\s+engineering", Degree.ME),
    (r"master(?:'s)?\s+of\s+science", Degree.MSC),
    (r"master(?:'s)?\s+of\s+computer\s+applications", Degree.MCA),
    (r"master(?:'s)?\s+of\s+business\s+administration", Degree.MBA),
    (r"b\.?\s?tech\b", Degree.BTECH),
    (r"m\.?\s?tech\b", Degree.MTECH),
    (r"b\.e\.?(?!\w)", Degree.BE),
    (r"m\.e\.?(?!\w)", Degree.ME),
    (r"b\.?\s?sc\b", Degree.BSC),
    (r"m\.?\s?sc\b", Degree.MSC),
    (r"b\.?\s?c\.?a\b", Degree.BCA),
    (r"m\.?\s?c\.?a\b", Degree.MCA),
    (r"m\.?\s?b\.?a\b", Degree.MBA),
    (r"ph\.?\s?d\b|doctorate", Degree.PHD),
    (r"diploma", Degree.DIPLOMA),
)

# --- Field of study -----------------------------------------------------
#
# Longest first again, so "computer science" is not shortened to
# "computer engineering" by an overlapping alternative. Abbreviations
# are limited to ones with no common English reading — "cse" and "ece"
# are safe, a bare "it" or "cs" is not.
_FIELD_TERMS: tuple[tuple[str, FieldOfStudy], ...] = (
    (r"artificial\s+intelligence(?:\s*(?:&|and|/)\s*machine\s+learning)?", FieldOfStudy.AI_ML),
    (r"machine\s+learning", FieldOfStudy.AI_ML),
    (r"\bai\s*(?:&|and|/)\s*ml\b|\bai\s*/\s*ml\b|\baiml\b", FieldOfStudy.AI_ML),
    (r"computer\s+science(?:\s*(?:&|and)\s*engineering)?", FieldOfStudy.COMPUTER_SCIENCE),
    (r"computer\s+engineering", FieldOfStudy.COMPUTER_ENGINEERING),
    (r"information\s+technology", FieldOfStudy.INFORMATION_TECHNOLOGY),
    (r"information\s+science", FieldOfStudy.INFORMATION_SCIENCE),
    (r"data\s+science", FieldOfStudy.DATA_SCIENCE),
    (r"electronics(?:\s*(?:&|and)\s*communication)?", FieldOfStudy.ELECTRONICS),
    (r"electrical(?:\s+engineering)?", FieldOfStudy.ELECTRICAL),
    (r"mechatronics", FieldOfStudy.MECHATRONICS),
    (r"mechanical(?:\s+engineering)?", FieldOfStudy.MECHANICAL),
    (r"civil(?:\s+engineering)?", FieldOfStudy.CIVIL),
    (r"chemical(?:\s+engineering)?", FieldOfStudy.CHEMICAL),
    (r"aerospace(?:\s+engineering)?|aeronautical", FieldOfStudy.AEROSPACE),
    (r"automobile(?:\s+engineering)?|automotive\s+engineering", FieldOfStudy.AUTOMOBILE),
    (r"instrumentation", FieldOfStudy.INSTRUMENTATION),
    (r"industrial\s+(?:iot|engineering)|internet\s+of\s+things", FieldOfStudy.INDUSTRIAL_IOT),
    (r"bio\s?technology", FieldOfStudy.BIOTECHNOLOGY),
    (r"mathematics|\bmaths\b", FieldOfStudy.MATHEMATICS),
    (r"\bcse\b", FieldOfStudy.COMPUTER_SCIENCE),
    (r"\bece\b", FieldOfStudy.ELECTRONICS),
    (r"\beee\b", FieldOfStudy.ELECTRICAL),
)

_DEGREE_PATTERN = re.compile(
    "|".join(f"(?P<d{i}>{term})" for i, (term, _) in enumerate(_DEGREE_TERMS)), re.IGNORECASE
)
_DEGREE_BY_GROUP = {f"d{i}": value for i, (_, value) in enumerate(_DEGREE_TERMS)}
_FIELD_PATTERN = re.compile(
    "|".join(f"(?P<f{i}>{term})" for i, (term, _) in enumerate(_FIELD_TERMS)), re.IGNORECASE
)
_FIELD_BY_GROUP = {f"f{i}": value for i, (_, value) in enumerate(_FIELD_TERMS)}

# --- Graduation year ----------------------------------------------------
#
# A year is only a graduation year inside an education context. A resume
# carries plenty of other four-digit numbers — employment ranges,
# founding dates, certification years — and none of them say when this
# person leaves university.
_GRAD_CONTEXT = re.compile(
    r"\b(?:graduat\w*|batch|class\s+of|expected|passing\s+out|pass[- ]?out|"
    r"b\.?\s?tech|m\.?\s?tech|b\.e\.|bachelor|master|degree|university|college|institute)\b",
    re.IGNORECASE,
)
_YEAR = r"(?:19|20)\d{2}"
_YEAR_RANGE = re.compile(rf"({_YEAR})\s*(?:-|–|—|to)\s*({_YEAR})")
_YEAR_SINGLE = re.compile(rf"\b({_YEAR})\b")

# --- Experience ---------------------------------------------------------
#
# Two shapes only, both explicit. Anything else stays UNKNOWN.
_FRESHER = re.compile(
    r"\bfresher\b|\bno\s+(?:prior\s+|work\s+|professional\s+)?experience\b"
    r"|\bwithout\s+(?:any\s+)?(?:prior\s+)?experience\b",
    re.IGNORECASE,
)
_EXPERIENCE_YEARS = re.compile(
    rf"({_NUMBER})\s*\+?\s*(?:years?|yrs?)\b[^\n.]{{0,30}}?\bexperien\w*",
    re.IGNORECASE,
)
_EXPERIENCE_YEARS_REVERSED = re.compile(
    rf"\bexperien\w*[^\n.]{{0,20}}?\b(?:of\s+)?({_NUMBER})\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)


def _decimal(raw: str) -> Decimal:
    return Decimal(raw)


def _parse_cgpa(text: str) -> ParsedFact | None:
    """The candidate's CGPA, with its scale only when the resume says.

    The DEFAULT SCALE OF 10 IS NOT APPLIED HERE. Storing a fabricated
    scale would make the row claim the document said something it did
    not; the default belongs at comparison time
    (app/eligibility/resolve.py), where it is visible as a product rule
    rather than baked into evidence. So "CGPA: 8.2" stores value 8.2,
    scale NULL, excerpt "CGPA: 8.2" — and still compares as 8.2/10.
    """
    match = _CGPA.search(text)
    if match is None:
        return None
    value = _decimal(match.group(1))
    scale = _decimal(match.group(2)) if match.group(2) else None
    if scale is not None and (scale <= 0 or value > scale):
        # "8.2 out of 4" is a misparse, not a discovery.
        return None
    # With no stated scale the only sanity bound available is the
    # default the comparison will use. A "CGPA" of 85 is somebody
    # writing a percentage.
    if scale is None and value > 10:
        return None
    return ParsedFact(
        fact_type=QualificationFactType.CGPA,
        value_numeric=value,
        value_scale=scale,
        excerpt=_line_excerpt(text, match.start()),
    )


def _parse_percentage(
    text: str, pattern: re.Pattern[str], fact_type: QualificationFactType
) -> ParsedFact | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = _decimal(match.group(1))
    if value > 100:
        return None
    return ParsedFact(
        fact_type=fact_type,
        value_numeric=value,
        excerpt=_line_excerpt(text, match.start()),
    )


def _parse_vocabulary(
    text: str,
    pattern: re.Pattern[str],
    by_group: dict[str, Degree] | dict[str, FieldOfStudy],
    fact_type: QualificationFactType,
) -> ParsedFact | None:
    """The FIRST match wins, and that is the point.

    A resume lists education newest-first at the top of its education
    block, so the first degree named is the highest one held. Scanning
    for "the best" would mean ranking degrees, which is a judgement this
    module has no business making.
    """
    match = pattern.search(text)
    if match is None or match.lastgroup is None:
        return None
    value = by_group.get(match.lastgroup)
    if value is None:  # pragma: no cover - defensive
        return None
    return ParsedFact(
        fact_type=fact_type,
        value_text=value.value,
        excerpt=_line_excerpt(text, match.start()),
    )


def _parse_graduation_year(text: str) -> ParsedFact | None:
    """A year, but only from a line that is about education.

    Line-scoped rather than document-scoped: an employment range on a
    different line is not a graduation date, however close it sits.
    """
    for line in _lines(text):
        offset, content = line
        if not _GRAD_CONTEXT.search(content):
            continue
        span = _YEAR_RANGE.search(content)
        if span:
            # "2022 - 2026" on a degree line: the later endpoint is when
            # the degree finishes.
            return ParsedFact(
                fact_type=QualificationFactType.GRADUATION_YEAR,
                value_numeric=_decimal(span.group(2)),
                excerpt=_line_excerpt(text, offset),
                confidence=_CONFIDENCE_CONTEXTUAL,
            )
        years = _YEAR_SINGLE.findall(content)
        if len(years) == 1:
            return ParsedFact(
                fact_type=QualificationFactType.GRADUATION_YEAR,
                value_numeric=_decimal(years[0]),
                excerpt=_line_excerpt(text, offset),
            )
        # Two or more loose years on one line with no range separator is
        # ambiguous — which one is the graduation?
    return None


def _parse_experience(text: str) -> ParsedFact | None:
    """Years of experience, from an explicit statement only.

    "Fresher" and "no prior experience" are real assertions of ZERO and
    are read as such. A numeric statement is read at face value. Nothing
    else produces a figure: employment date ranges are never summed, and
    job titles are never counted, because two jobs on a page is not two
    years and pretending otherwise invents a career.
    """
    fresher = _FRESHER.search(text)
    if fresher is not None:
        return ParsedFact(
            fact_type=QualificationFactType.YEARS_EXPERIENCE,
            value_numeric=Decimal("0"),
            excerpt=_line_excerpt(text, fresher.start()),
        )
    for pattern in (_EXPERIENCE_YEARS, _EXPERIENCE_YEARS_REVERSED):
        match = pattern.search(text)
        if match is not None:
            return ParsedFact(
                fact_type=QualificationFactType.YEARS_EXPERIENCE,
                value_numeric=_decimal(match.group(1)),
                excerpt=_line_excerpt(text, match.start()),
            )
    return None


def _lines(text: str) -> list[tuple[int, str]]:
    """Every line as (offset, content)."""
    spans: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        spans.append((offset, line))
        offset += len(line)
    return spans


def parse_qualifications(text: str) -> list[ParsedFact]:
    """Every qualification fact the resume states, at most one per type.

    PURE — no database — so every rule above is testable on its own. A
    fact the resume does not state is simply absent from the result;
    there is no placeholder, no zero and no default, because absence has
    to reach the store as absence.
    """
    found = [
        _parse_cgpa(text),
        _parse_percentage(text, _CLASS_10, QualificationFactType.CLASS_10_PERCENTAGE),
        _parse_percentage(text, _CLASS_12, QualificationFactType.CLASS_12_PERCENTAGE),
        _parse_vocabulary(
            text, _DEGREE_PATTERN, _DEGREE_BY_GROUP, QualificationFactType.HIGHEST_DEGREE
        ),
        _parse_vocabulary(
            text, _FIELD_PATTERN, _FIELD_BY_GROUP, QualificationFactType.FIELD_OF_STUDY
        ),
        _parse_graduation_year(text),
        _parse_experience(text),
    ]
    return [fact for fact in found if fact is not None]


async def extract_qualifications_for_resume(
    db: AsyncSession, resume_id: uuid.UUID
) -> QualificationSummary:
    """Read one resume's qualification facts and reconcile them into the
    candidate's store.

    COMMITS ONCE, at the end, matching `extract_skills_for_resume`: the
    worker calls both as independent units of work and owns no outer
    transaction spanning them, so each has to land its own writes. One
    commit rather than per-fact, because reconciliation must never be
    observed half-applied.

    THE OVERRIDE RULE, which is the whole reason this function is not a
    plain upsert:

      * no row yet          -> insert as `suggested`
      * `suggested`, same   -> nothing is written at all
      * `suggested`, changed-> refreshed, so a corrected resume corrects
      * `confirmed`         -> UNTOUCHED
      * `rejected`          -> UNTOUCHED

    `confirmed` and `rejected` are the user's word about their own
    record and outrank the extractor permanently. A manual edit writes
    `confirmed` (app/qualifications/store.py), which is what makes a
    user's value distinguishable from an unreviewed guess — without
    that, refreshing `suggested` rows would silently overwrite exactly
    the corrections this rule exists to protect. Same shape, and the
    same reasoning, as `ensure_candidate_skills`' ON CONFLICT DO NOTHING
    in app/skill_extraction.py.
    """
    summary = QualificationSummary()

    resume = await db.get(Resume, resume_id)
    if resume is None or not resume.extracted_text:
        return summary

    parsed = parse_qualifications(resume.extracted_text)
    summary.facts_found = len(parsed)
    if not parsed:
        return summary

    existing = {
        row.fact_type: row
        for row in (
            await db.scalars(
                select(CandidateQualification).where(
                    CandidateQualification.user_id == resume.user_id,
                    CandidateQualification.fact_type.in_([fact.fact_type.value for fact in parsed]),
                )
            )
        ).all()
    }

    for fact in parsed:
        row = existing.get(fact.fact_type.value)
        if row is not None and row.status != CandidateSkillStatus.SUGGESTED.value:
            # The user has spoken about this fact. Their decision stands.
            summary.facts_skipped_user_owned += 1
            continue

        numeric = fact.value_numeric if fact.fact_type.value in NUMERIC_FACTS else None
        text_value = fact.value_text if fact.fact_type.value not in NUMERIC_FACTS else None

        if row is None:
            db.add(
                CandidateQualification(
                    user_id=resume.user_id,
                    fact_type=fact.fact_type.value,
                    value_numeric=numeric,
                    value_text=text_value,
                    value_scale=fact.value_scale,
                    status=CandidateSkillStatus.SUGGESTED.value,
                    source_type=EvidenceSourceType.RESUME.value,
                    source_identifier=str(resume.id),
                    excerpt=fact.excerpt,
                    extraction_method=QualificationExtractionMethod.RESUME_LABEL_MATCH.value,
                    confidence=fact.confidence,
                )
            )
            summary.facts_written += 1
            continue

        # An unreviewed row. Refresh it, but only when something
        # actually changed — otherwise a reprocess would bump
        # `updated_at` on byte-identical data, which is the difference
        # between "duplicate-free" and "idempotent".
        unchanged = (
            row.value_numeric == numeric
            and row.value_text == text_value
            and row.value_scale == fact.value_scale
            and row.excerpt == fact.excerpt
            and row.source_identifier == str(resume.id)
        )
        if unchanged:
            continue
        row.value_numeric = numeric
        row.value_text = text_value
        row.value_scale = fact.value_scale
        row.source_type = EvidenceSourceType.RESUME.value
        row.source_identifier = str(resume.id)
        row.excerpt = fact.excerpt
        row.extraction_method = QualificationExtractionMethod.RESUME_LABEL_MATCH.value
        row.confidence = fact.confidence
        summary.facts_written += 1

    await db.commit()
    return summary
