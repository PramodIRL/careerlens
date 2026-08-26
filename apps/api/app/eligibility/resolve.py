"""The single place that decides whether a candidate clears one job
eligibility bar (Prompt 5.1a).

PURE: requirement values and candidate facts in, resolutions out. No
database, no I/O, no scoring — the same split app/matching/resolve.py
has, and for the same reason: the judgement lives in one place so the
`/eligibility` endpoint and anything built on it later cannot drift into
disagreeing.

IT COMPUTES NO SCORE AND TOUCHES NEITHER `skill_match_v1` NOR
`skill_gap_v1`. Eligibility is a separate, deterministic answer sitting
beside the skill match, never multiplied into it and never capping it. A
combined number would have to weigh "CGPA 7.5" against "knows Docker",
which nothing in this product can justify — and it would destroy exactly
the explainability this domain exists to provide.

THE TWO NON-VERDICTS ARE THE POINT OF THIS MODULE.

    UNKNOWN        the CANDIDATE has not told us something.
                   Actionable by them.
    UNDETERMINED   the REQUIREMENT cannot be evaluated as written —
                   today, a list the posting hedged open with "or a
                   related field". Not actionable by the candidate at
                   all.

Neither is a failure, and neither may be reported as one. An empty
profile field is not a rejection: turning "we do not know your CGPA"
into "you do not qualify" invents a fact about a person and then acts on
it, which is the precise failure docs/project-brief.md's Evidence-First
rule exists to prevent. Only NOT_SATISFIED — a value we actually hold,
compared against a bar we actually understood — makes anyone ineligible.
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from app.eligibility.classify import DEFAULT_CGPA_SCALE
from app.schemas.eligibility import (
    Comparator,
    EligibilityFlag,
    EligibilityRequirementType,
    EligibilityState,
)

# Bumped whenever this resolution changes. Separate from
# `skill_match_v1` and `skill_gap_v1` on purpose: the three answer
# different questions and must be free to version independently.
FORMULA_VERSION = "eligibility_v1"

LEVEL_REQUIRED = "required"


class Reason:
    """Stable machine-readable tokens explaining a state.

    Tokens, never sentences: the API owns the fact and the UI owns the
    wording, so a copy change never becomes an API change — and a client
    can branch on these without parsing prose.
    """

    SATISFIED = "satisfied"
    BELOW_THRESHOLD = "below_threshold"
    ABOVE_THRESHOLD = "above_threshold"
    OUTSIDE_RANGE = "outside_range"
    VALUE_MISMATCH = "value_mismatch"
    NOT_IN_ACCEPTED_VALUES = "not_in_accepted_values"
    CANDIDATE_VALUE_UNKNOWN = "candidate_value_unknown"
    OPEN_ENDED_LIST = "open_ended_list"
    UNSUPPORTED_COMPARATOR = "unsupported_comparator"


@dataclass(frozen=True)
class RequirementInput:
    """One stored eligibility requirement, reduced to what the decision
    needs. Plain values rather than an ORM row, so this stays testable
    without a database."""

    requirement_type: EligibilityRequirementType
    comparator: Comparator
    requirement_level: str
    numeric_value: Decimal | None = None
    numeric_max: Decimal | None = None
    value_scale: Decimal | None = None
    accepted_values: tuple[str, ...] = ()
    open_ended: bool = False
    excerpt: str = ""
    requirement_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CandidateFact:
    """One thing the candidate has told us. Absent from the mapping
    entirely when unknown — there is no "empty" fact, because an empty
    fact is indistinguishable from a zero."""

    value_numeric: Decimal | None = None
    value_text: str | None = None
    value_scale: Decimal | None = None


@dataclass(frozen=True)
class Resolution:
    """One requirement, resolved. Carries the candidate's own value so a
    caller can render "7.8 ≥ 7.5" rather than a bare verdict."""

    requirement: RequirementInput
    state: EligibilityState
    reason: str
    candidate_numeric: Decimal | None = None
    candidate_text: str | None = None
    candidate_scale: Decimal | None = None

    @property
    def blocks(self) -> bool:
        """Whether this alone makes the candidate ineligible.

        ONLY a hard requirement the candidate demonstrably fails. Not an
        unknown, not an undetermined, and not a failed preference.
        """
        return (
            self.state is EligibilityState.NOT_SATISFIED
            and self.requirement.requirement_level == LEVEL_REQUIRED
        )


@dataclass
class EligibilityTotals:
    """Counts across the four states. Deliberately not a percentage —
    see app/schemas/eligibility.py's EligibilityFlag."""

    satisfied: int = 0
    not_satisfied: int = 0
    unknown: int = 0
    undetermined: int = 0
    total_requirements: int = 0
    required_not_satisfied: int = 0


