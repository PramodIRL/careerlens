"""Deterministic classification of a matched skill as required,
preferred, or merely mentioned (Prompt 4.2).

PURE: text and an offset in, a level out. No database, no network, no
LLM, no model inference — so every rule here is testable without
fixtures, the same split app/skill_matching.py has from
app/skill_extraction.py.

WHY THIS DOES NOT USE `SkillMatch.excerpt`. That excerpt is LINE-scoped
(app/skill_matching.py's `_excerpt_for`), which is right for showing a
human why a skill was found but fatal for deciding what the posting
requires. A single line routinely carries several independent claims:

    "We require Python and PostgreSQL. Experience with FastAPI is
     preferred. Docker is a plus."

Classifying on "does the excerpt contain 'require'" marks ALL FOUR
skills required, including the two the sentence explicitly calls
preferred. So classification works from the match OFFSET and derives its
own CLAUSE-scoped window — the smallest span that still contains the
statement being made about this skill.

NEGATION IS CHECKED FIRST, AND IT WINS. "Python is not required" contains
the substring "required"; any cue-first design classifies it exactly
backwards. Scanning for negation before positive cues is what makes the
brief's explicit counter-examples come out right.

BIASED TOWARD UNDER-CLAIMING. Where a window is genuinely ambiguous the
weaker level is chosen, because Prompt 4.3 turns `required` into a skill
GAP — over-claiming invents work the candidate does not actually need.
"""

import re
from enum import StrEnum

# Clause terminators. Commas are included because a posting routinely
# packs separate claims into one sentence — "Python is required, Docker
# is a plus" — and a sentence-scoped window would let the first claim
# capture the second skill.
_CLAUSE_BOUNDARY = re.compile(r"[.;:!?\n\r]|(?:,)")

# Excerpt ceiling, matching skill_evidence's column width so a stored
# window is always representable.
_MAX_EXCERPT_CHARS = 500


class RequirementLevel(StrEnum):
    """How strongly a posting asks for a skill.

    MENTIONED IS THE DEFAULT, not a weak "required". A curated skill
    appearing with no explicit classifier means exactly that: the posting
    named it and said nothing about necessity. Reading intent into that
    silence is the guessing this module exists to avoid.
    """

    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"


# Strongest first. Used when one skill appears in several windows: a
# posting that calls Python required anywhere requires it, whatever else
# it says elsewhere.
_LEVEL_PRIORITY: dict[RequirementLevel, int] = {
    RequirementLevel.REQUIRED: 0,
    RequirementLevel.PREFERRED: 1,
    RequirementLevel.MENTIONED: 2,
}

# Phrases that DEFEAT a requirement reading. Checked before any positive
# cue, because most of them contain a positive cue as a substring.
_NEGATION_PHRASES: tuple[str, ...] = (
    "not required",
    "not a requirement",
    "not essential",
    "not necessary",
    "not mandatory",
    "no prior",
    "no experience",
    "does not require",
    "do not require",
    "don't require",
    "isn't required",
    "is not needed",
    "not needed",
    "without",
)

# "No <something> necessary/required" — the other common phrasing, where
# the negator and the cue are separated by the thing being negated
# ("No Python experience necessary"). A bounded gap keeps this from
# reaching across an entire clause.
_NO_X_NEEDED = re.compile(
    r"\bno\b[^,.;:!?\n]{0,40}?\b(?:necessary|required|needed|essential|experience)\b"
)

_REQUIRED_CUES: tuple[str, ...] = (
    "required",
    "requires",
    "require",
    "requirement",
    "must have",
    "must be",
    "must",
    "essential",
    "mandatory",
    "you will need",
    "we need",
    "needed",
    "proficiency in",
    "strong command of",
)

_PREFERRED_CUES: tuple[str, ...] = (
    "preferred",
    "preferably",
    "nice to have",
    "nice-to-have",
    "bonus",
    "a plus",
    "plus point",
    "ideally",
    "desirable",
    "advantageous",
    "an advantage",
    "would be great",
    "good to have",
    "welcome",
)


def clause_around(text: str, position: int) -> tuple[int, int]:
    """The span of the clause containing `position`.

    Walks outward to the nearest clause boundary in each direction. This
    is the unit a posting actually makes one claim in, and keeping it
    tight is what stops "require Python. Docker is a plus" from marking
    Docker required.
    """
    start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(text, 0, position):
        start = boundary.end()
    match = _CLAUSE_BOUNDARY.search(text, position)
    end = match.start() if match else len(text)
    return start, end


def clause_excerpt(text: str, position: int) -> str:
    """The verbatim clause that drove the decision, whitespace-collapsed.

    Always a real slice of the saved job's description — never generated
    prose. Storing the classification window rather than the matcher's
    line means the excerpt shows exactly the text that was judged, so a
    wrong call is visible rather than mysterious.
    """
    start, end = clause_around(text, position)
    return " ".join(text[start:end].split())[:_MAX_EXCERPT_CHARS]


def _is_negated(window: str) -> bool:
    """True when the window defeats a requirement reading.

    Deliberately phrase-based rather than parsed. Real negation scope
    needs a parser, which is a far larger commitment; being predictably
    conservative over a documented phrase list is the honest trade. The
    known gap is stated in the module docstring's bias note: "Python is
    required, unlike Docker" is not caught.
    """
    if any(phrase in window for phrase in _NEGATION_PHRASES):
        return True
    return _NO_X_NEEDED.search(window) is not None


def classify_at(text: str, position: int) -> RequirementLevel:
    """The level asserted about the skill at `position`.

    Order is load-bearing:

      1. NEGATION first. "Python is not required" and "No Python
         experience necessary" both contain a required cue as a
         substring, so any other order gets them exactly backwards. A
         negated window is MENTIONED — the skill was named, and the
         posting said it is not needed.
      2. Both cue kinds present -> PREFERRED. Clause segmentation
         already separates ordinary "X required, Y preferred" phrasing,
         so a window with both is genuinely ambiguous, and the weaker
         reading is the safer one (see the module's bias note).
      3. A single cue kind decides.
      4. Neither -> MENTIONED.
    """
    start, end = clause_around(text, position)
    window = text[start:end].casefold()

    if _is_negated(window):
        return RequirementLevel.MENTIONED

    has_required = any(cue in window for cue in _REQUIRED_CUES)
    has_preferred = any(cue in window for cue in _PREFERRED_CUES)

    if has_required and has_preferred:
        return RequirementLevel.PREFERRED
    if has_required:
        return RequirementLevel.REQUIRED
    if has_preferred:
        return RequirementLevel.PREFERRED
    return RequirementLevel.MENTIONED


def strongest(levels: list[RequirementLevel]) -> RequirementLevel:
    """The strongest level among several occurrences of one skill.

    required > preferred > mentioned. A posting that calls Python
    required anywhere requires it, however casually it mentions it
    elsewhere — and the ordering makes the result independent of which
    occurrence happened to come first.
    """
    return min(levels, key=lambda level: _LEVEL_PRIORITY[level])
