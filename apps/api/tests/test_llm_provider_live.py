"""The one test that needs a real model (Prompt 6.4).

SKIPS WHEN OLLAMA OR THE MODEL IS UNAVAILABLE, rather than failing.
Exactly the convention tests/test_semantic_fit.py established for the
real embedding model: a model that has not been pulled is an
ENVIRONMENT FACT, not a defect in this code, and CI has no daemon.
Nothing here runs in CI and nothing here is required to pass before a
merge.

DELIBERATELY SMALL. This is not a benchmark and must not grow into one.
It asserts the single thing a mock genuinely cannot tell you: that
output from a real model, under real constrained decoding, survives the
existing grounding validator unchanged. Prose QUALITY is a judgement
call for a person reading the dashboard, not an assertion.

    make llm-smoke     # or: uv run pytest tests/test_llm_provider_live.py -v
"""

import asyncio
import uuid

import pytest

from app.explanation.adapter import explain
from app.explanation.ollama_provider import check_available
from app.explanation.prompt import build_request
from app.explanation.provider import get_explanation_provider
from app.explanation.schema import (
    EvidenceFact,
    ExplanationFacts,
    GapFacts,
    JobFacts,
    LevelFact,
    MatchExplanation,
    ScoreFacts,
    SemanticFacts,
    SkillFact,
    VerdictFacts,
)
from app.explanation.validate import (
    ExplanationRejected,
    RejectionReason,
    validate_explanation,
)
from app.roadmap.adapter import narrate
from app.roadmap.facts import build_facts as build_roadmap_facts
from app.roadmap.priority import (
    GapState,
    JobDemand,
    Schedule,
    SelectedJob,
    rank_items,
    schedule_items,
)
from app.roadmap.provider import get_roadmap_provider
from app.roadmap.schema import RoadmapFacts
from app.settings import Settings

_TAXONOMY = frozenset({"Python", "PostgreSQL", "Docker", "Kubernetes", "AWS"})
_EVIDENCE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


# Reasons whose `detail` is safe to surface in a test failure message.
#
# `ExplanationRejected.detail` is NOT uniformly safe: for a malformed or
# schema-invalid response it carries parser output including fragments
# of what the model wrote, and for an ungrounded claim or a disallowed
# link it carries the model's prose. Those must not be printed, for the
# same reason app/explanation/adapter.py refuses to log them.
#
# The four below carry a bare identifier, a taxonomy term, a number or a
# byte count — no candidate text, no resume, no README, no model prose.
# Whitelisted rather than filtered, so a reason added later is silent by
# default instead of leaking by default.
_SAFE_DETAIL_REASONS = frozenset(
    {
        RejectionReason.UNKNOWN_EVIDENCE_ID,
        RejectionReason.INVENTED_SKILL,
        RejectionReason.INVENTED_NUMBER,
        RejectionReason.RESPONSE_TOO_LARGE,
    }
)


def describe_rejection(error: ExplanationRejected) -> str:
    """A failure message that says enough to debug and no more.

    For an unknown-id rejection this IS the diagnosis: the identifier
    the model produced. It is an opaque token, so printing it reveals
    nothing about the candidate — unlike the parser output or model
    prose the other reasons carry, which stay hidden.
    """
    if error.reason in _SAFE_DETAIL_REASONS:
        return f"{error.reason.value}: {error.detail}"
    return f"{error.reason.value} (detail withheld — may contain model or candidate text)"


def _live_settings() -> Settings:
    return Settings(explanation_provider="ollama")


def _available() -> bool:
    return asyncio.run(check_available(_live_settings()))