@dataclass
class EligibilityResult:
    formula_version: str = FORMULA_VERSION
    flag: EligibilityFlag = EligibilityFlag.UNKNOWN
    has_requirements: bool = False
    totals: EligibilityTotals = field(default_factory=EligibilityTotals)
    resolutions: list[Resolution] = field(default_factory=list)


def _normalized_cgpa(
    requirement: RequirementInput, fact: CandidateFact
) -> tuple[Decimal, Decimal] | str:
    """Put a CGPA requirement and a candidate CGPA on one scale.

    Returns (candidate_fraction, requirement_fraction) when they are
    comparable, or a Reason token when they are not.

    EXPLICIT SCALE WINS; AN UNSTATED ONE IS 10. Rescaling two stated
    scales is plain arithmetic — 7.5/10 and 3.0/4 are the same fraction.
    Where a scale is absent the product rule supplies 10 rather than
    refusing to compare, because the alternative leaves the commonest
    phrasing of all ("minimum CGPA 7.5", "CGPA: 8.2") permanently
    unresolvable.

    The job side never actually reaches this branch: the extractor
    stores the default on the row, so the assumption is visible in the
    API response rather than hidden here. The fallback stays for rows
    written before that behaviour existed, and for a candidate who typed
    a CGPA without picking a scale.
    """
    if requirement.numeric_value is None:  # pragma: no cover - defensive
        return Reason.CANDIDATE_VALUE_UNKNOWN
    if fact.value_numeric is None:
        return Reason.CANDIDATE_VALUE_UNKNOWN
    requirement_scale = requirement.value_scale or DEFAULT_CGPA_SCALE
    candidate_scale = fact.value_scale or DEFAULT_CGPA_SCALE
    if requirement_scale <= 0 or candidate_scale <= 0:  # pragma: no cover - defensive
        return Reason.CANDIDATE_VALUE_UNKNOWN
    return (
        fact.value_numeric / candidate_scale,
        requirement.numeric_value / requirement_scale,
    )


def _compare_numeric(
    requirement: RequirementInput, candidate: Decimal, threshold: Decimal
) -> tuple[EligibilityState, str]:
    """Apply the comparator. Exact equality at the boundary SATISFIES a
    `gte`: "minimum 7.5" includes 7.5, and a candidate sitting exactly on
    the bar has met it."""
    if requirement.comparator is Comparator.GTE:
        if candidate >= threshold:
            return EligibilityState.SATISFIED, Reason.SATISFIED
        return EligibilityState.NOT_SATISFIED, Reason.BELOW_THRESHOLD
    if requirement.comparator is Comparator.LTE:
        if candidate <= threshold:
            return EligibilityState.SATISFIED, Reason.SATISFIED
        return EligibilityState.NOT_SATISFIED, Reason.ABOVE_THRESHOLD
    if requirement.comparator is Comparator.EQ:
        if candidate == threshold:
            return EligibilityState.SATISFIED, Reason.SATISFIED
        return EligibilityState.NOT_SATISFIED, Reason.VALUE_MISMATCH
    if requirement.comparator is Comparator.BETWEEN:
        upper = requirement.numeric_max
        if upper is None:  # pragma: no cover - defensive
            return EligibilityState.UNDETERMINED, Reason.UNSUPPORTED_COMPARATOR
        if threshold <= candidate <= upper:
            return EligibilityState.SATISFIED, Reason.SATISFIED
        return EligibilityState.NOT_SATISFIED, Reason.OUTSIDE_RANGE
    # A comparator this deployment has not learned yet. Undetermined
    # rather than an exception: the vocabulary is stored as plain text
    # precisely so it can grow, and an eligibility endpoint should not
    # 500 because a newer writer used a comparator it does not know.
    return EligibilityState.UNDETERMINED, Reason.UNSUPPORTED_COMPARATOR


