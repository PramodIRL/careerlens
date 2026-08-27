"""Where an ungrounded explanation is rejected (Prompt 6.1).

FAIL CLOSED, NEVER REPAIR. Every check below either passes the whole
explanation or discards it. Nothing here fixes a malformed field, drops
a bad citation and keeps the rest, or retries — a half-corrected
explanation is one nobody can reason about, and "we removed the parts we
could detect" is not a claim this product should make.

FIVE LAYERS:

  1. transport  size ceiling, then strict JSON parsing
  2. schema     app/explanation/schema.py, extra="forbid", bounded
  3. citations  every cited id is in the facts; a strength must cite
  4. grounding  no number, and no known skill, that the facts lack
  5. hygiene    no links

WHAT LAYER 4 CANNOT DO, stated here rather than discovered later. It
catches invented NUMBERS and invented TAXONOMY SKILLS. A fluent sentence
built only from in-vocabulary words — "your Python work suggests you
would ramp up quickly" — is not mechanically detectable and will pass.
Nor is the number check arithmetic: it asks whether a quantity appears
in the facts AT ALL, so "3 years of experience" survives when 3 happens
to be a requirement weight. Requiring a citation on every strength is
what bounds the damage: the claim has to point at a row a reader can
check. These limits are real, and the honest place for them is a
docstring rather than an implied guarantee.

The skill check is CASE-SENSITIVE, which trades one error for another:
it avoids rejecting an explanation for saying "go through your gaps"
(the taxonomy contains "Go"), at the cost of missing a lowercased
invented skill. Rejection is the safe direction, and the mock provider
does neither.
"""

import json
import re
import uuid
from enum import StrEnum

from pydantic import ValidationError

from app.explanation.schema import (
    MAX_RESPONSE_BYTES,
    ExplanationFacts,
    MatchExplanation,
)

# A digit run, with an optional decimal part. Deliberately simple: the
# question is "does this text state a quantity", and anything with
# digits in it is a quantity somebody might believe.
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Anything that would render as a link. An invented course URL is a
# fabrication with a click target, which is worse than a fabricated
# sentence.
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)


class RejectionReason(StrEnum):
    """Machine-readable, returned to the client as `reason`.

    Named for what the model did wrong, not for what the user should
    do — the UI decides the wording, the API states the fact.
    """

    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_JSON = "malformed_json"
    SCHEMA_INVALID = "schema_invalid"
    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
    UNGROUNDED_CLAIM = "ungrounded_claim"
    INVENTED_SKILL = "invented_skill"
    INVENTED_NUMBER = "invented_number"
    DISALLOWED_LINK = "disallowed_link"


