"""Where an ungrounded explanation is rejected (Prompt 6.1).

FAIL CLOSED, NEVER REPAIR. Every check below either passes the whole
explanation or discards it. Nothing here fixes a malformed field, drops
a bad citation and keeps the rest, or retries — a half-corrected
explanation is one nobody can reason about, and "we removed the parts we
could detect" is not a claim this product should make.

SIX LAYERS:

  1. transport   size ceiling, then strict JSON parsing
  2. schema      app/explanation/schema.py, extra="forbid", bounded
  3. citations   every cited id is in the facts; a strength must cite
  4. grounding   no number, and no known skill, that the facts lack
  5. consistency no sentence that contradicts the decided verdict
  6. hygiene     no links

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

LAYER 5 EXISTS BECAUSE OF A REAL FAILURE, AND ITS LIMITS ARE REAL TOO
(Prompt 6.4b). A 100% match with no gaps produced "REST APIs is a
required skill for the role" about a skill that was MATCHED and merely
MENTIONED — every word of it in vocabulary, no number in it, so layers
3, 4 and 6 all passed it. `_check_consistency` compares what a sentence
ASSERTS about a skill against what the facts DECIDED about it, over a
closed vocabulary of absence and requirement words.

It is LEXICAL, not semantic, and that bound is worth stating plainly: a
paraphrase naming no skill ("your backend story is thinner here") is
invisible to it, and so is a contradiction phrased in words outside the
marker lists. It is a real check on the failures actually observed, not
a guarantee about every possible one. The structural half of the fix —
`summary_gap` pinned to null and `gaps` bounded to zero when the facts
hold no gap, in app/explanation/ollama_provider.py — is what makes the
observed failure unrepresentable; this catches the free-text remainder.

FALSE POSITIVES ARE THE COST, AND THEY ARE MANAGED RATHER THAN IGNORED.
A truthful negation — "this is preferred rather than required" — must
survive, so a requirement marker is excused when the clause actually
says the skill is NOT required, and negated absences ("no gaps here")
are scrubbed before the absence markers are counted. Same scrub-then-
search shape `_check_numbers` already uses.

WHAT 6.4c CHANGED, AND WHY BOTH DIRECTIONS WERE WRONG. The escape was
too wide and the scope too narrow, so the same rule both missed real
contradictions and produced avoidable rejections. "The posting mentions
AWS as required" escaped because "mentions" counted as a contrast; "<A>
and <B>, both of which this posting requires" escaped because naming one
genuinely required skill disarmed the rule entirely. Neither is a
loosening or a tightening on its own — both are the rule failing to mean
what it says.

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
# A quantity, not a digit inside a name. The lookarounds matter: without
# them "EC2" yields "2", "S3" yields "3" and "K8s" yields "8", so any
# roadmap mentioning ordinary cloud services was rejected for stating a
# number nobody supplied. The rule's intent is "do not assert a quantity
# we did not give you"; a digit inside an identifier is part of a name
# and asserts nothing. Same boundary style `_check_skills` already uses.
_NUMBER = re.compile(r"(?<![0-9A-Za-z])\d+(?:\.\d+)?(?![0-9A-Za-z])")

# Anything that would render as a link. An invented course URL is a
# fabrication with a click target, which is worse than a fabricated
# sentence.
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)

# --- layer 5's vocabulary (Prompt 6.4b) ------------------------------
# Three small closed lists. Closed is what makes this checkable at all,
# exactly as the curated taxonomy is what makes the invented-skill check
# decidable.

# "This person does not have it." The words an explanation would use to
# describe a hole in somebody's profile.
_ABSENCE = re.compile(
    r"(?<![0-9A-Za-z])(?:"
    r"missing|lacks|lacking|absent|gaps?|shortfalls?|"
    r"does not (?:yet )?(?:have|show|cover|include|demonstrate)|"
    r"do not (?:yet )?(?:have|show|cover|include|demonstrate)|"
    r"doesn't (?:yet )?(?:have|show|cover|include|demonstrate)|"
    r"don't (?:yet )?(?:have|show|cover|include|demonstrate)|"
    r"not (?:yet )?(?:shown|covered|demonstrated|evidenced|present)|"
    r"no evidence|without evidence|"
    r"needs? to learn|need to learn|"
    r"(?:main )?area to improve|room to improve|weakest"
    r")(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# A NEGATED absence, which is the opposite claim and perfectly true on a
# complete match. Scrubbed out before the absence markers are counted,
# so "there are no gaps here" does not read as asserting a gap.
_NEGATED_ABSENCE = re.compile(
    r"(?<![0-9A-Za-z])(?:no|not any|zero|none|nothing|neither)"
    r"(?:\s+\S+){0,5}?\s+(?:missing|gaps?|shortfalls?|lacking|absent)"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# "The posting demands it." Saying this about a preferred or merely
# mentioned skill is a false statement about the job.
_REQUIREMENT = re.compile(
    r"(?<![0-9A-Za-z])(?:required|requires|require|requirements?|"
    r"must[- ]have|mandatory|essential|prerequisites?|non[- ]negotiable)"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# A sentence that NEGATES the requirement rather than asserting it —
# "preferred rather than required", "asked for but not required". True,
# useful, and it must survive, so a requirement marker in its company is
# not read as a claim.
#
# NARROWED IN 6.4c, AND THIS IS A CORRECTNESS FIX. The list used to
# include "preferred", "mentioned", "mentions" and "in passing" on their
# own, which made it an escape hatch rather than a negation test: "the
# posting mentions AWS as required" contains both a requirement marker
# and a contrast word, and sailed through while asserting exactly the
# thing the rule exists to catch. Measured live, it did. Only a
# construction that actually says "not required" excuses one now.
_NEGATED_REQUIREMENT = re.compile(
    r"(?<![0-9A-Za-z])(?:rather than (?:required|a requirement|being required)|"
    r"instead of (?:required|a requirement)|as opposed to (?:required|a requirement)|"
    r"not (?:strictly |formally |technically )?required|"
    r"not a (?:hard |strict )?requirement|"
    r"n[o']t (?:strictly )?required|"
    r"nice[- ]to[- ]have|optional|"
    r"does not require|do not require|doesn't require|don't require)"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# A quantifier that spreads a claim across EVERY skill the clause names.
#
# THE HOLE THIS CLOSES. Rule 3 below used to fire only when a clause
# named no required skill at all, so that a comparative sentence — "you
# have Python but not Kubernetes" — was not read as a claim about the
# wrong one. But "<A> and <B>, both of which this posting requires"
# names a required skill AND distributes the claim over a preferred one,
# and the guard let it through: measured 27 times across 30 live runs on
# verdicts with a non-required matched skill. "Both" is precisely the
# word that makes the claim apply to each of them.
_DISTRIBUTIVE = re.compile(
    r"(?<![0-9A-Za-z])(?:both|each|either|"
    r"all of (?:which|them|these|the above)|"
    r"each of (?:which|them|these)|every one of|"
    r"all (?:three|four|five|six|seven)|"
    r"(?:are|is|were) all|all (?:are|were))"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# CLAUSE boundaries, not sentence boundaries, and the difference is
# load-bearing. "Your profile is a strong fit for the Python side of
# this role, and the main area to improve is cloud experience" is one
# sentence containing two independent claims: the first names a matched
# skill, the second states an absence about something else entirely.
# Judged whole, the absence attaches to Python and a truthful sentence
# is rejected.
#
# Splitting on the coordinators does NOT weaken the check on the failure
# it was built for. "The posting lists REST APIs as a required skill,
# and your profile does not yet show this capability" splits into a
# first clause that still asserts a non-required skill is required —
# caught by rule 3 — and a second that names no skill at all.
# How far a quantifier reaches forward to the claim it governs. Wide
# enough for "both of which this posting explicitly requires", narrow
# enough that a "both" later in the sentence, attached to something
# else entirely, does not reach backwards to a requirement it has
# nothing to do with.
_QUANTIFIER_REACH = 45

_SENTENCE = re.compile(
    r"(?<=[.!?])\s+|;\s+|\s+—\s+|,\s+(?:and|but|so|while|though|whereas|although)\s+"
)


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
    # The answer stated something the deterministic result did not say —
    # a matched skill described as missing, a preferred one called
    # required, or a gap on a result that has none (Prompt 6.4b).
    CONTRADICTS_FACTS = "contradicts_facts"

    # --- provider failures (Prompt 6.2) ------------------------------
    # One vocabulary for `reason`, so a client has a single set of
    # values to understand rather than "these come from the validator
    # and those from somewhere else". Nothing below is raised by this
    # module — app/explanation/adapter.py reports them — but they are
    # the same field, so they belong in the same enum.
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"


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


def allowed_numbers(facts: ExplanationFacts) -> frozenset[str]:
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
        explanation.summary_fit,
        explanation.summary_gap or "",
        *(claim.text for claim in explanation.strengths),
        *(claim.text for claim in explanation.gaps),
        *explanation.next_steps,
    ]


def _check_numbers(explanation: MatchExplanation, facts: ExplanationFacts) -> None:
    allowed = allowed_numbers(facts)
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


def _named_in(text: str, names: frozenset[str]) -> set[str]:
    """Which of `names` this text actually names.

    Same word-boundary rule and same case-sensitivity as
    `_check_skills`, so the two layers cannot disagree about whether a
    sentence mentions a skill.
    """
    return {
        name
        for name in names
        if re.search(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", text)
    }


# What may sit BETWEEN two skill names and still leave them one list.
# Only the coordinators: a comma, "and", "or", an ampersand. Anything
# else — a verb, a relative clause, a semicolon — means the second name
# is doing its own work in the sentence rather than sharing the first's.
_COORDINATOR = r"(?:\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*)"


def _coordination(sentence: str, names: set[str]) -> re.Match[str] | None:
    """The first run of two or more of `names` joined only by
    coordinators — "Kubernetes and AWS", "CI/CD, Git and Linux".

    This is the span a claim outside it governs as a whole.
    """
    if len(names) < 2:
        return None
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    pattern = (
        rf"(?<![0-9A-Za-z])(?:{alternatives})"
        rf"(?:{_COORDINATOR}(?:{alternatives}))+(?![0-9A-Za-z])"
    )
    return re.search(pattern, sentence)


def _claim_distributes(sentence: str, marker: re.Match[str], named: set[str]) -> bool:
    """Does this requirement claim reach EVERY skill the clause names?

    TWO SIGNALS, both from ordinary English.

    A QUANTIFIER — "both", "each", "all of which" — says so outright,
    but only for the claim it actually governs. "<A> and <B>, both of
    which this posting requires" distributes the requirement; "the
    posting requires <A> and prefers <B>, both of which are missing"
    distributes the ABSENCE and leaves each verb with its own object.
    Both sentences contain "both"; only the first is false. So the
    quantifier has to sit just before the requirement marker to count,
    which is what `_QUANTIFIER_REACH` measures.

    A COORDINATED LIST says so by structure. When two or more skills are
    joined by nothing but commas and "and", they are one grammatical
    object, and a requirement claim sitting OUTSIDE that run governs all
    of it:

        "The posting requires Kubernetes and AWS"     -> covers both
        "Python and Git, which the role requires"     -> covers both

    Whereas a claim sitting BETWEEN two names attaches only to the first,
    because whatever separates them is no longer a coordinator:

        "Python, which this posting requires, and Git" -> covers Python
        "The posting requires Python but you bring Git" -> covers Python

    Both of the false shapes above were produced by the real model and
    accepted before this existed; both of the true ones are ordinary
    prose that has to keep working.

    Needs two skills to be a distribution at all: with one, there is
    nothing for the claim to spread to and the clause-scoped rule
    already covers it.
    """
    if any(
        0 <= marker.start() - quantifier.end() <= _QUANTIFIER_REACH
        for quantifier in _DISTRIBUTIVE.finditer(sentence)
    ):
        return True
    run = _coordination(sentence, named)
    if run is None:
        return False
    # Outside the run on either side: the claim governs the whole list.
    return marker.end() <= run.start() or marker.start() >= run.end()


def _check_consistency(explanation: MatchExplanation, facts: ExplanationFacts) -> None:
    """No sentence may contradict the decided verdict (Prompt 6.4b).

    THREE RULES, each aimed at a failure that was actually observed or
    is its exact mirror. Every one is scoped to a SENTENCE, so a
    paragraph that discusses a matched skill and a missing one is judged
    clause by clause rather than as a whole.

      1. no gap at all      the facts hold no gap, so no sentence may
                            assert one. The broadest rule and the
                            cheapest: with nothing missing, an absence
                            marker anywhere is wrong by construction.
      2. matched as missing a sentence naming only matched skills may
                            not say one of them is absent.
      3. preferred as       a clause may not say the posting demands a
         required           skill the verdict says it merely prefers or
                            mentions.

    RULE 2 REQUIRES "ONLY". A sentence naming both a matched and a
    missing skill is ordinary comparative prose — "you have Python but
    not Kubernetes" — and reading a marker in it as a claim about the
    wrong skill would reject the most useful sentence in the answer.

    RULE 3 IS SCOPED THE SAME WAY, EXCEPT WHEN A QUANTIFIER DISTRIBUTES
    THE CLAIM (6.4c). "You have <required skill>, which this posting
    requires, and <preferred skill>" scopes the requirement to the
    first. "<required skill> and <preferred skill>, both of which this
    posting requires" applies it to each — and that form was accepted 27
    times across 30 live runs before `_DISTRIBUTIVE` existed, because
    naming one genuinely required skill was enough to disarm the rule.
    """
    verdict = facts.verdict
    matched = verdict.matched_names()
    gaps = verdict.gap_names()
    required = verdict.required_names()
    non_required = verdict.non_required_names()

    for text in _generated_text(explanation):
        if not text:
            continue
        for sentence in _SENTENCE.split(text):
            # A negated absence is the opposite claim. Scrubbed first,
            # so "there are no gaps here" cannot read as asserting one.
            scrubbed = _NEGATED_ABSENCE.sub(" ", sentence)
            absence = _ABSENCE.search(scrubbed)

            if absence and not verdict.has_any_gap:
                raise ExplanationRejected(
                    RejectionReason.CONTRADICTS_FACTS, f"gap claimed with none: {absence.group()}"
                )

            named_matched = _named_in(sentence, matched)
            if absence and named_matched and not _named_in(sentence, gaps):
                raise ExplanationRejected(
                    RejectionReason.CONTRADICTS_FACTS,
                    f"matched skill called missing: {sorted(named_matched)[0]}",
                )

            named_non_required = _named_in(sentence, non_required)
            marker = _REQUIREMENT.search(sentence)
            if (
                named_non_required
                and marker
                and not _NEGATED_REQUIREMENT.search(sentence)
                # Scoped to the clause unless the claim distributes.
                # "You have <required>, which this posting requires, and
                # <preferred>" says nothing about the second; "<required>
                # and <preferred>, both of which this posting requires"
                # says something false about it.
                and (
                    not _named_in(sentence, required)
                    or _claim_distributes(
                        sentence, marker, named_non_required | _named_in(sentence, required)
                    )
                )
            ):
                raise ExplanationRejected(
                    RejectionReason.CONTRADICTS_FACTS,
                    f"non-required skill called required: {sorted(named_non_required)[0]}",
                )


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
    _check_consistency(explanation, facts)
    _check_links(explanation)

    # The returned citation set is the one recomputed from the claims,
    # never the one the model declared.
    return explanation.model_copy(update={"cited_evidence_ids": cited})