def resolve_requirement(requirement: RequirementInput, fact: CandidateFact | None) -> Resolution:
    """Resolve one requirement against one candidate fact.

    Order is load-bearing. The requirement's own evaluability is checked
    BEFORE the candidate is consulted, so a posting that wrote an
    unusable bar never produces a verdict about a person — not even an
    UNKNOWN that implies they could fix it by filling in a field.
    """
    # 1. Do we know anything about the candidate here?
    if fact is None:
        return Resolution(
            requirement=requirement,
            state=EligibilityState.UNKNOWN,
            reason=Reason.CANDIDATE_VALUE_UNKNOWN,
        )

    # 2. Categorical membership.
    if requirement.comparator is Comparator.IN:
        if fact.value_text is None:
            return Resolution(
                requirement=requirement,
                state=EligibilityState.UNKNOWN,
                reason=Reason.CANDIDATE_VALUE_UNKNOWN,
            )
        if fact.value_text in requirement.accepted_values:
            return Resolution(
                requirement=requirement,
                state=EligibilityState.SATISFIED,
                reason=Reason.SATISFIED,
                candidate_text=fact.value_text,
            )
        if requirement.open_ended:
            # "or a related field". The posting hedged its own list
            # open and there is no deterministic membership test for
            # "related", so this is not a rejection we are entitled to
            # make.
            return Resolution(
                requirement=requirement,
                state=EligibilityState.UNDETERMINED,
                reason=Reason.OPEN_ENDED_LIST,
                candidate_text=fact.value_text,
            )
        return Resolution(
            requirement=requirement,
            state=EligibilityState.NOT_SATISFIED,
            reason=Reason.NOT_IN_ACCEPTED_VALUES,
            candidate_text=fact.value_text,
        )

    # 3. Numeric comparison, with CGPA first because it is the one that
    #    needs both sides on a common scale.
    if requirement.requirement_type is EligibilityRequirementType.CGPA:
        normalized = _normalized_cgpa(requirement, fact)
        if isinstance(normalized, str):
            # Only reachable when the candidate has no CGPA at all — a
            # missing scale no longer blocks the comparison.
            return Resolution(
                requirement=requirement,
                state=EligibilityState.UNKNOWN,
                reason=normalized,
                candidate_numeric=fact.value_numeric,
                candidate_scale=fact.value_scale,
            )
        candidate_fraction, threshold_fraction = normalized
        state, reason = _compare_numeric(requirement, candidate_fraction, threshold_fraction)
        return Resolution(
            requirement=requirement,
            state=state,
            reason=reason,
            candidate_numeric=fact.value_numeric,
            candidate_scale=fact.value_scale,
        )

    if fact.value_numeric is None or requirement.numeric_value is None:
        return Resolution(
            requirement=requirement,
            state=EligibilityState.UNKNOWN,
            reason=Reason.CANDIDATE_VALUE_UNKNOWN,
            candidate_numeric=fact.value_numeric,
        )

    state, reason = _compare_numeric(requirement, fact.value_numeric, requirement.numeric_value)
    return Resolution(
        requirement=requirement,
        state=state,
        reason=reason,
        candidate_numeric=fact.value_numeric,
    )


def resolve_eligibility(
    requirements: list[RequirementInput],
    facts: dict[EligibilityRequirementType, CandidateFact],
) -> EligibilityResult:
    """Resolve every requirement, then decide the whole-job flag.

    THE FLAG IS CONSERVATIVE IN THE DIRECTION THAT PROTECTS THE
    CANDIDATE. `eligible` is claimed only when every hard requirement is
    positively satisfied — one unknown is enough to hold the answer at
    `unknown`, because "we checked and you qualify" is a much stronger
    statement than "nothing we checked ruled you out".
    """
    result = EligibilityResult()
    if not requirements:
        # No recognised bars. NOT "eligible": we have verified nothing,
        # and saying otherwise would claim a check we never ran. The
        # same distinction `has_requirements` draws on the match side.
        return result

    result.has_requirements = True
    result.resolutions = [
        resolve_requirement(requirement, facts.get(requirement.requirement_type))
        for requirement in requirements
    ]

    totals = EligibilityTotals(total_requirements=len(result.resolutions))
    for resolution in result.resolutions:
        if resolution.state is EligibilityState.SATISFIED:
            totals.satisfied += 1
        elif resolution.state is EligibilityState.NOT_SATISFIED:
            totals.not_satisfied += 1
            if resolution.requirement.requirement_level == LEVEL_REQUIRED:
                totals.required_not_satisfied += 1
        elif resolution.state is EligibilityState.UNKNOWN:
            totals.unknown += 1
        else:
            totals.undetermined += 1
    result.totals = totals

    if totals.required_not_satisfied:
        result.flag = EligibilityFlag.NOT_ELIGIBLE
    elif any(
        resolution.requirement.requirement_level == LEVEL_REQUIRED
        and resolution.state in {EligibilityState.UNKNOWN, EligibilityState.UNDETERMINED}
        for resolution in result.resolutions
    ):
        result.flag = EligibilityFlag.UNKNOWN
    else:
        result.flag = EligibilityFlag.ELIGIBLE
    return result
