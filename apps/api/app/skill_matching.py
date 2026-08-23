"""Deterministic, boundary-aware matching of taxonomy terms against
resume text (Prompt 2.4).

No LLM, no embeddings, no fuzzy/edit-distance matching — per
docs/project-brief.md's Evidence-First rule, a skill must never be
invented, and every attribution here is a literal string match a human
can re-check by eye.

Pure: text and terms in, matches out. No database, no I/O — so the
matching rules can be tested exhaustively without any fixtures (see
tests/test_skill_matching.py), and app/skill_extraction.py is left with
only persistence to worry about.

Matching runs against the RAW text rather than a normalized copy. That
is deliberate: regex match offsets then point straight into the original
string, so excerpts are exact source spans with no index-mapping step to
get wrong.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# How a term's parts may be joined in the text, so "Next.js" /
# "Next JS" / "NextJS" and "Object-Oriented" / "Object Oriented" all
# match one term. Applied between parts, never at the edges (those are
# guarded by the boundary lookarounds below).
#
# A joiner is whitespace-only, OR punctuation-only, OR absent — but
# never punctuation FOLLOWED BY whitespace. That exclusion is what stops
# "The unit. Testing was manual." from matching the term "Unit Testing":
# a dot with a space after it is sentence punctuation, whereas the dot
# in "Next.js" sits inside a single compound token. Without this, any
# two-word term would silently match across a sentence boundary.
_SEPARATOR = r"(?:\s+|[._\-/]+)?"
_SEPARATOR_SPLIT = re.compile(r"[\s._\-/]+")

# Terms that are also ordinary English words likely to appear in resume
# prose with a NON-skill meaning. These match only in "list context"
# (see _in_list_context), which is what stops "I go to the office" and
# "express delivery" from becoming skills.
#
# Chosen conservatively — the guard costs real recall, so a term earns a
# place here only if its everyday sense plausibly appears in a resume:
#
#   go       "go to", "go live", "ongoing work" -> very common
#   express  "express interest", "express delivery" -> common enough
#
# Considered and deliberately EXCLUDED, because the recall cost exceeds
# the false-positive risk in resume text specifically:
#
#   react    "Built React components" is overwhelmingly the common case;
#            "react to feedback" is rare in a resume, and requiring list
#            context would miss most genuine React experience.
#   jest     the English word essentially never appears in a resume.
#   agile    "agile team"/"agile mindset" ARE about the methodology, so
#            matching them is correct rather than a false positive.
_AMBIGUOUS_TERMS = frozenset({"go", "express"})

# Characters that mark a term as an enumeration item rather than prose.
_LIST_LEFT = frozenset("\n\r,;:|•*-–—([/")
_LIST_RIGHT = frozenset("\n\r,;:|•)]/.")
_INLINE_WHITESPACE = frozenset(" \t")

# Excerpt shaping. The column allows 500 characters; staying well under
# keeps evidence readable and leaves headroom.
_MAX_EXCERPT_CHARS = 300
_WINDOW_CHARS = 120


class MatchKind(StrEnum):
    """How a term matched, which is the sole input to its confidence.

    AMBIGUOUS_LIST_CONTEXT outranks nothing: it is the weakest kind,
    applied whenever the matched term is in _AMBIGUOUS_TERMS, regardless
    of whether that term was a canonical name or an alias.
    """

    CANONICAL = "canonical"
    ALIAS = "alias"
    AMBIGUOUS_LIST_CONTEXT = "ambiguous_list_context"


# Fixed, documented confidences — a lookup table, never a computed or
# tuned score, so the same resume always yields the same numbers.
# Ordinal placeholders for Prompt 4.x scoring: a canonical name is the
# least ambiguous signal available, an alias is a shorthand that is
# marginally likelier to be coincidental, and an ambiguous term retains
# residual risk even after passing the list-context guard. 1.00 is
# reserved for a user's own assertion (manual entry), which outranks any
# inference this module can make.
CONFIDENCE_BY_KIND: dict[MatchKind, Decimal] = {
    MatchKind.CANONICAL: Decimal("0.90"),
    MatchKind.ALIAS: Decimal("0.75"),
    MatchKind.AMBIGUOUS_LIST_CONTEXT: Decimal("0.60"),
}

# Selection order when one skill matched through several of its terms —
# strongest kind wins, then earliest position. So a resume containing
# both "Python" and "py" records the canonical 0.90, not the alias 0.75.
_KIND_PRIORITY: dict[MatchKind, int] = {
    MatchKind.CANONICAL: 0,
    MatchKind.ALIAS: 1,
    MatchKind.AMBIGUOUS_LIST_CONTEXT: 2,
}


@dataclass(frozen=True)
class SkillTerm:
    """One searchable spelling of one canonical skill: either the
    skill's own name, or one of its aliases."""

    skill_id: uuid.UUID
    term: str
    is_alias: bool