# The whole module skips in one place, so a machine without a daemon
# sees one clear reason rather than a wall of identical skips.
pytestmark = pytest.mark.skipif(
    not _available(),
    reason="ollama or the configured model is unavailable — run `ollama serve` and pull it",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _facts() -> ExplanationFacts:
    return ExplanationFacts(
        job=JobFacts(saved_job_id=uuid.uuid4(), title="Backend Engineer", company="Acme"),
        score=ScoreFacts(
            formula_version="skill_match_v1",
            overall_score=75,
            earned_weight=6,
            obtainable_weight=8,
            has_requirements=True,
            required_matched=2,
            required_total=2,
            by_level={"required": LevelFact(matched=2, total=2)},
            weights={"required": 3},
        ),
        matched_skills=[
            SkillFact(
                skill_name="Python",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            )
        ],
        missing_required_skills=[
            SkillFact(skill_name="Docker", requirement_level="required", candidate_status=None)
        ],
        gaps=GapFacts(formula_version="skill_gap_v1"),
        semantic=SemanticFacts(
            formula_version="semantic_fit_v1",
            fit=0,
            band="none",
            model_identifier="mock-deterministic-v1",
            considered=0,
        ),
        evidence=[
            EvidenceFact(
                evidence_id=_EVIDENCE_ID,
                source_type="resume",
                source_identifier="resume-1",
                excerpt="Built backend services in Python",
            )
        ],
    )


@pytest.mark.anyio
async def test_a_real_model_passes_the_existing_validator() -> None:
    """THE ONLY THING A MOCK CANNOT TELL YOU.

    A rejection here is not a bug in the validator — grounding is not
    loosened to accommodate a smaller model. It means the prompt needs
    tuning, and the reason on the outcome says which rule was broken.
    """
    settings = _live_settings()
    facts = _facts()
    outcome = await explain(
        facts,
        provider=get_explanation_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )

    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.explanation is not None
    assert outcome.explanation.summary
    # Grounded: every citation is a row that was supplied.
    for claim in outcome.explanation.strengths:
        assert set(claim.evidence_ids) <= {_EVIDENCE_ID}


@pytest.mark.anyio
async def test_the_real_model_does_not_write_the_mock_s_sentences() -> None:
    """6.4 exists to replace templated prose, not to rephrase it. If a
    real model still emits the mock's exact wording, something is
    wired wrong — most likely the provider never switched."""
    settings = _live_settings()
    outcome = await explain(
        _facts(),
        provider=get_explanation_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )

    assert outcome.provider == "ollama"
    assert outcome.explanation is not None
    assert "You already have evidence for" not in outcome.explanation.summary


@pytest.mark.anyio
async def test_the_model_copies_evidence_ids_exactly() -> None:
    """THE REGRESSION THIS SLICE EXISTS FOR.

    Goes provider -> validator directly rather than through `explain`,
    because the adapter deliberately discards `ExplanationRejected.detail`
    — it can quote model output and has no business in a response or a
    log. Here, in a test, the whitelisted reasons may surface it, and for
    an unknown id that detail is the whole answer: the identifier the
    model produced, beside the ones it was given.

    Before the schema was pinned, a 7B model transcribing a
    36-character UUID produced a well-formed id that existed nowhere in
    the facts. The enum now makes that unrepresentable; this asserts it
    stays that way against a real model rather than a mocked transport.
    """
    settings = _live_settings()
    facts = _facts()
    provider = get_explanation_provider(settings)
    raw = await provider.complete(build_request(facts))

    try:
        explanation = validate_explanation(raw, facts=facts, taxonomy=_TAXONOMY)
    except ExplanationRejected as error:
        supplied = sorted(str(key) for key in facts.evidence_by_id())
        pytest.fail(f"{describe_rejection(error)} | supplied: {supplied}")

    cited = {row for claim in explanation.strengths for row in claim.evidence_ids}
    assert cited <= set(facts.evidence_by_id())


# Words that condition advice on actually earning the skill first.
# "Once you have built it, add it to your profile" is honest; "add it to
# your profile" alone is advice to misrepresent.
_CONDITIONING = ("once", "after", "when you", "then ", "having ")


@pytest.mark.anyio
async def test_the_model_does_not_advise_claiming_an_unearned_skill() -> None:
    """THE FAILURE NO VALIDATOR CATCHES.

    Browser testing produced "Consider adding AWS and Kubernetes to your
    tech stack and resume" for skills the candidate does not have. It is
    perfectly grounded — every name was in the facts — and it is advice
    to misrepresent yourself, which is the one thing this product must
    never do.

    A HEURISTIC, DELIBERATELY NOT A VALIDATOR RULE. Detecting "advises
    claiming something unearned" means regex over prose: it would both
    miss real cases and reject honest phrasings, and a grounding rule
    that fails closed on a false positive costs a user their
    explanation. Honesty belongs in the prompt; this asserts the prompt
    is working, and gates nothing at runtime.
    """
    settings = _live_settings()
    facts = _facts()
    outcome = await explain(
        facts,
        provider=get_explanation_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )
    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.explanation is not None

    missing = {fact.skill_name for fact in facts.missing_required_skills}
    for step in outcome.explanation.next_steps:
        lowered = step.lower()
        mentions_profile = "resume" in lowered or "profile" in lowered
        names_a_missing_skill = any(name in step for name in missing)
        if mentions_profile and names_a_missing_skill:
            assert any(word in lowered for word in _CONDITIONING), (
                "advises adding a skill the candidate does not have, "
                f"without conditioning it on earning it first: {step!r}"
            )


@pytest.mark.anyio
async def test_the_model_does_not_leak_internal_names() -> None:
    """Browser testing produced "mentioned as informational gaps" — a
    CareerLens bucket name a student cannot interpret. The prose has to
    say what the categories MEAN."""
    settings = _live_settings()
    outcome = await explain(
        _facts(),
        provider=get_explanation_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )
    assert outcome.explanation is not None

    prose = " ".join([outcome.explanation.summary, *outcome.explanation.next_steps]).lower()
    for leaked in ("skill_match_v1", "skill_gap_v1", "informational_gaps", "needs_confirmation"):
        assert leaked not in prose, f"leaked an internal name: {leaked}"


# =====================================================================
# 6.4b — the two failures the browser test found
#
# Both needed a real model to appear, and both stay here for the same
# reason the evidence-id test does: the mock cannot reproduce either.
# A templated fake never reaches for a skill to improve when there is
# nothing to improve, and never writes a learning sequence.
# =====================================================================


def _complete_match_facts() -> ExplanationFacts:
    """The shape that produced the contradiction: everything matched,
    nothing missing, and one skill the posting only MENTIONS.

    The live version of the offline regression in
    tests/test_llm_grounding_regressions.py — same shape, real decoder.
    """
    return ExplanationFacts(
        job=JobFacts(saved_job_id=uuid.uuid4(), title="Frontend Engineer", company="Acme"),
        score=ScoreFacts(
            formula_version="skill_match_v1",
            overall_score=100,
            earned_weight=9,
            obtainable_weight=9,
            has_requirements=True,
            required_matched=2,
            required_total=2,
            by_level={"required": LevelFact(matched=2, total=2)},
            weights={"required": 3},
        ),
        matched_skills=[
            SkillFact(
                skill_name="Python",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            ),
            SkillFact(
                skill_name="PostgreSQL",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            ),
            SkillFact(
                skill_name="Docker",
                requirement_level="mentioned",
                candidate_status="confirmed",
                evidence_ids=[],
            ),
        ],
        gaps=GapFacts(formula_version="skill_gap_v1"),
        semantic=SemanticFacts(
            formula_version="semantic_fit_v1",
            fit=0,
            band="none",
            model_identifier="mock-deterministic-v1",
            considered=0,
        ),
        evidence=[
            EvidenceFact(
                evidence_id=_EVIDENCE_ID,
                source_type="resume",
                source_identifier="resume-1",
                excerpt="Built and shipped backend services in Python on PostgreSQL",
            )
        ],
    )


def _multi_gap_facts() -> ExplanationFacts:
    """Three gaps at two different requirement levels — the case where
    calling a preferred skill required is most tempting."""
    return ExplanationFacts(
        job=JobFacts(saved_job_id=uuid.uuid4(), title="Platform Engineer", company="Acme"),
        score=ScoreFacts(
            formula_version="skill_match_v1",
            overall_score=33,
            earned_weight=3,
            obtainable_weight=9,
            has_requirements=True,
            required_matched=1,
            required_total=2,
            by_level={"required": LevelFact(matched=1, total=2)},
            weights={"required": 3, "preferred": 2},
        ),
        matched_skills=[
            SkillFact(
                skill_name="Python",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            )
        ],
        missing_required_skills=[SkillFact(skill_name="Kubernetes", requirement_level="required")],
        missing_other_skills=[
            SkillFact(skill_name="AWS", requirement_level="preferred"),
            SkillFact(skill_name="Docker", requirement_level="mentioned"),
        ],
        gaps=GapFacts(
            formula_version="skill_gap_v1",
            required_gaps=["Kubernetes"],
            preferred_gaps=["AWS"],
            informational_gaps=["Docker"],
        ),
        semantic=SemanticFacts(
            formula_version="semantic_fit_v1",
            fit=0,
            band="none",
            model_identifier="mock-deterministic-v1",
            considered=0,
        ),
        evidence=[
            EvidenceFact(
                evidence_id=_EVIDENCE_ID,
                source_type="resume",
                source_identifier="resume-1",
                excerpt="Built backend services in Python",
            )
        ],
    )


@pytest.mark.anyio
async def test_a_complete_match_produces_no_invented_gap() -> None:
    """THE 6.4 FAILURE, against the real decoder.

    Observed: a 100% match with nothing missing, explained as "the main
    area to improve is REST APIs, as this is a required skill for the
    role" — about a skill that was MATCHED and merely MENTIONED.

    Two defences are being exercised at once. `summary_gap` and `gaps`
    are pinned shut in the schema, so the sentence is unrepresentable;
    and `_check_consistency` would refuse it independently if it were
    not. A `generated` outcome here means neither had to fire.
    """
    settings = _live_settings()
    facts = _complete_match_facts()
    assert facts.verdict.has_any_gap is False

    outcome = await explain(
        facts,
        provider=get_explanation_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )

    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.explanation is not None
    # STRUCTURALLY EMPTY, not merely unfilled.
    assert outcome.explanation.summary_gap is None
    assert outcome.explanation.gaps == []
    # And nothing anywhere reads as an absence.
    prose = " ".join([outcome.explanation.summary, *outcome.explanation.next_steps]).lower()
    for word in ("missing", "does not have", "lacking", "area to improve"):
        assert word not in prose, f"invented a gap on a complete match: {word!r} in {prose!r}"


@pytest.mark.anyio
async def test_a_mentioned_skill_is_never_called_required() -> None:
    """The other half of the same failure: a skill the posting merely
    mentions, described as one it demands."""
    settings = _live_settings()
    outcome = await explain(
        _complete_match_facts(),
        provider=get_explanation_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )
    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.explanation is not None

    prose = " ".join([outcome.explanation.summary, *outcome.explanation.next_steps])
    for sentence in prose.split("."):
        if "Docker" in sentence and "Python" not in sentence and "PostgreSQL" not in sentence:
            lowered = sentence.lower()
            assert "required" not in lowered and "must-have" not in lowered, sentence


@pytest.mark.anyio
async def test_a_multi_gap_job_keeps_each_gap_at_its_own_level() -> None:
    """RETRIED, LIKE THE 6.4c SHAPES (see `_LIVE_ATTEMPTS`).

    The multi-gap verdict is the least stable of the shapes measured:
    the model gets each gap right on its own line and sometimes merges
    them in the summary, which `_check_consistency` then refuses. A
    single generation asserted here flakes on that; three with two
    required still catches a systematically broken shape, and the
    rejection it tolerates is FAIL-CLOSED — the score, the categories
    and the gap list all survive it.
    """
    settings = _live_settings()
    facts = _multi_gap_facts()
    accepted = []
    refusals = []
    for _ in range(_LIVE_ATTEMPTS):
        outcome = await explain(
            facts,
            provider=get_explanation_provider(settings),
            taxonomy=_TAXONOMY,
            settings=settings,
        )
        if outcome.status == "generated" and outcome.explanation is not None:
            accepted.append(outcome.explanation)
        else:
            refusals.append(outcome.reason)

    assert len(accepted) >= _LIVE_REQUIRED, f"refused as {refusals}"
    # A real gap must still be describable — the check must not have
    # made honest advice unwritable.
    for explanation in accepted:
        assert explanation.summary_gap is not None
    # The deterministic result is untouched by having been explained.
    assert facts.score.overall_score == 33
    assert facts.verdict.required_missing == ["Kubernetes"]
    assert facts.verdict.preferred_missing == ["AWS"]


# --- the roadmap half of 6.4b -----------------------------------------
#
# A REAL MODEL IS THE ONLY WAY TO CHECK THIS. The deterministic layer
# decides which skill, in what order, on which days, in which mode; what
# it cannot decide is which sub-topics of AWS matter first or what is
# worth building. That judgement is the whole point of the model, and a
# templated mock has none of it.


def _roadmap_plan(
    duration_days: int, hours_per_day: float, states: list[GapState]
) -> tuple[Schedule, RoadmapFacts]:
    jobs = [
        SelectedJob(
            saved_job_id=uuid.UUID(int=0, version=4),
            title="Platform Engineer",
            company="Acme",
            rank=1,
            match_score=40,
        )
    ]
    names = {
        uuid.UUID(int=index + 10, version=4): name
        for index, name in enumerate(("AWS", "Kubernetes", "Docker")[: len(states)])
    }
    demands = {
        skill_id: [JobDemand(jobs[0].saved_job_id, state)]
        for skill_id, state in zip(names, states, strict=True)
    }
    ranked = rank_items(demands, skill_names=names, selected_jobs=jobs)
    schedule = schedule_items(ranked, duration_days=duration_days, hours_per_day=hours_per_day)
    return schedule, build_roadmap_facts(
        schedule=schedule,
        selected=jobs,
        evidence_by_skill={},
        duration_days=duration_days,
        hours_per_day=hours_per_day,
        total_hours=round(duration_days * hours_per_day, 1),
    )


@pytest.mark.anyio
async def test_a_long_plan_becomes_a_day_by_day_journey() -> None:
    """THE REPORTED FAILURE. 28 days and two gaps used to produce two
    fourteen-day blocks and two weeks reading "nothing scheduled".

    What is asserted here is what a mock cannot supply: a real sentence
    per block of days, differing between blocks. Prose QUALITY remains a
    person's judgement — this checks the plan is a sequence rather than
    one instruction repeated.
    """
    settings = _live_settings()
    schedule, facts = _roadmap_plan(28, 2.0, [GapState.MISSING_REQUIRED, GapState.MISSING_REQUIRED])
    assert facts.plan.coverage == "full"
    assert sorted(facts.week_numbers()) == [1, 2, 3, 4]

    outcome = await narrate(
        facts,
        provider=get_roadmap_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )

    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.narrative is not None
    # One entry per decided block of days, and one per week that holds
    # work — every week of the four, which is the fix.
    assert {step.step_id for step in outcome.narrative.steps} == facts.step_ids()
    assert {week.week for week in outcome.narrative.weeks} == {1, 2, 3, 4}
    # A SEQUENCE, NOT ONE INSTRUCTION REPEATED.
    #
    # Within an item this is the contract — "steps of one item must
    # PROGRESS" — and is asserted strictly. Across items it is a style
    # rule the prompt states and a small model occasionally misses on
    # the last rung, so it is asserted as a proportion: a form letter
    # still fails, one shared self-check sentence does not flake the
    # suite.
    by_item: dict[str, list[str]] = {}
    for step in outcome.narrative.steps:
        by_item.setdefault(step.step_id.rsplit(":", 1)[0], []).append(step.task)
    for item_id, tasks in by_item.items():
        assert len(set(tasks)) == len(tasks), f"{item_id} repeated a step verbatim: {tasks}"

    everything = [step.task for step in outcome.narrative.steps]
    assert len(set(everything)) >= len(everything) - 1, (
        f"the plan reads as a form letter: {everything}"
    )
    assert all(step.done_when.strip() for step in outcome.narrative.steps)
    # Every item still says what gets built and what to be able to
    # explain afterwards.
    for item in outcome.narrative.items:
        assert item.task.strip() and item.outcome.strip() and item.success_criteria.strip()


@pytest.mark.anyio
async def test_a_two_day_intensive_is_valid_and_proportionate() -> None:
    """A 2-day plan must work, and must not read like a 28-day one."""
    settings = _live_settings()
    _schedule, facts = _roadmap_plan(2, 3.0, [GapState.MISSING_REQUIRED])
    assert facts.plan.weeks == 1
    assert len(facts.items[0].steps) == 1

    outcome = await narrate(
        facts,
        provider=get_roadmap_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )

    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.narrative is not None
    assert len(outcome.narrative.steps) == 1
    assert len(outcome.narrative.weeks) == 1


@pytest.mark.anyio
async def test_weak_evidence_asks_for_evidence_not_for_learning() -> None:
    """WEAK IS NOT MISSING. The candidate may already have the skill,
    and the deterministic ladder hands the model evidence rungs rather
    than learning ones so the prose cannot drift."""
    settings = _live_settings()
    _schedule, facts = _roadmap_plan(14, 2.0, [GapState.WEAK_EVIDENCE])
    phases = {step.phase for step in facts.items[0].steps}
    assert phases <= {"demonstrate", "document", "prove"}

    outcome = await narrate(
        facts,
        provider=get_roadmap_provider(settings),
        taxonomy=_TAXONOMY,
        settings=settings,
    )

    assert outcome.status == "generated", f"rejected: {outcome.reason}"
    assert outcome.narrative is not None
    prose = " ".join(step.task for step in outcome.narrative.steps).lower()
    # A heuristic, not a validator rule — the same posture as the
    # unearned-skill check above. It gates nothing at runtime.
    assert "learn the basics" not in prose
    assert "start learning" not in prose


# =====================================================================
# 6.4c — every verdict shape, against the real model
#
# 6.4b's live cases covered the complete match and a single required
# gap, which are the two shapes where every matched skill happens to be
# REQUIRED. The shapes where one is merely preferred or mentioned were
# not covered, and that is exactly where the instruction's own worked
# examples were putting "required" into the model's mouth: 30 live
# generations before the fix produced 5 rejections and 27 sentences
# asserting a requirement the verdict did not support.
#
# ASSERTS ACCEPTANCE AND NON-CONTRADICTION ONLY. Prose quality is a
# person's judgement reading the dashboard, and a smoke suite this small
# cannot say anything statistical about a model.
# =====================================================================


def _shape(matched: list[tuple[str, str]], missing: list[tuple[str, str]]) -> ExplanationFacts:
    """A fact bundle from `(skill, requirement_level)` pairs."""
    return ExplanationFacts(
        job=JobFacts(saved_job_id=uuid.uuid4(), title="Engineer", company="Acme"),
        score=ScoreFacts(
            formula_version="skill_match_v1",
            overall_score=75,
            earned_weight=6,
            obtainable_weight=8,
            has_requirements=True,
            required_matched=sum(1 for _, level in matched if level == "required"),
            required_total=sum(1 for _, level in matched + missing if level == "required"),
            by_level={"required": LevelFact(matched=1, total=1)},
            weights={"required": 3, "preferred": 2, "mentioned": 1},
        ),
        matched_skills=[
            SkillFact(
                skill_name=name,
                requirement_level=level,
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            )
            for name, level in matched
        ],
        missing_required_skills=[
            SkillFact(skill_name=n, requirement_level=lv) for n, lv in missing if lv == "required"
        ],
        missing_other_skills=[
            SkillFact(skill_name=n, requirement_level=lv) for n, lv in missing if lv != "required"
        ],
        gaps=GapFacts(
            formula_version="skill_gap_v1",
            required_gaps=[n for n, lv in missing if lv == "required"],
            preferred_gaps=[n for n, lv in missing if lv == "preferred"],
            informational_gaps=[n for n, lv in missing if lv == "mentioned"],
        ),
        semantic=SemanticFacts(
            formula_version="semantic_fit_v1",
            fit=0,
            band="none",
            model_identifier="mock-deterministic-v1",
            considered=0,
        ),
        evidence=[
            EvidenceFact(
                evidence_id=_EVIDENCE_ID,
                source_type="resume",
                source_identifier="resume-1",
                excerpt="Built and shipped services with it",
            )
        ],
    )


_SHAPES: dict[str, ExplanationFacts] = {
    "all-matched": _shape([("Python", "required"), ("PostgreSQL", "required")], []),
    "partial-informational": _shape(
        [("Python", "required"), ("Docker", "preferred")], [("AWS", "mentioned")]
    ),
    "partial-required": _shape(
        [("Python", "required"), ("Docker", "required")], [("AWS", "required")]
    ),
    "multi-gap": _shape(
        [("Python", "required"), ("Docker", "mentioned")],
        [("Kubernetes", "required"), ("AWS", "preferred")],
    ),
    "all-non-required-matched": _shape(
        [("Docker", "preferred"), ("Go", "mentioned")], [("AWS", "preferred")]
    ),
}

# Field names, bucket names and version strings a reader cannot
# interpret. The validator catches the version strings as numbers; the
# rest is the prompt's job, and this is what checks the prompt works.
_INTERNAL_TERMS = (
    "skill_match_v1",
    "skill_gap_v1",
    "semantic_fit_v1",
    "required_gaps",
    "preferred_gaps",
    "informational_gaps",
    "needs_confirmation",
    "evidence_ids",
    "has_any_gap",
    "summary_fit",
    "summary_gap",
    "verdictfacts",
    "deterministic facts",
)


# How many times each shape is generated, and how many of those have to
# be accepted.
#
# WHAT THIS IS FOR, AND WHAT IT IS NOT. A verdict shape the instruction
# handles badly fails SYSTEMATICALLY — the shape whose matched skills
# were preferred and mentioned was rejected five times out of five
# before 6.4c, and zero out of five after. Three attempts with two
# required catches that cleanly. It says nothing about the rate a user
# will see: a smoke suite this small cannot, and claiming otherwise from
# it would be the kind of unearned number this product refuses
# everywhere else.
_LIVE_ATTEMPTS = 3
_LIVE_REQUIRED = 2


async def _generate(name: str) -> tuple[list[MatchExplanation], list[str | None]]:
    """`_LIVE_ATTEMPTS` explanations for one shape: the accepted ones,
    and the reasons the rest were refused."""
    settings = _live_settings()
    facts = _SHAPES[name]
    accepted: list[MatchExplanation] = []
    refusals: list[str | None] = []
    for _ in range(_LIVE_ATTEMPTS):
        outcome = await explain(
            facts,
            provider=get_explanation_provider(settings),
            taxonomy=_TAXONOMY,
            settings=settings,
        )
        if outcome.status == "generated" and outcome.explanation is not None:
            accepted.append(outcome.explanation)
        else:
            refusals.append(outcome.reason)
    return accepted, refusals


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(_SHAPES))
async def test_every_verdict_shape_is_explained_without_contradiction(name: str) -> None:
    """THE 6.4c REGRESSION, one shape per run.

    A rejection here is not a validator bug — grounding is never
    loosened to accommodate a smaller model. It means the instruction is
    letting the model assert something the verdict does not support, and
    the reason says which rule caught it.
    """
    facts = _SHAPES[name]
    verdict = facts.verdict
    accepted, refusals = await _generate(name)

    assert len(accepted) >= _LIVE_REQUIRED, (
        f"{name}: only {len(accepted)} of {_LIVE_ATTEMPTS} accepted; refused as {refusals}"
    )

    for explanation in accepted:
        _assert_faithful(name, explanation, verdict)


