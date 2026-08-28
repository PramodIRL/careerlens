"""Deciding whether a narrative may be shown (Prompt 6.3).

FAIL CLOSED, WHOLE. A narrative that breaks any rule below is discarded
entirely — never trimmed to the parts that passed. The route still
returns the deterministic plan, so a rejection costs the user the
wording and none of the substance.

THE BIGGEST GUARANTEE IS NOT ENFORCED HERE — it is enforced by the
shape. Items are keyed by `item_id` and steps by `step_id`, so "the
model added a skill", "dropped one", "invented a block of days" or
"reordered the plan" are not violations to catch; there is no field to
express them in. What remains for this module is narrower than 6.1's
job: the key sets must match exactly, and the prose must not smuggle in
a skill, a job, a number or a link that the facts do not contain.

THE LIMITS, STATED RATHER THAN IMPLIED. The skill check compares against
the curated taxonomy, so a fabricated technology that is not in that
~33-entry vocabulary is not detected by name. The number check spots
quantities absent from the facts, not wrong arithmetic over present
ones. Both are the same limits app/explanation/validate.py documents,
and neither is a reason to weaken what IS checked.
"""

import json
import re

from pydantic import ValidationError

from app.explanation.validate import ExplanationRejected, RejectionReason
from app.roadmap.priority import DAYS_PER_WEEK
from app.roadmap.schema import (
    MAX_RESPONSE_BYTES,
    RoadmapFacts,
    RoadmapNarrative,
)

# Matches app/explanation/validate.py. Kept as its own reference rather
# than imported so a tightening there cannot silently change the rules
# a roadmap is judged by.
# A quantity, not a digit inside a name. The lookarounds matter: without
# them "EC2" yields "2", "S3" yields "3" and "K8s" yields "8", so any
# roadmap mentioning ordinary cloud services was rejected for stating a
# number nobody supplied. The rule's intent is "do not assert a quantity
# we did not give you"; a digit inside an identifier is part of a name
# and asserts nothing. Same boundary style `_check_skills` already uses.
_NUMBER = re.compile(r"(?<![0-9A-Za-z])\d+(?:\.\d+)?(?![0-9A-Za-z])")
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)


def allowed_numbers(facts: RoadmapFacts) -> frozenset[str]:
    """Every quantity the facts actually contain.

    Built from the values, not from the serialised JSON: an id contains
    digits, and letting those count would allow almost any number
    through by coincidence.
    """
    allowed: set[str] = {
        # The shape of the plan itself is a fact about it: how many
        # items it holds and how many jobs it drew from are both
        # legitimately quotable back.
        str(len(facts.items)),
        str(len(facts.jobs)),
        str(len(facts.evidence)),
        str(facts.plan.selected_job_count),
        str(facts.plan.duration_days),
        str(facts.plan.hours_per_day),
        str(facts.plan.total_hours),
        str(facts.plan.weeks),
    }
    # The day each WEEK spans. Derived from the duration exactly as
    # app/api/v1/roadmap.py derives the label a reader sees ("Days
    # 8-14"), so a narrative referring to a week's span is quoting a
    # deterministic fact — it was simply never exposed here, and the
    # model was rejected for repeating something true.
    for week in range(1, facts.plan.weeks + 1):
        allowed.add(str((week - 1) * DAYS_PER_WEEK + 1))
        allowed.add(str(min(week * DAYS_PER_WEEK, facts.plan.duration_days)))
    for job in facts.jobs:
        allowed.add(str(job.rank))
        if job.match_score is not None:
            allowed.add(str(job.match_score))
    allowed.add(str(facts.plan.scheduled_days))
    allowed.add(str(facts.plan.unscheduled_days))
    for item in facts.items:
        allowed.update(
            {
                str(item.week),
                str(item.start_day),
                str(item.end_day),
                str(item.score),
                str(item.estimated_hours),
            }
        )
        # A step's own days and hours are deterministic facts about the
        # plan, exactly as an item's are. Omitting them would reject a
        # narrative for repeating something the schedule told it.
        for step in item.steps:
            allowed.update(
                {
                    str(step.week),
                    str(step.start_day),
                    str(step.end_day),
                    str(step.estimated_hours),
                }
            )
        # The deterministic `why` already states counts in prose ("3 of
        # your 5 selected jobs"), so those numbers are legitimately
        # quotable back.
        allowed.update(_NUMBER.findall(item.why))
    # Both spellings of every value, so "1" and "1.0" are equivalent.
    for value in list(allowed):
        try:
            number = float(value)
        except ValueError:  # pragma: no cover - defensive
            continue
        allowed.add(str(int(number)) if number.is_integer() else str(number))
        allowed.add(str(number))
    return frozenset(allowed)