class ExplanationRejected(Exception):
    """One rejected explanation, and why.

    `detail` is for logs and tests. It is NOT returned to the client:
    it can quote the offending fragment, and echoing model output back
    into the response is how "we never show ungrounded content" quietly
    stops being true.
    """

    def __init__(self, reason: RejectionReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


def _allowed_numbers(facts: ExplanationFacts) -> frozenset[str]:
    """Every quantity the facts contain, as the strings they'd be
    written as. A number outside this set was invented."""
    score = facts.score
    values: list[float] = [
        score.overall_score,
        score.earned_weight,
        score.obtainable_weight,
        score.required_matched,
        score.required_total,
        facts.semantic.fit,
        facts.semantic.considered,
        len(facts.matched_skills),
        len(facts.missing_required_skills),
        len(facts.missing_other_skills),
        len(facts.evidence),
    ]
    values.extend(score.weights.values())
    for level in score.by_level.values():
        values.extend((level.matched, level.total))
    values.extend(facts.gaps.totals.values())

    allowed = {str(int(value)) for value in values}
    # Similarities are the one non-integer fact, quoted at the precision
    # the API itself reports them with.
    allowed.update(f"{hit.similarity:.2f}" for hit in facts.semantic.hits)
    allowed.update(str(hit.similarity) for hit in facts.semantic.hits)
    return frozenset(allowed)


def _fact_strings(facts: ExplanationFacts) -> list[str]:
    """String facts that legitimately contain digits — version
    identifiers, a model name, the job's own title. They are removed
    from the text before numbers are counted, so quoting
    `skill_match_v1` is not read as stating the number 1."""
    strings = [
        facts.score.formula_version,
        facts.gaps.formula_version,
        facts.semantic.formula_version,
        facts.semantic.model_identifier,
        facts.job.title,
        facts.job.company,
    ]
    strings.extend(facts.skill_names())
    # Longest first, so a shorter string that is a substring of a longer
    # one cannot chop it up before it is matched whole.
    return sorted((value for value in strings if value), key=len, reverse=True)


def _generated_text(explanation: MatchExplanation) -> list[str]:
    return [
        explanation.summary,
        *(claim.text for claim in explanation.strengths),
        *(claim.text for claim in explanation.gaps),
        *explanation.next_steps,
    ]


def _check_numbers(explanation: MatchExplanation, facts: ExplanationFacts) -> None:
    allowed = _allowed_numbers(facts)
    strings = _fact_strings(facts)
    for text in _generated_text(explanation):
        scrubbed = text
        for value in strings:
            scrubbed = scrubbed.replace(value, " ")
        for found in _NUMBER.findall(scrubbed):
            if found not in allowed:
                raise ExplanationRejected(RejectionReason.INVENTED_NUMBER, found)


def _check_skills(
    explanation: MatchExplanation,
    facts: ExplanationFacts,
    taxonomy: frozenset[str],
) -> None:
    """A curated-taxonomy skill named in the output but absent from the
    facts is an invented skill. The taxonomy is the closed vocabulary
    that makes this decidable at all — a name outside it is undetectable
    and passes, which the module docstring says out loud."""
    outside = {name for name in taxonomy if name not in facts.skill_names()}
    if not outside:
        return
    patterns = [
        (name, re.compile(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])")) for name in outside
    ]
    for text in _generated_text(explanation):
        for name, pattern in patterns:
            if pattern.search(text):
                raise ExplanationRejected(RejectionReason.INVENTED_SKILL, name)


def _check_citations(explanation: MatchExplanation, facts: ExplanationFacts) -> list[uuid.UUID]:
    """Every cited id must be a row in the facts, and every strength
    must cite. Returns the recomputed union — the model's own
    `cited_evidence_ids` is checked and then discarded."""
    known = facts.evidence_by_id()
    cited: list[uuid.UUID] = []

    for evidence_id in explanation.cited_evidence_ids:
        if evidence_id not in known:
            raise ExplanationRejected(RejectionReason.UNKNOWN_EVIDENCE_ID, str(evidence_id))

    for claim in (*explanation.strengths, *explanation.gaps):
        for evidence_id in claim.evidence_ids:
            if evidence_id not in known:
                raise ExplanationRejected(RejectionReason.UNKNOWN_EVIDENCE_ID, str(evidence_id))
            if evidence_id not in cited:
                cited.append(evidence_id)

    for claim in explanation.strengths:
        if not claim.evidence_ids:
            raise ExplanationRejected(RejectionReason.UNGROUNDED_CLAIM, claim.text[:80])

    return sorted(cited, key=str)


def _check_links(explanation: MatchExplanation) -> None:
    for text in _generated_text(explanation):
        if _LINK.search(text):
            raise ExplanationRejected(RejectionReason.DISALLOWED_LINK, text[:80])


def validate_explanation(
    raw: str,
    *,
    facts: ExplanationFacts,
    taxonomy: frozenset[str],
) -> MatchExplanation:
    """Raw provider text in, a grounded `MatchExplanation` out.

    Raises `ExplanationRejected` at the first failure. `taxonomy` is the
    curated skill vocabulary (app/seeds/skill_taxonomy.py) and is passed
    in rather than sent to the model — a list of every skill the system
    knows is an invitation to use one of them.
    """
    if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ExplanationRejected(RejectionReason.RESPONSE_TOO_LARGE, str(len(raw)))

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        raise ExplanationRejected(RejectionReason.MALFORMED_JSON, str(error)) from error

    if not isinstance(payload, dict):
        raise ExplanationRejected(RejectionReason.MALFORMED_JSON, type(payload).__name__)

    try:
        explanation = MatchExplanation.model_validate(payload)
    except ValidationError as error:
        raise ExplanationRejected(RejectionReason.SCHEMA_INVALID, str(error)) from error

    cited = _check_citations(explanation, facts)
    _check_skills(explanation, facts, taxonomy)
    _check_numbers(explanation, facts)
    _check_links(explanation)

    # The returned citation set is the one recomputed from the claims,
    # never the one the model declared.
    return explanation.model_copy(update={"cited_evidence_ids": cited})
