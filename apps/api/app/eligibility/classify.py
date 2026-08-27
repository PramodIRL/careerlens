"""Deterministic parsing of qualification bars out of a job description
(Prompt 5.1a).

PURE: text in, parsed requirements out. No database, no network, NO LLM,
no embeddings, no semantic inference — every rule here is a compiled
regex over a curated vocabulary, testable without fixtures. Same split
app/job_requirements/classify.py has from its persistence layer.

WHY A SEPARATE CLASSIFIER FROM THE SKILL ONE. That module answers "how
strongly is this skill wanted", scanning for cue words around a term
that already matched a taxonomy. This one answers "what threshold is
being set", and the threshold IS the match — a number, a scale, a
degree name. There is no shared matcher to reuse, only the clause-window
helpers, which are imported rather than reimplemented.

BIASED TOWARD WRITING NOTHING. Where a phrasing is ambiguous, no row is
produced at all. That asymmetry is deliberate and it is not the same
trade the skill side makes: a missed skill requirement understates a
gap, but a WRONG eligibility requirement tells a person they cannot
apply for a job they are entitled to. Silence is the cheaper error.

SENTENCE-SCOPED, NOT CLAUSE-SCOPED — the opposite of
app/job_requirements/classify.py, and deliberately so. That module
splits on commas and colons because one sentence routinely carries
several independent skill claims ("Python is required, Docker is a
plus"). Eligibility is the other shape: a sentence states ONE bar, and
its parts are spread across exactly the punctuation a clause split would
cut through — "Class X: 92%, Class XII: 88%" needs the marker and the
number in the same window to pair them at all.

A period is a boundary only when it ENDS a sentence. The skill
classifier's rule treats every "." as one, which is harmless when the
match is a taxonomy name but fatal here: it splits 7.5 into "7" and "5",
and B.Tech into "B" and "Tech", destroying the very tokens this module
exists to read.

THE CGPA SCALE: EXPLICIT WINS, OTHERWISE 10. "CGPA: 8.2/10" gives a
scale of 10 and "3.6/4.0" gives 4, because the posting said so. When no
scale is written at all — "Minimum CGPA 7.5" — the scale defaults to 10,
which is the product rule for this domain: a 10-point scale is the
overwhelmingly common case in the postings this feature targets, and
holding the requirement at UNDETERMINED would leave the single most
common phrasing permanently unresolvable.

The default is STORED, not applied silently at comparison time, so the
requirement row and the API response both say "7.5/10" and a reader can
see exactly what was assumed on their behalf. An explicitly written
scale is never overridden by it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.schemas.eligibility import Comparator, EligibilityRequirementType
from app.schemas.qualification import Degree, FieldOfStudy

# Requirement strength. Eligibility has no "mentioned" tier: a posting
# that names a CGPA floor at all has set one, however casually it is
# worded. The distinction that matters is whether it is a hard bar or a
# preference, so only two levels exist here.
LEVEL_REQUIRED = "required"
LEVEL_PREFERRED = "preferred"

# How cleanly the pattern matched — NOT how strongly the posting wants
# it. Mirrors the meaning `confidence` already carries on the skill
# side, so a reader does not have to learn a second sense of the word.
_CONFIDENCE_EXPLICIT = Decimal("0.90")
_CONFIDENCE_INFERRED_SUBJECT = Decimal("0.75")

# Used when a posting states a CGPA with no scale. Stored on the row
# rather than applied at comparison time, so the assumption is visible
# in the API response instead of hidden inside the resolver.
DEFAULT_CGPA_SCALE = Decimal("10")

_PREFERRED_CUES: tuple[str, ...] = (
    "preferred",
    "preferably",
    "nice to have",
    "ideally",
    "desirable",
    "a plus",
    "bonus",
    "good to have",
)

# Phrases that DEFEAT an eligibility reading. Checked before anything
# else, because each contains the pattern it negates: "no minimum CGPA"
# holds a perfectly good CGPA match. A negated clause yields no row —
# not a relaxed one — because "we do not screen on CGPA" is the absence
# of a bar, not a bar at zero.
# Direction cues for a graduation-year clause: "2024 or later" is a
# FLOOR, "2024 or earlier" a CEILING, and a bare "2024" a specific batch.
# Without these every phrasing collapsed to `eq`, so only a candidate
# whose year exactly equalled the bar could ever satisfy the
# requirement — "2024 or later" rejected 2027.
#
# INCLUSIVE PHRASINGS ONLY. "or later" means >= 2024, and every cue here
# has that same "this year counts too" sense. Bare exclusive forms
# ("before 2024", "after 2024") mean strictly < and >, which `Comparator`
# cannot express — mapping them onto lte/gte would shift a real
# eligibility decision by one year, so they are deliberately left
# unhandled and keep falling through to `eq`.
_YEAR_FLOOR_CUES: tuple[str, ...] = (
    "or later",
    "or after",
    "and later",
    "onwards",
    "onward",
    "or above",
)

_YEAR_CEILING_CUES: tuple[str, ...] = (
    "or earlier",
    "or before",
    "and earlier",
    "or prior",
)

_NEGATION_PHRASES: tuple[str, ...] = (
    "no minimum",
    "no specific",
    "not a criterion",
    "not a criteria",
    "no cgpa",
    "no gpa",
    "no degree",
    "no formal",
    "regardless of",
    "irrespective of",
    "not required",
    "no experience",
    "without a degree",
    "no percentage",
    "waived",
)

_NUMBER = r"\d{1,3}(?:\.\d{1,2})?"

# --- CGPA ---------------------------------------------------------------
#
# Both orders a posting writes it in. The keyword is MANDATORY in each:
# a bare "7.5" in a sentence about anything is not a CGPA, and treating
# it as one is how a product invents a requirement nobody stated.
_CGPA_AFTER = re.compile(
    rf"\b(?:cgpa|gpa)\b[^0-9\n]{{0,30}}?({_NUMBER})(?:\s*/\s*({_NUMBER}))?",
    re.IGNORECASE,
)
_CGPA_BEFORE = re.compile(
    rf"({_NUMBER})(?:\s*/\s*({_NUMBER}))?\s*\b(?:cgpa|gpa)\b",
    re.IGNORECASE,
)
# A scale stated in words rather than as a fraction. Still explicit —
# the posting is naming the scale, not leaving it to be guessed.
_CGPA_SCALE_WORDS = re.compile(
    rf"(?:out of|on a)\s*({_NUMBER})(?:\s*[- ]?\s*point)?(?:\s*scale)?",
    re.IGNORECASE,
)

# --- School percentages -------------------------------------------------
_PERCENTAGE = re.compile(rf"({_NUMBER})\s*(?:%|per\s?cent|percent)", re.IGNORECASE)
_CLASS_10 = re.compile(r"\b(?:class\s*(?:10|x)\b|10th|tenth|matriculation|secondary)\b", re.I)
_CLASS_12 = re.compile(
    r"\b(?:class\s*(?:12|xii)\b|12th|twelfth|intermediate|higher\s+secondary)\b", re.I
)

# --- Experience ---------------------------------------------------------
#
# "years" alone is not experience — "a 4 year degree" and "2 years of
# experience" are different claims. The clause must say so.
_YEARS = re.compile(rf"({_NUMBER})\s*\+?\s*(?:or\s+more\s+)?year(?:s)?\b", re.IGNORECASE)
_EXPERIENCE_CONTEXT = re.compile(r"\bexperien\w*\b", re.IGNORECASE)

# --- Graduation year ----------------------------------------------------
_YEAR = r"(?:19|20)\d{2}"
_GRAD_CONTEXT = re.compile(
    r"\b(?:graduat\w*|batch|class\s+of|passing\s+out|pass[- ]?out)\b", re.IGNORECASE
)
_YEAR_RANGE = re.compile(rf"({_YEAR})\s*(?:-|–|—|to|/)\s*({_YEAR})")
_YEAR_SINGLE = re.compile(rf"\b({_YEAR})\b")

# --- Degree and field vocabularies --------------------------------------
#
# Spellings, longest first so "bachelor of technology" is not consumed
# by a shorter alternative. Every entry needs punctuation or a full word
# — a bare "be" or "it" would match the English words, and "BE required"
# would silently become a Bachelor of Engineering requirement.
_DEGREE_TERMS: tuple[tuple[str, Degree], ...] = (
    (r"bachelor(?:'s)?\s+of\s+technology", Degree.BTECH),
    (r"bachelor(?:'s)?\s+of\s+engineering", Degree.BE),
    (r"bachelor(?:'s)?\s+of\s+science", Degree.BSC),
    (r"master(?:'s)?\s+of\s+technology", Degree.MTECH),
    (r"master(?:'s)?\s+of\s+science", Degree.MSC),
    (r"b\.?\s?tech\b", Degree.BTECH),
    (r"m\.?\s?tech\b", Degree.MTECH),
    (r"b\.e\.?(?!\w)", Degree.BE),
    (r"b\.?\s?sc\b", Degree.BSC),
    (r"m\.?\s?sc\b", Degree.MSC),
    (r"b\.?\s?c\.?a\b", Degree.BCA),
    (r"m\.?\s?c\.?a\b", Degree.MCA),
    (r"ph\.?\s?d\b|doctorate", Degree.PHD),
    (r"diploma", Degree.DIPLOMA),
)

_FIELD_TERMS: tuple[tuple[str, FieldOfStudy], ...] = (
    (r"computer\s+science(?:\s+engineering)?", FieldOfStudy.COMPUTER_SCIENCE),
    (r"information\s+technology", FieldOfStudy.INFORMATION_TECHNOLOGY),
    (r"data\s+science", FieldOfStudy.DATA_SCIENCE),
    (r"electronics(?:\s+and\s+communication)?", FieldOfStudy.ELECTRONICS),
    (r"electrical(?:\s+engineering)?", FieldOfStudy.ELECTRICAL),
    (r"mechanical(?:\s+engineering)?", FieldOfStudy.MECHANICAL),
    (r"civil(?:\s+engineering)?", FieldOfStudy.CIVIL),
    (r"mathematics|\bmaths\b", FieldOfStudy.MATHEMATICS),
    # Abbreviations, kept to ones with no common English reading.
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

# "or related field", "or equivalent" — the posting hedging its own list
# open. Not expandable deterministically, and not silently ignorable
# either: it changes a mismatch from a rejection into "we cannot tell".
_OPEN_ENDED = re.compile(
    r"\bor\s+(?:a\s+|any\s+)?(?:related|similar|equivalent|allied|relevant)\b"
    r"|\bor\s+equivalent\b|\brelated\s+(?:field|discipline|branch|stream)",
    re.IGNORECASE,
)

_MAX_TERM_CHARS = 80
# Matches skill_evidence/job requirement excerpt widths, so a stored
# window is always representable.
_MAX_EXCERPT_CHARS = 500

# Sentence terminators. Note what is ABSENT: the comma and the colon,
# both of which the skill classifier treats as boundaries. Keeping them
# inside the window is what lets "Class X: 92%, Class XII: 88%" pair two
# markers with two numbers. The period is a boundary only when followed
# by whitespace or end-of-text, which is what protects "7.5" and
# "B.Tech".
_SENTENCE_BOUNDARY = re.compile(r"[;!?\n\r]|\.(?=\s|$)")


@dataclass(frozen=True)
class ParsedRequirement:
    """One eligibility bar, as read off the description.

    Plain values rather than an ORM row, so every rule above stays
    testable without a database.
    """

    requirement_type: EligibilityRequirementType
    comparator: Comparator
    numeric_value: Decimal | None = None
    numeric_max: Decimal | None = None
    value_scale: Decimal | None = None
    accepted_values: tuple[str, ...] = ()
    requirement_level: str = LEVEL_REQUIRED
    open_ended: bool = False
    matched_term: str = ""
    excerpt: str = ""
    confidence: Decimal = _CONFIDENCE_EXPLICIT
    position: int = 0


def _is_negated(window: str) -> bool:
    return any(phrase in window for phrase in _NEGATION_PHRASES)


def _level_for(window: str) -> str:
    return LEVEL_PREFERRED if any(cue in window for cue in _PREFERRED_CUES) else LEVEL_REQUIRED


def _year_direction(window: str) -> Comparator:
    """Which way a single graduation year is open, from its clause.

    `EQ` is the default rather than a guess: a clause naming one year
    with no direction cue ("Graduation year 2024.") IS an exact batch
    requirement, so falling through to equality is correct behaviour and
    not a fallback.
    """
    if any(cue in window for cue in _YEAR_FLOOR_CUES):
        return Comparator.GTE
    if any(cue in window for cue in _YEAR_CEILING_CUES):
        return Comparator.LTE
    return Comparator.EQ


def _decimal(raw: str) -> Decimal:
    return Decimal(raw)


def sentence_around(text: str, position: int) -> tuple[int, int]:
    """The span of the sentence containing `position`."""
    start = 0
    for boundary in _SENTENCE_BOUNDARY.finditer(text, 0, position):
        start = boundary.end()
    match = _SENTENCE_BOUNDARY.search(text, position)
    end = match.start() if match else len(text)
    return start, end


def sentence_excerpt(text: str, position: int) -> str:
    """The verbatim sentence that drove the decision, whitespace-
    collapsed. Always a real slice of the saved job's description —
    never generated prose."""
    start, end = sentence_around(text, position)
    return " ".join(text[start:end].split())[:_MAX_EXCERPT_CHARS]


def _window(text: str, position: int) -> tuple[str, str]:
    """The sentence around `position`, casefolded for cue tests, plus
    the verbatim excerpt to store."""
    start, end = sentence_around(text, position)
    return text[start:end].casefold(), sentence_excerpt(text, position)


def _parse_cgpa(text: str) -> list[ParsedRequirement]:
    """CGPA floors, with a scale ONLY when the posting states one."""
    found: list[ParsedRequirement] = []
    for pattern in (_CGPA_AFTER, _CGPA_BEFORE):
        for match in pattern.finditer(text):
            window, excerpt = _window(text, match.start())
            if _is_negated(window):
                continue
            value = _decimal(match.group(1))
            scale = _decimal(match.group(2)) if match.group(2) else None
            if scale is None:
                # A scale written in words still counts as stated.
                words = _CGPA_SCALE_WORDS.search(window)
                if words:
                    scale = _decimal(words.group(1))
            if scale is None:
                # No scale written anywhere. Fall back to the domain
                # default rather than leaving the bar unresolvable.
                scale = DEFAULT_CGPA_SCALE
            # A scale that cannot contain its own value is a misparse,
            # not a discovery — drop the pair rather than store a
            # nonsense bar like "8.2 out of 4". This also rejects an
            # implausible bare figure ("CGPA 85"), which the default
            # would otherwise turn into a bar nobody could ever meet.
            if scale <= 0 or value > scale:
                continue
            found.append(
                ParsedRequirement(
                    requirement_type=EligibilityRequirementType.CGPA,
                    comparator=Comparator.GTE,
                    numeric_value=value,
                    value_scale=scale,
                    requirement_level=_level_for(window),
                    matched_term=match.group(0).strip()[:_MAX_TERM_CHARS],
                    excerpt=excerpt,
                    position=match.start(),
                )
            )
    return found


def _parse_school_percentages(text: str) -> list[ParsedRequirement]:
    """Class 10 and Class 12 percentage floors.

    ONE PERCENTAGE, SEVERAL CLASSES is the common phrasing — "75% in
    Class 10 and Class 12" states the same bar twice and must produce
    TWO independently resolvable rows, because a candidate can clear one
    and fail the other and deserves to be told which.

    Pairing is only attempted in two unambiguous shapes: one percentage
    applying to every class named in the clause, or an equal number of
    each, paired in document order. Anything else (two percentages and
    three classes) is left unparsed rather than guessed.
    """
    found: list[ParsedRequirement] = []
    # Clause-by-clause, so "Class X: 92%, Class XII: 88%" is two
    # separate problems rather than one four-way ambiguity.
    for clause_start, clause_text in _sentences(text):
        window = clause_text.casefold()
        if _is_negated(window):
            continue
        percentages = list(_PERCENTAGE.finditer(clause_text))
        if not percentages:
            continue

        markers: list[tuple[int, EligibilityRequirementType]] = []
        for marker in _CLASS_10.finditer(clause_text):
            markers.append((marker.start(), EligibilityRequirementType.CLASS_10_PERCENTAGE))
        for marker in _CLASS_12.finditer(clause_text):
            markers.append((marker.start(), EligibilityRequirementType.CLASS_12_PERCENTAGE))
        if not markers:
            continue
        markers.sort()

        if len(percentages) == 1:
            pairs = [(marker, percentages[0]) for marker in markers]
        elif len(percentages) == len(markers):
            pairs = list(zip(markers, percentages, strict=True))
        else:
            # Genuinely ambiguous. Say nothing.
            continue

        excerpt = sentence_excerpt(text, clause_start)
        level = _level_for(window)
        for (marker_position, requirement_type), percentage in pairs:
            value = _decimal(percentage.group(1))
            if value > 100:
                continue
            found.append(
                ParsedRequirement(
                    requirement_type=requirement_type,
                    comparator=Comparator.GTE,
                    numeric_value=value,
                    requirement_level=level,
                    matched_term=percentage.group(0).strip()[:_MAX_TERM_CHARS],
                    excerpt=excerpt,
                    # A shared percentage is a slightly weaker read than
                    # a one-to-one pairing, but both are deterministic.
                    confidence=(
                        _CONFIDENCE_EXPLICIT
                        if len(percentages) == len(markers)
                        else _CONFIDENCE_INFERRED_SUBJECT
                    ),
                    position=clause_start + marker_position,
                )
            )
    return found


def _parse_experience(text: str) -> list[ParsedRequirement]:
    """Years-of-experience floors.

    The clause must actually say "experience". "A 4 year degree" and
    "2 years of experience" both contain a number and the word "years",
    and only one of them is a requirement about a career.
    """
    found: list[ParsedRequirement] = []
    for match in _YEARS.finditer(text):
        window, excerpt = _window(text, match.start())
        if _is_negated(window) or not _EXPERIENCE_CONTEXT.search(window):
            continue
        found.append(
            ParsedRequirement(
                requirement_type=EligibilityRequirementType.YEARS_EXPERIENCE,
                comparator=Comparator.GTE,
                numeric_value=_decimal(match.group(1)),
                requirement_level=_level_for(window),
                matched_term=match.group(0).strip()[:_MAX_TERM_CHARS],
                excerpt=excerpt,
                position=match.start(),
            )
        )
    return found


def _parse_graduation_year(text: str) -> list[ParsedRequirement]:
    """Graduation-year constraints.

    A year is only read as a graduation year when the clause says so —
    a posting is full of other four-digit numbers (founding dates,
    revenue figures, "2024 award"), and none of them constrain who may
    apply.
    """
    found: list[ParsedRequirement] = []
    for context in _GRAD_CONTEXT.finditer(text):
        start, end = sentence_around(text, context.start())
        clause = text[start:end]
        window = clause.casefold()
        if _is_negated(window):
            continue
        excerpt = sentence_excerpt(text, context.start())
        level = _level_for(window)

        span = _YEAR_RANGE.search(clause)
        if span:
            low, high = _decimal(span.group(1)), _decimal(span.group(2))
            if low > high:
                continue
            found.append(
                ParsedRequirement(
                    requirement_type=EligibilityRequirementType.GRADUATION_YEAR,
                    comparator=Comparator.BETWEEN,
                    numeric_value=low,
                    numeric_max=high,
                    requirement_level=level,
                    matched_term=span.group(0).strip()[:_MAX_TERM_CHARS],
                    excerpt=excerpt,
                    position=start + span.start(),
                )
            )
            continue

        years = list(_YEAR_SINGLE.finditer(clause))
        # More than one loose year in a clause with no range separator
        # is ambiguous — which one is the bar?
        if len(years) != 1:
            continue
        found.append(
            ParsedRequirement(
                requirement_type=EligibilityRequirementType.GRADUATION_YEAR,
                # Read from the clause, not assumed: "2024 or later" is a
                # floor and "2024 or earlier" a ceiling. `window` is the
                # casefolded clause already used for negation and level.
                comparator=_year_direction(window),
                numeric_value=_decimal(years[0].group(1)),
                requirement_level=level,
                matched_term=years[0].group(0),
                excerpt=excerpt,
                position=start + years[0].start(),
            )
        )
    return found


def _parse_categorical(
    text: str,
    pattern: re.Pattern[str],
    by_group: Mapping[str, Degree | FieldOfStudy],
    requirement_type: EligibilityRequirementType,
) -> list[ParsedRequirement]:
    """Degree and field-of-study lists, collected per clause.

    Several values in one clause are ALTERNATIVES, not separate bars:
    "B.Tech or B.E." is one requirement accepting two degrees. So a
    clause produces at most one row, carrying every value it named.
    """
    found: list[ParsedRequirement] = []
    for clause_start, clause_text in _sentences(text):
        window = clause_text.casefold()
        if _is_negated(window):
            continue
        values: list[str] = []
        terms: list[str] = []
        first_position: int | None = None
        for match in pattern.finditer(clause_text):
            group = match.lastgroup
            if group is None or group not in by_group:  # pragma: no cover - defensive
                continue
            value = by_group[group].value
            if value not in values:
                values.append(value)
                terms.append(match.group(0).strip())
            if first_position is None:
                first_position = match.start()
        if not values or first_position is None:
            continue
        found.append(
            ParsedRequirement(
                requirement_type=requirement_type,
                comparator=Comparator.IN,
                accepted_values=tuple(values),
                requirement_level=_level_for(window),
                # "or related field" — recorded, never expanded.
                open_ended=_OPEN_ENDED.search(clause_text) is not None,
                matched_term=", ".join(terms)[:_MAX_TERM_CHARS],
                excerpt=sentence_excerpt(text, clause_start + first_position),
                position=clause_start + first_position,
            )
        )
    return found


def _sentences(text: str) -> list[tuple[int, str]]:
    """Every sentence in the description, as (offset, text)."""
    spans: list[tuple[int, str]] = []
    position = 0
    length = len(text)
    while position < length:
        start, end = sentence_around(text, position)
        if end > start:
            spans.append((start, text[start:end]))
        position = max(end + 1, position + 1)
    return spans


def parse_eligibility_requirements(description: str) -> list[ParsedRequirement]:
    """Every eligibility bar the description states, in document order.

    Returns ALL occurrences; collapsing repeats into one row per
    requirement type is `build_desired_requirements`' job, exactly as on
    the skill side.
    """
    parsed = [
        *_parse_cgpa(description),
        *_parse_school_percentages(description),
        *_parse_experience(description),
        *_parse_graduation_year(description),
        *_parse_categorical(
            description,
            _DEGREE_PATTERN,
            _DEGREE_BY_GROUP,
            EligibilityRequirementType.HIGHEST_DEGREE,
        ),
        *_parse_categorical(
            description, _FIELD_PATTERN, _FIELD_BY_GROUP, EligibilityRequirementType.FIELD_OF_STUDY
        ),
    ]
    return sorted(parsed, key=lambda item: (item.position, item.requirement_type.value))


def strictest(candidates: list[ParsedRequirement]) -> ParsedRequirement:
    """The binding statement among several for one requirement type.

    A hard requirement outranks a preference, and among equals the
    tighter bound wins: a posting asking for 7.0 in one place and 7.5 in
    another requires 7.5. Ties break on the earliest position, which is
    what makes a re-run over an unchanged description produce a
    byte-identical row and therefore write nothing.
    """

    def sort_key(item: ParsedRequirement) -> tuple[int, Decimal, int]:
        level_rank = 0 if item.requirement_level == LEVEL_REQUIRED else 1
        if item.numeric_value is None:
            tightness = Decimal(0)
        elif item.comparator is Comparator.LTE:
            tightness = item.numeric_value
        else:
            # Higher floor = stricter, so negate to sort ascending.
            tightness = -item.numeric_value
        return (level_rank, tightness, item.position)

    return min(candidates, key=sort_key)