def _generated_text(narrative: RoadmapNarrative) -> list[str]:
    return (
        [narrative.overview]
        + [text for week in narrative.weeks for text in (week.focus, week.checkpoint)]
        + [
            text
            for item in narrative.items
            for text in (item.task, item.outcome, item.success_criteria)
        ]
        + [text for step in narrative.steps for text in (step.task, step.done_when)]
    )


def _check_items(narrative: RoadmapNarrative, facts: RoadmapFacts) -> None:
    """The key set must match the supplied one EXACTLY.

    Not a superset and not a subset. An extra id is an item nobody
    decided on; a missing one is a decided priority silently dropped
    from the plan, which is the quieter failure and the more damaging
    one — the user would never know the skill they most need was left
    out.
    """
    returned = [item.item_id for item in narrative.items]
    if len(returned) != len(set(returned)):
        raise ExplanationRejected(RejectionReason.SCHEMA_INVALID, "duplicate item_id")

    expected = facts.item_ids()
    unknown = set(returned) - expected
    if unknown:
        raise ExplanationRejected(RejectionReason.UNKNOWN_EVIDENCE_ID, sorted(unknown)[0])
    missing = expected - set(returned)
    if missing:
        raise ExplanationRejected(RejectionReason.UNGROUNDED_CLAIM, f"missing {sorted(missing)[0]}")


def _check_steps(narrative: RoadmapNarrative, facts: RoadmapFacts) -> None:
    """The step key set must match the schedule's EXACTLY.

    Same discipline as `_check_items`, for the same two reasons. An
    extra step is a block of days nobody scheduled — and since every
    step carries hours, an invented one is work outside the budget the
    candidate declared. A missing one leaves a hole in the calendar the
    UI would render as a day with nothing in it, which is the failure
    6.4b exists to remove.
    """
    returned = [step.step_id for step in narrative.steps]
    if len(returned) != len(set(returned)):
        raise ExplanationRejected(RejectionReason.SCHEMA_INVALID, "duplicate step_id")

    expected = facts.step_ids()
    unknown = set(returned) - expected
    if unknown:
        raise ExplanationRejected(RejectionReason.UNKNOWN_EVIDENCE_ID, sorted(unknown)[0])
    missing = expected - set(returned)
    if missing:
        raise ExplanationRejected(RejectionReason.UNGROUNDED_CLAIM, f"missing {sorted(missing)[0]}")


def _check_weeks(narrative: RoadmapNarrative, facts: RoadmapFacts) -> None:
    """The week set must match the weeks the SCHEDULE produced.

    Same discipline as `_check_items`, for the same reason: a week the
    schedule did not create is a week nobody planned, and a missing one
    silently drops whatever was placed in it. Weeks with no work get no
    entry — asking a model to theme an empty week invites it to invent
    something to put there.
    """
    returned = [week.week for week in narrative.weeks]
    if len(returned) != len(set(returned)):
        raise ExplanationRejected(RejectionReason.SCHEMA_INVALID, "duplicate week")

    expected = facts.week_numbers()
    unknown = set(returned) - expected
    if unknown:
        raise ExplanationRejected(RejectionReason.UNGROUNDED_CLAIM, f"week {sorted(unknown)[0]}")
    missing = expected - set(returned)
    if missing:
        raise ExplanationRejected(
            RejectionReason.UNGROUNDED_CLAIM, f"missing week {sorted(missing)[0]}"
        )


def _check_numbers(narrative: RoadmapNarrative, facts: RoadmapFacts) -> None:
    allowed = allowed_numbers(facts)
    for text in _generated_text(narrative):
        for number in _NUMBER.findall(text):
            if number not in allowed:
                raise ExplanationRejected(RejectionReason.INVENTED_NUMBER, number)