def _assert_faithful(name: str, explanation: MatchExplanation, verdict: VerdictFacts) -> None:
    """Everything an ACCEPTED explanation still has to be true of."""

    prose = " ".join(
        [
            explanation.summary,
            *explanation.next_steps,
            *(claim.text for claim in explanation.strengths),
            *(claim.text for claim in explanation.gaps),
        ]
    )

    # No internal vocabulary reaches a reader.
    for term in _INTERNAL_TERMS:
        assert term not in prose.lower(), f"{name}: leaked {term!r}"

    # The gap half matches the verdict exactly, in both directions.
    if verdict.has_any_gap:
        assert explanation.summary_gap is not None, f"{name}: a real gap went unmentioned"
    else:
        assert explanation.summary_gap is None
        assert explanation.gaps == []

    # There is always something to do.
    assert explanation.next_steps, f"{name}: no advice at all"

    # ONE STRENGTH PER SKILL IS THE THING THIS REPLACES. With several
    # matched skills, a claim each is a roll-call rather than a reading.
    if len(verdict.matched_names()) > 2:
        assert len(explanation.strengths) < len(verdict.matched_names()), (
            f"{name}: one strength per skill reads as a database dump"
        )


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(_SHAPES))
async def test_no_shape_advises_claiming_an_unearned_skill(name: str) -> None:
    """The product rule, across every verdict rather than only the one
    shape 6.4 happened to test."""
    facts = _SHAPES[name]
    accepted, refusals = await _generate(name)
    assert len(accepted) >= _LIVE_REQUIRED, f"{name}: refused as {refusals}"

    missing = facts.verdict.gap_names()
    for explanation in accepted:
        for step in explanation.next_steps:
            lowered = step.lower()
            if ("resume" in lowered or "profile" in lowered) and any(m in step for m in missing):
                assert any(word in lowered for word in _CONDITIONING), (
                    f"{name}: advises adding a skill the candidate does not have, "
                    f"without conditioning it on earning it first: {step!r}"
                )


@pytest.mark.anyio
async def test_a_complete_match_gets_proof_advice_not_learning_advice() -> None:
    """WHAT A FULL MATCH ACTUALLY NEEDS. There is nothing to learn, so
    the useful next step is evidence somebody can be walked through —
    and "keep building your <skill they already have> skills" is the
    invented weakness this steer exists to stop."""
    accepted, refusals = await _generate("all-matched")
    assert len(accepted) >= _LIVE_REQUIRED, f"refused as {refusals}"

    for explanation in accepted:
        assert explanation.next_steps
        steps = " ".join(explanation.next_steps).lower()
        for gap_word in ("missing", "lacking", "a gap", "not yet shown", "not yet demonstrated"):
            assert gap_word not in steps, f"gap vocabulary on a complete match: {gap_word!r}"