@dataclass(frozen=True)
class SkillMatch:
    """One skill found in the text, with the span that justifies it."""

    skill_id: uuid.UUID
    matched_term: str
    kind: MatchKind
    excerpt: str
    start: int

    @property
    def confidence(self) -> Decimal:
        return CONFIDENCE_BY_KIND[self.kind]


def compile_term(term: str) -> re.Pattern[str]:
    """Build the separator-tolerant, boundary-guarded pattern for a term.

    Boundaries use `(?<!\\w)` / `(?!\\w)` rather than `\\b`. For terms
    starting and ending in word characters the two are equivalent — both
    stop "Java" matching inside "JavaScript" — but the lookarounds stay
    correct for a term that begins or ends with punctuation (".NET",
    "C++") if the taxonomy ever grows one, where `\\b` would silently
    invert its meaning.
    """
    parts = [part for part in _SEPARATOR_SPLIT.split(term.strip()) if part]
    if not parts:
        raise ValueError(f"term {term!r} has no matchable characters")
    body = _SEPARATOR.join(re.escape(part) for part in parts)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _in_list_context(text: str, start: int, end: int) -> bool:
    """True when the span sits in an enumeration rather than prose.

    Resumes list skills as "Python, Go, Docker", "• Go", or
    "Languages: Go" — all of which have a delimiter on both sides. Prose
    uses ("I go to", "Go the extra mile") do not. Inline whitespace is
    skipped on each side; a document edge counts as a delimiter.
    """
    left = start - 1
    while left >= 0 and text[left] in _INLINE_WHITESPACE:
        left -= 1
    if left >= 0 and text[left] not in _LIST_LEFT:
        return False

    right = end
    while right < len(text) and text[right] in _INLINE_WHITESPACE:
        right += 1
    return right >= len(text) or text[right] in _LIST_RIGHT


def _excerpt_for(text: str, start: int, end: int) -> str:
    """The line containing the match, or a window around it when that
    line is unreasonably long (a resume flattened to one paragraph).

    Whitespace is collapsed so the stored evidence is a single readable
    line. Always a verbatim slice of the source — nothing is reworded,
    summarized, or generated.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)

    if line_end - line_start <= _MAX_EXCERPT_CHARS:
        snippet = text[line_start:line_end]
        prefix = suffix = ""
    else:
        window_start = max(line_start, start - _WINDOW_CHARS)
        window_end = min(line_end, end + _WINDOW_CHARS)
        # Snap inward to whitespace so the excerpt never begins or ends
        # mid-word.
        if window_start > line_start:
            space = text.find(" ", window_start, start)
            if space != -1:
                window_start = space + 1
        if window_end < line_end:
            space = text.rfind(" ", end, window_end)
            if space != -1:
                window_end = space
        snippet = text[window_start:window_end]
        prefix = "…" if window_start > line_start else ""
        suffix = "…" if window_end < line_end else ""

    collapsed = " ".join(snippet.split())
    return f"{prefix}{collapsed}{suffix}"[:_MAX_EXCERPT_CHARS]


def _kind_for(term: SkillTerm) -> MatchKind:
    if term.term.strip().casefold() in _AMBIGUOUS_TERMS:
        return MatchKind.AMBIGUOUS_LIST_CONTEXT
    return MatchKind.ALIAS if term.is_alias else MatchKind.CANONICAL


def _overlaps(claimed: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in claimed)


def find_skill_matches(text: str, terms: Sequence[SkillTerm]) -> list[SkillMatch]:
    """Every skill the text mentions, at most one match per skill.

    Terms are tried longest-first and each accepted match claims its
    span, so a shorter term can never match inside a longer one's
    territory (belt and braces alongside the boundary lookarounds).
    Where a skill matched through several of its terms, the strongest
    kind wins and ties break on the earliest position — making the
    result, and therefore the stored excerpt and confidence, stable
    across reruns.
    """
    if not text or not text.strip():
        return []

    ordered = sorted(terms, key=lambda t: (-len(t.term), t.term.casefold()))
    claimed: list[tuple[int, int]] = []
    best: dict[uuid.UUID, SkillMatch] = {}

    for term in ordered:
        kind = _kind_for(term)
        needs_list_context = kind is MatchKind.AMBIGUOUS_LIST_CONTEXT

        for found in compile_term(term.term).finditer(text):
            start, end = found.start(), found.end()
            if _overlaps(claimed, start, end):
                continue
            if needs_list_context and not _in_list_context(text, start, end):
                continue

            claimed.append((start, end))
            candidate = SkillMatch(
                skill_id=term.skill_id,
                matched_term=term.term,
                kind=kind,
                excerpt=_excerpt_for(text, start, end),
                start=start,
            )
            current = best.get(term.skill_id)
            if current is None or (_KIND_PRIORITY[kind], start) < (
                _KIND_PRIORITY[current.kind],
                current.start,
            ):
                best[term.skill_id] = candidate
            # One accepted match per term is enough — this module records
            # one representative excerpt per skill, matching the evidence
            # natural key established in Prompt 2.3.
            break

    return sorted(best.values(), key=lambda match: match.start)