def _check_skills(
    narrative: RoadmapNarrative, facts: RoadmapFacts, taxonomy: frozenset[str]
) -> None:
    """No skill the plan does not already contain.

    The taxonomy is the closed vocabulary the system knows and is passed
    in rather than sent to the model — handing over a list of every
    skill in the product is an invitation to use one of them.
    """
    outside = {name for name in taxonomy if name not in facts.skill_names()}
    if not outside:
        return
    # CASE-SENSITIVE, matching app/explanation/validate.py exactly.
    # Lowercasing both sides made `\bgo\b` match the verb "go", `\breact\b`
    # match "react to feedback" and `\bagile\b` match "an agile approach" —
    # so ordinary mentoring prose was rejected for naming a skill it never
    # named. Skill names are proper nouns; matching them as such is more
    # accurate, not more permissive, and it stops the two validators
    # disagreeing about the same rule.
    patterns = [
        (name, re.compile(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])")) for name in outside
    ]
    for text in _generated_text(narrative):
        for name, pattern in patterns:
            if pattern.search(text):
                raise ExplanationRejected(RejectionReason.INVENTED_SKILL, name)


def _check_jobs(narrative: RoadmapNarrative, facts: RoadmapFacts) -> None:
    """No company outside the SELECTED set.

    Jobs below the user's Top-N were never assembled into the facts, so
    this catches a model naming an employer it invented — the excluded
    ones it has never seen.
    """
    allowed = {job.company.lower() for job in facts.jobs}
    known_titles = {job.title.lower() for job in facts.jobs}
    for text in _generated_text(narrative):
        lowered = text.lower()
        for token in re.findall(r"\b[A-Z][A-Za-z0-9&.\-]{2,}\b", text):
            candidate = token.lower()
            if candidate in allowed or candidate in known_titles:
                continue
            # Only company-shaped tokens that appear in a possessive or
            # "at X" construction are treated as employer claims; the
            # rest is ordinary prose and is left to the other checks.
            if re.search(rf"\bat {re.escape(candidate)}\b", lowered):
                raise ExplanationRejected(RejectionReason.UNGROUNDED_CLAIM, token)


def _check_links(narrative: RoadmapNarrative) -> None:
    for text in _generated_text(narrative):
        if _LINK.search(text):
            raise ExplanationRejected(RejectionReason.DISALLOWED_LINK, text[:80])


def _check_evidence(narrative: RoadmapNarrative, facts: RoadmapFacts) -> None:
    """Nothing to check on the way back, and that is by design.

    Evidence ids are attached to items DETERMINISTICALLY (see
    app/roadmap/facts.py) and the narrative has no field for them, so a
    citation to an unavailable row is unrepresentable. This function
    exists to say so where a reader would look for the check.
    """
    unknown = {
        evidence_id
        for item in facts.items
        for evidence_id in item.evidence_ids
        if evidence_id not in facts.evidence_ids()
    }
    if unknown:  # pragma: no cover - defensive; facts.py builds both sides
        raise ExplanationRejected(RejectionReason.UNKNOWN_EVIDENCE_ID, str(sorted(unknown)[0]))


def validate_narrative(
    raw: str,
    *,
    facts: RoadmapFacts,
    taxonomy: frozenset[str],
) -> RoadmapNarrative:
    """Raw provider text in, a grounded `RoadmapNarrative` out.

    Raises `ExplanationRejected` at the first failure, reusing 6.1's
    reason vocabulary so a client has one set of values to understand
    across both features rather than two that nearly agree.
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
        narrative = RoadmapNarrative.model_validate(payload)
    except ValidationError as error:
        raise ExplanationRejected(RejectionReason.SCHEMA_INVALID, str(error)) from error

    _check_items(narrative, facts)
    _check_steps(narrative, facts)
    _check_weeks(narrative, facts)
    _check_evidence(narrative, facts)
    _check_links(narrative)
    _check_numbers(narrative, facts)
    _check_skills(narrative, facts, taxonomy)
    _check_jobs(narrative, facts)
    return narrative


__all__ = ["ExplanationRejected", "RejectionReason", "validate_narrative"]
