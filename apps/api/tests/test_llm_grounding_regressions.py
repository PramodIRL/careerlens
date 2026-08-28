"""Grounding failures found by browser-testing the real model (6.4a).

EVERY TEST HERE COMES FROM AN OBSERVED FAILURE, not from imagination.
The mock never tripped any of these, because templated prose does not
write "EC2", does not say "go deeper", and does not suggest adding a
skill to a resume. A real model does all three, which is why they only
appeared once one was wired in.

The load-bearing distinction, which must not blur: these are
FALSE-POSITIVE fixes, not relaxations. A digit inside `EC2` was never a
quantity and the verb "go" was never the language Go — the checks were
matching things they were never meant to match. What they ARE meant to
catch is asserted here too, in the same file, so the two cannot drift.
"""

import json
import re
import uuid
from typing import Any

import pytest

from app.explanation.schema import (
    EvidenceFact,
    ExplanationFacts,
    GapFacts,
    JobFacts,
    LevelFact,
    ScoreFacts,
    SemanticFacts,
    SkillFact,
)
from app.explanation.validate import (
    ExplanationRejected,
    RejectionReason,
    validate_explanation,
)
from app.roadmap.schema import (
    RoadmapFacts,
    RoadmapItemFact,
    RoadmapPlanFacts,
    RoadmapStepFact,
)
from app.roadmap.validate import validate_narrative

_EVIDENCE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_ITEM_ID = "22222222-2222-4222-8222-222222222222"
_STEP_ID = f"{_ITEM_ID}:1"
# The real taxonomy words that collide with ordinary English.
_TAXONOMY = frozenset(
    {"Python", "PostgreSQL", "Docker", "Kubernetes", "AWS", "Go", "React", "Agile", "Express"}
)


def _explanation_facts() -> ExplanationFacts:
    return ExplanationFacts(
        job=JobFacts(saved_job_id=uuid.uuid4(), title="Engineer", company="Acme"),
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
            SkillFact(skill_name="AWS", requirement_level="required", candidate_status=None)
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


def _explanation(summary: str, *, steps: list[str] | None = None) -> str:
    return json.dumps(
        {
            "schema_version": "match_explanation_v1",
            "summary_fit": summary,
            "strengths": [
                {
                    "text": "Your own evidence backs Python.",
                    "evidence_ids": [str(_EVIDENCE_ID)],
                }
            ],
            "gaps": [],
            "next_steps": steps or [],
            "cited_evidence_ids": [str(_EVIDENCE_ID)],
        }
    )


def _check(raw: str) -> None:
    validate_explanation(raw, facts=_explanation_facts(), taxonomy=_TAXONOMY)


def _roadmap_facts() -> RoadmapFacts:
    return RoadmapFacts(
        plan=RoadmapPlanFacts(
            formula_version="roadmap_priority_v1",
            schedule_version="roadmap_schedule_v1",
            selected_job_count=1,
            duration_days=28,
            hours_per_day=1.0,
            total_hours=28.0,
            weeks=4,
        ),
        items=[
            RoadmapItemFact(
                item_id=_ITEM_ID,
                skill_name="AWS",
                state="missing_required",
                start_day=1,
                end_day=7,
                week=1,
                score=105,
                why="AWS is missing and is required by 1 of your 1 selected job.",
                steps=[
                    RoadmapStepFact(
                        step_id=_STEP_ID,
                        phase="build",
                        start_day=1,
                        end_day=7,
                        week=1,
                        estimated_hours=7.0,
                    )
                ],
            )
        ],
    )


def _narrative(task: str, *, focus: str = "Start with what blocks you most") -> str:
    return json.dumps(
        {
            "schema_version": "roadmap_narrative_v1",
            "overview": "A plan for the days ahead.",
            "weeks": [
                {
                    "week": 1,
                    "focus": focus,
                    "checkpoint": "You should be able to explain it.",
                }
            ],
            "items": [
                {
                    "item_id": _ITEM_ID,
                    "task": task,
                    "outcome": "A running service and a written page.",
                    "success_criteria": "Someone else can follow it.",
                }
            ],
            "steps": [
                {
                    "step_id": _STEP_ID,
                    "task": "Build one small thing with it.",
                    "done_when": "It runs.",
                }
            ],
        }
    )


def _check_roadmap(raw: str) -> None:
    validate_narrative(raw, facts=_roadmap_facts(), taxonomy=_TAXONOMY)


def _roadmap_facts_over(duration_days: int, *, weeks_used: list[int]) -> RoadmapFacts:
    """A plan that spans more weeks than it schedules work into."""
    return RoadmapFacts(
        plan=RoadmapPlanFacts(
            formula_version="roadmap_priority_v1",
            schedule_version="roadmap_schedule_v1",
            selected_job_count=1,
            duration_days=duration_days,
            hours_per_day=1.0,
            total_hours=float(duration_days),
            weeks=-(-duration_days // 7),
            work_weeks=sorted(weeks_used),
        ),
        items=[
            RoadmapItemFact(
                item_id=str(uuid.UUID(int=index, version=4)),
                skill_name="AWS",
                state="missing_required",
                start_day=(week - 1) * 7 + 1,
                end_day=week * 7,
                week=week,
                score=105,
                why="AWS is missing and is required by 1 of your 1 selected job.",
                steps=[
                    RoadmapStepFact(
                        step_id=f"{uuid.UUID(int=index, version=4)}:1",
                        phase="build",
                        start_day=(week - 1) * 7 + 1,
                        end_day=week * 7,
                        week=week,
                        estimated_hours=7.0,
                    )
                ],
            )
            for index, week in enumerate(sorted(weeks_used))
        ],
    )


def _narrative_for(facts: RoadmapFacts) -> str:
    return json.dumps(
        {
            "schema_version": "roadmap_narrative_v1",
            "overview": "A plan for the days ahead.",
            "weeks": [
                {
                    "week": week,
                    "focus": "Working through the gap",
                    "checkpoint": "You should be able to explain it.",
                }
                for week in sorted(facts.week_numbers())
            ],
            "items": [
                {
                    "item_id": item.item_id,
                    "task": "Deploy a small service and document it.",
                    "outcome": "A running service and a written page.",
                    "success_criteria": "Someone else can follow it.",
                }
                for item in facts.items
            ],
            "steps": [
                {
                    "step_id": step.step_id,
                    "task": "Build one small thing with it.",
                    "done_when": "It runs.",
                }
                for item in facts.items
                for step in item.steps
            ],
        }
    )


# --- PROBLEM 1: digits inside technology names ---------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "Cover EC2, access roles and network rules.",
        "Store build artefacts in S3 and document the layout.",
        "Write K8s manifests for the service you built.",
        "Wire up OAuth2 against your own test account.",
    ],
    ids=["ec2", "s3", "k8s", "oauth2"],
)
def test_a_technology_name_containing_digits_is_not_a_quantity(phrase: str) -> None:
    """THE BUG THAT REJECTED EVERY CLOUD ROADMAP. `EC2` is a name; the
    "2" in it asserts nothing about the candidate's data."""
    _check_roadmap(_narrative(phrase))


def test_the_same_names_are_fine_in_an_explanation() -> None:
    _check(_explanation("Your profile covers the Python side; EC2 and S3 work is the gap."))


# --- PROBLEM 1: real quantities are still caught -------------------------


@pytest.mark.parametrize(
    ("summary", "offending"),
    [
        ("This covers 5 of your saved jobs.", "5"),
        ("You match 90% of what they ask for.", "90"),
        ("There are 12 requirements listed.", "12"),
    ],
)
def test_a_standalone_number_absent_from_the_facts_is_still_rejected(
    summary: str, offending: str
) -> None:
    """THE RULE DID NOT GO AWAY. Narrowing what counts as a quantity is
    not the same as accepting invented ones."""
    with pytest.raises(ExplanationRejected) as error:
        _check(_explanation(summary))

    assert error.value.reason is RejectionReason.INVENTED_NUMBER
    assert error.value.detail == offending


def test_numbers_that_are_in_the_facts_still_pass() -> None:
    _check(_explanation("Both of the 2 required skills are already covered."))


def test_a_week_span_is_a_deterministic_fact() -> None:
    """ "Days 1 to 7" is derived from the declared duration exactly as the
    label a reader sees is. It was simply never exposed to the
    validator, so the model was rejected for repeating something true."""
    _check_roadmap(_narrative("Build it.", focus="Days 1 to 7: get the basics working"))


# --- PROBLEM 2: skills -----------------------------------------------------


def test_an_unsupported_skill_is_still_rejected_in_an_explanation() -> None:
    with pytest.raises(ExplanationRejected) as error:
        _check(_explanation("You should also pick up Kubernetes for this role."))

    assert error.value.reason is RejectionReason.INVENTED_SKILL
    assert error.value.detail == "Kubernetes"


def test_an_unsupported_skill_is_still_rejected_in_a_roadmap() -> None:
    with pytest.raises(ExplanationRejected) as error:
        _check_roadmap(_narrative("Learn Docker alongside this."))

    assert error.value.reason is RejectionReason.INVENTED_SKILL


@pytest.mark.parametrize(
    "phrase",
    [
        "A good next step is to go deeper on deployment.",
        "Then react to the feedback you get and iterate.",
        "Take an agile approach to the week.",
        "Try to express what you built in your own words.",
    ],
    ids=["go", "react", "agile", "express"],
)
def test_ordinary_english_is_not_an_invented_skill(phrase: str) -> None:
    """`Go`, `React`, `Agile` and `Express` are taxonomy skills AND
    ordinary English words. Lowercasing both sides made the verb "go"
    match the language Go — natural mentoring prose was rejected for
    naming a skill it never named."""
    _check_roadmap(_narrative(phrase))


@pytest.mark.parametrize(
    "phrase",
    ["React to the feedback you get.", "Express what you built in your own words."],
    ids=["react", "express"],
)
def test_a_sentence_starting_with_a_skill_word_is_still_rejected(phrase: str) -> None:
    """THE RESIDUAL, RECORDED RATHER THAN HIDDEN.

    Case-sensitivity fixes the common case — "go deeper", "then react to
    feedback" — but a sentence STARTING with one of these words is
    capitalised and is genuinely ambiguous: "React to the feedback" and
    "React to the feedback" are the same characters whether React is a
    library or a verb. No matcher can separate them without parsing
    English.

    Fail-closed is the right side to err on: the cost is the prose, and
    the deterministic plan is unaffected. The prompt's allowed-skill
    vocabulary is what actually keeps a model away from this, and this
    test exists so the limitation is a known one rather than a surprise.
    """
    with pytest.raises(ExplanationRejected) as error:
        _check_roadmap(_narrative(phrase))

    assert error.value.reason is RejectionReason.INVENTED_SKILL


def test_a_supported_skill_named_properly_still_passes() -> None:
    _check_roadmap(_narrative("Deploy a small service to AWS and document it."))


# --- PROBLEM 3: shape of a good answer -----------------------------------


def test_a_grounded_explanation_may_group_skills_into_one_claim() -> None:
    """The failure mode 6.4a is fixing is one near-identical claim per
    skill. Grouping them is what a person writes, and nothing in the
    validator was ever against it."""
    raw = json.dumps(
        {
            "schema_version": "match_explanation_v1",
            "summary_fit": (
                "Your profile is a strong fit for the Python side of this role, "
                "and the main area to improve is cloud experience."
            ),
            "strengths": [
                {
                    "text": "Your own project evidence backs Python, which they list as required.",
                    "evidence_ids": [str(_EVIDENCE_ID)],
                }
            ],
            "gaps": [
                {
                    "text": "AWS is asked for and your profile does not show it yet.",
                    "evidence_ids": [],
                }
            ],
            "next_steps": [
                "Build a small deployment project that uses AWS and write up how you ran it.",
                "Once that project exists, that experience is what belongs on your profile.",
            ],
            "cited_evidence_ids": [str(_EVIDENCE_ID)],
        }
    )

    _check(raw)


def test_a_roadmap_item_keeps_every_mentoring_field() -> None:
    """LEARN -> BUILD -> PROVE -> SELF-CHECK. A narrative that drops one
    of these is schema-invalid, so an empty half-plan cannot be shown."""
    facts = _roadmap_facts()
    narrative = validate_narrative(
        _narrative("Work through the deployment concepts, then ship a small service."),
        facts=facts,
        taxonomy=_TAXONOMY,
    )

    item = narrative.items[0]
    assert item.task and item.outcome and item.success_criteria
    assert narrative.weeks[0].checkpoint
    assert narrative.overview


@pytest.mark.parametrize(
    "field",
    ["task", "outcome", "success_criteria"],
)
def test_a_blank_mentoring_field_is_rejected(field: str) -> None:
    payload: dict[str, Any] = json.loads(_narrative("Build it."))
    payload["items"][0][field] = ""

    with pytest.raises(ExplanationRejected) as error:
        _check_roadmap(json.dumps(payload))

    assert error.value.reason is RejectionReason.SCHEMA_INVALID


# --- 6.4b: fact SHAPES that a real job produces --------------------------
#
# Every case below was found by running the real model against fact
# bundles the browser produces and the live fixture does not. The live
# fixture is the easy case: three matched skills, all with evidence. A
# real candidate routinely has a confirmed skill with NO evidence — they
# ticked it themselves, or the resume it came from was deleted — and a
# real plan routinely spans more weeks than it schedules work into.


def _facts_without_evidence() -> ExplanationFacts:
    """A confirmed skill that has lost its evidence.

    NOT AN EDGE CASE. app/api/v1/skill_profile.py documents this as the
    intended semantics: "a confirmed skill survives losing every piece
    of evidence (a disconnected GitHub account, a deleted resume)
    because the user asserted it".
    """
    facts = _explanation_facts()
    return facts.model_copy(
        update={
            "matched_skills": [
                SkillFact(
                    skill_name="Python",
                    requirement_level="required",
                    candidate_status="confirmed",
                    evidence_ids=[],
                )
            ],
            "evidence": [],
        }
    )


def test_a_matched_skill_with_evidence_may_be_a_strength() -> None:
    _check(_explanation("Your profile covers the Python side of this role."))


def test_a_matched_skill_without_evidence_may_appear_in_the_summary() -> None:
    """The match is real and worth mentioning; it simply cannot be a
    strength, because a strength has to cite and there is nothing to
    cite. An empty strengths list is the correct answer."""
    raw = json.dumps(
        {
            "schema_version": "match_explanation_v1",
            "summary_fit": "You have confirmed Python yourself, which covers the main requirement.",
            "strengths": [],
            "gaps": [],
            "next_steps": [],
            "cited_evidence_ids": [],
        }
    )

    validate_explanation(raw, facts=_facts_without_evidence(), taxonomy=_TAXONOMY)


def test_an_uncited_strength_is_still_rejected() -> None:
    """THE RULE DID NOT MOVE. The prompt now steers the model away from
    writing one; the validator still refuses it."""
    raw = json.dumps(
        {
            "schema_version": "match_explanation_v1",
            "summary_fit": "You have Python.",
            "strengths": [{"text": "You have Python.", "evidence_ids": []}],
            "gaps": [],
            "next_steps": [],
            "cited_evidence_ids": [],
        }
    )

    with pytest.raises(ExplanationRejected) as error:
        validate_explanation(raw, facts=_facts_without_evidence(), taxonomy=_TAXONOMY)

    assert error.value.reason is RejectionReason.UNGROUNDED_CLAIM


def test_no_matched_skills_allows_a_short_factual_summary() -> None:
    facts = _explanation_facts().model_copy(update={"matched_skills": [], "evidence": []})
    raw = json.dumps(
        {
            "schema_version": "match_explanation_v1",
            "summary_fit": "This posting asks for AWS, and your profile does not cover it yet.",
            "strengths": [],
            "gaps": [],
            "next_steps": [],
            "cited_evidence_ids": [],
        }
    )

    validate_explanation(raw, facts=facts, taxonomy=_TAXONOMY)


def test_the_roadmap_prompt_obeys_its_own_numeric_rule() -> None:
    """An example that breaks a rule teaches the model to break it. The
    prompt's own success-criteria example contained "502", and the model
    copied it verbatim into every narrative."""
    from app.roadmap.prompt import SYSTEM_INSTRUCTION

    instruction = SYSTEM_INSTRUCTION[: SYSTEM_INSTRUCTION.index("RULES,")]
    assert "502" not in SYSTEM_INSTRUCTION
    # No figure anywhere in the worked examples, whatever it might be.
    assert not re.search(r"(?<![0-9A-Za-z])\d+(?![0-9A-Za-z])", instruction)


def test_the_week_allowlist_is_the_weeks_that_hold_work() -> None:
    """A 28-day plan SPANS four weeks; two items land in weeks 1 and 3.
    Telling the model only "weeks: 4" made it theme all four."""
    from app.roadmap.prompt import build_request as build_roadmap_request

    facts = _roadmap_facts_over(28, weeks_used=[1, 3])
    request = build_roadmap_request(facts)

    assert facts.plan.weeks == 4
    assert facts.plan.work_weeks == [1, 3]
    assert request.allowed_weeks == (1, 3)


def test_a_narrative_naming_an_unscheduled_week_is_still_rejected() -> None:
    """The pin makes it unrepresentable at decode time; the validator
    still refuses it independently."""
    facts = _roadmap_facts_over(28, weeks_used=[1, 3])
    payload = json.loads(_narrative_for(facts))
    payload["weeks"].append(
        {"week": 2, "focus": "A week nobody scheduled", "checkpoint": "Nothing to check."}
    )

    with pytest.raises(ExplanationRejected) as error:
        validate_narrative(json.dumps(payload), facts=facts, taxonomy=_TAXONOMY)

    assert error.value.reason is RejectionReason.UNGROUNDED_CLAIM


def test_the_pinned_week_schema_matches_the_scheduled_weeks() -> None:
    from app.explanation.ollama_provider import OllamaRoadmapProvider
    from app.settings import Settings

    facts = _roadmap_facts_over(28, weeks_used=[1, 3])
    provider = OllamaRoadmapProvider(Settings(explanation_provider="ollama"))

    schema = provider.schema_for(
        [item.item_id for item in facts.items], sorted(facts.week_numbers())
    )

    assert schema["$defs"]["NarrativeWeek"]["properties"]["week"] == {"enum": [1, 3]}


def test_neither_prompt_contains_content_its_own_rules_forbid() -> None:
    """THE STRUCTURAL GUARD, added after this bug bit three times.

    The prompts told the model "never name a skill outside the facts"
    and "never write a number that is not in the facts" — while their
    own worked examples said EC2, 502, FastAPI, Python and 75. The model
    copied them verbatim and was rejected for it, every single time,
    deterministically.

    An example that breaks a rule teaches the rule can be broken. This
    asserts the instruction obeys the instruction, so the next example
    somebody adds cannot reintroduce it. Placeholders like <skill A> are
    what the examples use instead.
    """
    import app.seeds.skill_taxonomy as taxonomy_module
    from app.explanation.prompt import SYSTEM_INSTRUCTION as EXPLANATION_PROMPT
    from app.roadmap.prompt import SYSTEM_INSTRUCTION as ROADMAP_PROMPT

    seeds = next(
        value
        for value in vars(taxonomy_module).values()
        if isinstance(value, tuple) and value and hasattr(value[0], "name")
    )
    names = [seed.name for seed in seeds]

    for label, prompt in (
        ("explanation", EXPLANATION_PROMPT),
        ("roadmap", ROADMAP_PROMPT),
    ):
        named = [
            name
            for name in names
            if re.search(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", prompt)
        ]
        assert not named, f"{label} prompt names real skills the model will copy: {named}"

        figures = re.findall(r"(?<![0-9A-Za-z])\d+(?![0-9A-Za-z])", prompt)
        assert not figures, f"{label} prompt contains figures the model will copy: {figures}"


def test_no_evidence_pins_strengths_to_empty() -> None:
    """Prompt-only was not enough at 7B — the model wrote an uncited
    strength 4 times out of 4, because "you matched Python" is right
    there in the facts. With no citable evidence the only valid answer
    is an empty list, so the schema says so."""
    from app.explanation.ollama_provider import OllamaExplanationProvider
    from app.settings import Settings

    provider = OllamaExplanationProvider(Settings(explanation_provider="ollama"))

    from app.explanation.schema import MAX_CLAIMS

    assert provider.schema_for([])["properties"]["strengths"]["maxItems"] == 0
    # With evidence available, strengths keep the schema's own ceiling
    # and nothing more — the pin narrows only the no-evidence case.
    with_evidence = provider.schema_for([str(uuid.uuid4())])
    assert with_evidence["properties"]["strengths"]["maxItems"] == MAX_CLAIMS


def test_array_lengths_are_bounded_so_a_value_cannot_repeat() -> None:
    """An enum constrains each ELEMENT, not the array. A six-item plan
    came back with weeks [1, 2, 3, 2] — every value legal, the array not.
    Bounding the length makes a duplicate overflow it."""
    from app.explanation.ollama_provider import OllamaRoadmapProvider
    from app.settings import Settings

    provider = OllamaRoadmapProvider(Settings(explanation_provider="ollama"))
    schema = provider.schema_for(["a", "b", "c"], [1, 2, 3])

    assert schema["properties"]["items"]["maxItems"] == 3
    assert schema["properties"]["weeks"]["maxItems"] == 3


def test_a_duplicate_week_is_still_rejected_by_the_validator() -> None:
    """The pin stops it being generated; the validator still refuses it
    independently."""
    facts = _roadmap_facts_over(28, weeks_used=[1, 3])
    payload = json.loads(_narrative_for(facts))
    payload["weeks"].append(payload["weeks"][0])

    with pytest.raises(ExplanationRejected) as error:
        validate_narrative(json.dumps(payload), facts=facts, taxonomy=_TAXONOMY)

    assert error.value.reason is RejectionReason.SCHEMA_INVALID


# =====================================================================
# 6.4b — the explanation contradicting the deterministic result
#
# ALL FROM ONE OBSERVED FAILURE. A Frontend Engineer job scored 100%,
# 7/7 required skills matched, no gaps, with REST APIs MATCHED and only
# MENTIONED in the posting. The model wrote "the main area to improve is
# REST APIs, as this is a required skill for the role" and "the posting
# lists REST APIs as a required skill, and your profile does not yet
# show this capability".
#
# Every word was in the allowed skill list and no number was invented,
# so citations, numbers, skills and links all passed it. What was
# missing was a check that a sentence AGREES with the verdict, and a
# fact representation that put the verdict where the model could not
# miss it.
# =====================================================================


def _complete_match_facts() -> ExplanationFacts:
    """The shape that produced the failure: everything matched, nothing
    missing, and one skill the posting only MENTIONS."""
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
                skill_name="React",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            ),
            SkillFact(
                skill_name="Python",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[_EVIDENCE_ID],
            ),
            # The skill the model turned into a missing requirement.
            SkillFact(
                skill_name="Express",
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
                excerpt="Built interfaces in React",
            )
        ],
    )


def _complete_output(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "schema_version": "match_explanation_v1",
        "summary_fit": "You cover everything this posting asks for.",
        "summary_gap": None,
        "strengths": [{"text": "Your evidence backs React.", "evidence_ids": [str(_EVIDENCE_ID)]}],
        "gaps": [],
        "next_steps": [],
        "cited_evidence_ids": [str(_EVIDENCE_ID)],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _reject_complete(raw: str) -> RejectionReason:
    with pytest.raises(ExplanationRejected) as caught:
        validate_explanation(raw, facts=_complete_match_facts(), taxonomy=_TAXONOMY)
    return caught.value.reason


def test_the_verdict_groups_every_skill_by_status() -> None:
    """The fact representation the prompt renders. Derived from the
    skill lists, so it cannot describe one of them differently."""
    verdict = _complete_match_facts().verdict

    assert verdict.required_matched == ["React", "Python"]
    assert verdict.mentioned_matched == ["Express"]
    assert verdict.required_missing == []
    assert verdict.preferred_missing == []
    assert verdict.mentioned_missing == []
    assert verdict.has_any_gap is False


def test_a_verdict_cannot_be_supplied_to_disagree_with_the_skills() -> None:
    """COMPUTED, NOT PASSED. A caller handing in a verdict that
    contradicts the skills is exactly the bug class this exists to stop,
    so the value is recomputed and the supplied one discarded."""
    facts = _complete_match_facts()
    payload = json.loads(facts.model_dump_json())
    payload["verdict"]["has_any_gap"] = True
    payload["verdict"]["required_missing"] = ["Kubernetes"]

    reparsed = ExplanationFacts.model_validate(payload)

    assert reparsed.verdict.has_any_gap is False
    assert reparsed.verdict.required_missing == []


def test_the_observed_sentence_is_now_rejected() -> None:
    """THE EXACT FAILURE, in the exact shape it was written."""
    reason = _reject_complete(
        _complete_output(
            summary_fit=(
                "The main area to improve is backend work, specifically with "
                "Express, as this is a required skill for the role."
            )
        )
    )
    assert reason is RejectionReason.CONTRADICTS_FACTS


def test_the_second_observed_sentence_is_now_rejected() -> None:
    reason = _reject_complete(
        _complete_output(
            summary_gap=(
                "The posting lists Express as a required skill, and your profile "
                "does not yet show this capability."
            )
        )
    )
    assert reason is RejectionReason.CONTRADICTS_FACTS


def test_a_matched_skill_cannot_be_described_as_missing() -> None:
    for sentence in (
        "Your profile is missing React.",
        "You do not have Python yet.",
        "React is a gap for this role.",
        "Your profile does not show Python.",
    ):
        assert _reject_complete(_complete_output(summary_fit=sentence)) is (
            RejectionReason.CONTRADICTS_FACTS
        ), sentence


def test_a_mentioned_skill_cannot_be_called_required() -> None:
    for sentence in (
        "Express is required for this role.",
        "This posting requires Express.",
        "Express is a must-have here.",
        "Express is essential for this posting.",
    ):
        assert _reject_complete(_complete_output(summary_fit=sentence)) is (
            RejectionReason.CONTRADICTS_FACTS
        ), sentence


def test_empty_gaps_cannot_produce_a_gap_claim_anywhere() -> None:
    """Not in the summary, not in "gaps", not in "next_steps"."""
    # No skill named at all, so this is rule 1 alone: a result with
    # nothing missing cannot carry an absence anywhere.
    assert (
        _reject_complete(
            _complete_output(summary_gap="There is still a gap to close before you apply.")
        )
        is RejectionReason.CONTRADICTS_FACTS
    )
    assert (
        _reject_complete(_complete_output(next_steps=["Work on what your profile is lacking."]))
        is RejectionReason.CONTRADICTS_FACTS
    )
    assert (
        _reject_complete(
            _complete_output(
                gaps=[{"text": "Your profile does not cover Express.", "evidence_ids": []}]
            )
        )
        is RejectionReason.CONTRADICTS_FACTS
    )


def test_a_truthful_complete_match_still_passes() -> None:
    """THE FALSE-POSITIVE GUARD, and the reason the rules are scoped to
    a clause rather than to the whole answer. Everything below is TRUE
    of these facts and must survive."""
    for summary in (
        "You cover everything this posting asks for.",
        "There are no gaps here — React and Python are both required and both backed by "
        "your own evidence.",
        "Nothing is missing from your profile for this role.",
        "React and Python are required and you have both; Express is mentioned in passing "
        "and you have that too.",
        "Express is mentioned rather than required, and you already have it.",
    ):
        explanation = validate_explanation(
            _complete_output(summary_fit=summary),
            facts=_complete_match_facts(),
            taxonomy=_TAXONOMY,
        )
        assert explanation.summary_fit == summary


def test_a_real_gap_is_still_describable() -> None:
    """The check must not make an honest gap unwritable. These facts DO
    have one, and the sentence naming it has to pass."""
    raw = _explanation(
        "Your profile is a strong fit for the Python side of this role.",
    )
    payload = json.loads(raw)
    payload["summary_gap"] = "AWS is missing, and this posting lists it as required."
    payload["gaps"] = [{"text": "AWS is required here and you do not have it.", "evidence_ids": []}]

    explanation = validate_explanation(
        json.dumps(payload), facts=_explanation_facts(), taxonomy=_TAXONOMY
    )
    assert explanation.summary_gap is not None


def test_the_deterministic_score_and_categories_are_untouched() -> None:
    """The explanation layer READS the verdict; it never writes one. Two
    validations of the same facts leave both identical."""
    facts = _complete_match_facts()
    before = facts.model_dump_json()

    validate_explanation(_complete_output(), facts=facts, taxonomy=_TAXONOMY)

    assert facts.model_dump_json() == before
    assert facts.score.overall_score == 100
    assert [fact.requirement_level for fact in facts.matched_skills] == [
        "required",
        "required",
        "mentioned",
    ]


def test_the_prompt_states_every_category_and_the_no_gap_verdict() -> None:
    """THE FACT-REPRESENTATION HALF OF THE FIX. Status has to travel in
    the instruction at the same salience as the names, or the model
    infers it from nested data — which is what went wrong."""
    from app.explanation.prompt import build_request

    system = build_request(_complete_match_facts()).system

    assert "RESULT" in system
    assert "THIS RESULT HAS NO GAPS" in system
    # ONE ROW PER SKILL, each carrying the phrase to use for it (6.4c).
    # The bucket form this replaced left the model to remember which
    # heading a name had appeared under, and on a preferred/mentioned
    # verdict it did not.
    assert "React" in system and "you have it" in system
    for name in ("React", "Python", "Express"):
        assert name in system
    assert 'say "the posting requires it"' in system
    assert 'say "the posting mentions it in passing"' in system
    # And the no-gap branch steers next steps at proof rather than
    # learning, which is what a complete match actually needs.
    assert "PROOF, not learning" in system


def test_the_prompt_says_there_are_gaps_when_there_are() -> None:
    from app.explanation.prompt import build_request

    system = build_request(_explanation_facts()).system

    assert "THIS RESULT HAS GAPS" in system
    assert "THIS RESULT HAS NO GAPS" not in system


def test_no_gaps_pins_the_gap_slots_shut_in_the_decoder() -> None:
    """THE STRUCTURAL HALF. Prose was tried and a 7B model wrote the gap
    sentence anyway, because "where you stand, and here is the gap" is
    the shape of every summary it has seen. Removing the slot is the
    fix; asking is not."""
    from app.explanation.ollama_provider import OllamaExplanationProvider
    from app.settings import Settings

    provider = OllamaExplanationProvider(Settings(explanation_provider="ollama"))

    shut = provider.schema_for([str(_EVIDENCE_ID)], allows_gap_claims=False)
    assert shut["properties"]["summary_gap"] == {"type": "null"}
    assert shut["properties"]["gaps"]["maxItems"] == 0

    # And left open when the result really has a gap.
    open_schema = provider.schema_for([str(_EVIDENCE_ID)], allows_gap_claims=True)
    assert open_schema["properties"]["gaps"].get("maxItems") != 0
    assert open_schema["properties"]["summary_gap"] != {"type": "null"}


def test_the_request_carries_the_gap_flag_from_the_facts() -> None:
    from app.explanation.prompt import build_request

    assert build_request(_complete_match_facts()).allows_gap_claims is False
    assert build_request(_explanation_facts()).allows_gap_claims is True


# =====================================================================
# 6.4c — the explanation must be correct for EVERY verdict shape
#
# 6.4b fixed the complete-match case and left a hole next to it. The
# verdicts where every matched skill happens to be REQUIRED came out
# right; the ones where a matched skill is preferred or merely mentioned
# did not, because the instruction's own worked examples had "listed as
# required" baked into the sentence frame and a 7B model reuses a frame
# far more readily than it re-reads a table.
#
# MEASURED, NOT GUESSED. Six verdict shapes x five live generations
# before the fix: 5 rejections, all on the shape whose matched skills
# were preferred and mentioned, and 27 sentences asserting "required"
# about a skill the verdict said was not.
# =====================================================================


def _verdict_facts(*, matched: list[tuple[str, str]], missing: list[tuple[str, str]]) -> Any:
    """A fact bundle from `(skill, requirement_level)` pairs.

    Deliberately parameterised rather than four hand-written fixtures:
    the property under test is "every combination of status and
    category", and hand-writing them invites the one combination nobody
    thought of — which is exactly how 6.4b shipped with this hole.
    """
    matched_facts = [
        SkillFact(
            skill_name=name,
            requirement_level=level,
            candidate_status="confirmed",
            evidence_ids=[_EVIDENCE_ID],
        )
        for name, level in matched
    ]
    missing_required = [
        SkillFact(skill_name=name, requirement_level=level)
        for name, level in missing
        if level == "required"
    ]
    missing_other = [
        SkillFact(skill_name=name, requirement_level=level)
        for name, level in missing
        if level != "required"
    ]
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
        matched_skills=matched_facts,
        missing_required_skills=missing_required,
        missing_other_skills=missing_other,
        gaps=GapFacts(
            formula_version="skill_gap_v1",
            required_gaps=[n for n, level in missing if level == "required"],
            preferred_gaps=[n for n, level in missing if level == "preferred"],
            informational_gaps=[n for n, level in missing if level == "mentioned"],
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
                excerpt="Worked with it",
            )
        ],
    )


def _answer(facts: Any, **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "schema_version": "match_explanation_v1",
        "summary_fit": "You are on solid ground here.",
        "summary_gap": "The one thing to work on is not yet in your profile."
        if facts.verdict.has_any_gap
        else None,
        "strengths": [
            {"text": "Your foundation is already solid.", "evidence_ids": [str(_EVIDENCE_ID)]}
        ],
        "gaps": [],
        "next_steps": ["Build something small and write up how you ran it."],
        "cited_evidence_ids": [str(_EVIDENCE_ID)],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _accepts(facts: Any, **overrides: Any) -> None:
    validate_explanation(_answer(facts, **overrides), facts=facts, taxonomy=_TAXONOMY)


def _refuses(facts: Any, **overrides: Any) -> RejectionReason:
    with pytest.raises(ExplanationRejected) as caught:
        validate_explanation(_answer(facts, **overrides), facts=facts, taxonomy=_TAXONOMY)
    return caught.value.reason


# --- the four result classes, and the fifth that was missing ----------

_ALL_REQUIRED = _verdict_facts(matched=[("Python", "required"), ("Docker", "required")], missing=[])
_MATCHED_PREFERRED = _verdict_facts(
    matched=[("Python", "required"), ("Docker", "preferred")], missing=[]
)
_MATCHED_MENTIONED = _verdict_facts(
    matched=[("Python", "required"), ("Docker", "mentioned")], missing=[]
)
_INFORMATIONAL_GAP = _verdict_facts(
    matched=[("Python", "required"), ("Docker", "preferred")], missing=[("AWS", "mentioned")]
)
_REQUIRED_GAP = _verdict_facts(
    matched=[("Python", "required"), ("Docker", "required")], missing=[("AWS", "required")]
)
_MULTI_GAP = _verdict_facts(
    matched=[("Python", "required"), ("Docker", "mentioned")],
    missing=[("Kubernetes", "required"), ("AWS", "preferred"), ("Go", "mentioned")],
)
_ALL_NON_REQUIRED = _verdict_facts(
    matched=[("Docker", "preferred"), ("Go", "mentioned")], missing=[("AWS", "preferred")]
)

_CLASSES = {
    "all-required-matched": _ALL_REQUIRED,
    "matched-preferred": _MATCHED_PREFERRED,
    "matched-mentioned": _MATCHED_MENTIONED,
    "partial-informational-gap": _INFORMATIONAL_GAP,
    "partial-required-gap": _REQUIRED_GAP,
    "multi-gap": _MULTI_GAP,
    "all-non-required-matched": _ALL_NON_REQUIRED,
}


@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_every_result_class_accepts_a_faithful_explanation(name: str) -> None:
    """The baseline every other test in this block is measured against:
    a correct, category-neutral answer works for ALL of them."""
    _accepts(_CLASSES[name])


@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_a_matched_skill_is_never_callable_missing(name: str) -> None:
    facts = _CLASSES[name]
    matched = sorted(facts.verdict.matched_names())[0]
    assert _refuses(facts, summary_fit=f"Your profile is missing {matched}.") is (
        RejectionReason.CONTRADICTS_FACTS
    )


@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_a_missing_skill_is_never_callable_matched(name: str) -> None:
    """The mirror. A gap described as evidence the candidate holds is a
    strength claim with nothing behind it, so the citation rule catches
    it even where the wording alone would not."""
    facts = _CLASSES[name]
    if not facts.verdict.gap_names():
        pytest.skip("no gap in this class")
    gap = sorted(facts.verdict.gap_names())[0]
    assert (
        _refuses(facts, strengths=[{"text": f"You already have {gap}.", "evidence_ids": []}])
        is RejectionReason.UNGROUNDED_CLAIM
    )


@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_a_non_required_skill_is_never_callable_required(name: str) -> None:
    facts = _CLASSES[name]
    non_required = sorted(facts.verdict.non_required_names())
    if not non_required:
        pytest.skip("every skill in this class is required")
    assert _refuses(facts, summary_fit=f"{non_required[0]} is required for this role.") is (
        RejectionReason.CONTRADICTS_FACTS
    )


@pytest.mark.parametrize("name", ["all-required-matched", "matched-preferred", "matched-mentioned"])
def test_a_no_gap_class_cannot_produce_gap_language(name: str) -> None:
    facts = _CLASSES[name]
    assert facts.verdict.has_any_gap is False
    assert _refuses(facts, summary_fit="There is still a gap to close here.") is (
        RejectionReason.CONTRADICTS_FACTS
    )
    assert _refuses(facts, next_steps=["Work on what your profile is lacking."]) is (
        RejectionReason.CONTRADICTS_FACTS
    )


# --- the distributive rule --------------------------------------------


def test_a_distributive_claim_reaches_every_skill_it_names() -> None:
    """THE 6.4c HOLE. Naming one genuinely required skill used to disarm
    the rule for the whole clause, so this exact sentence — the model's
    single most common output shape — asserted something false about
    Docker 27 times across 30 live runs and was accepted every time."""
    for phrasing in (
        "You already have evidence for Python and Docker, both of which this posting requires.",
        "Python and Docker are both required by this posting.",
        "Python and Docker, all of which this posting requires, are covered.",
        "You have Python and Docker, each of which is required here.",
        "Python and Docker are all required.",
        # NO QUANTIFIER AT ALL, only position: a relative clause after
        # the last name in a coordinated list attaches to the list. The
        # shape that survived the first pass of this fix, twice in five
        # live runs.
        "You are on solid ground with Python and Docker, which the role requires.",
        "You have a solid foundation in Python and Docker, which this posting requires.",
        # A LEADING VERB governing a coordinated object distributes the
        # same way a trailing relative clause does. The last shape the
        # live model produced that the first two passes did not catch.
        "The posting requires Python and Docker.",
    ):
        assert _refuses(_MATCHED_PREFERRED, summary_fit=phrasing) is (
            RejectionReason.CONTRADICTS_FACTS
        ), phrasing


def test_a_scoped_claim_about_a_required_skill_still_passes() -> None:
    """THE FALSE-POSITIVE GUARD. Without the quantifier the requirement
    is scoped to the skill it follows, and saying so must stay legal —
    otherwise the fix trades one broken verdict shape for another."""
    for phrasing in (
        # The SAME relative pronoun, attached to the first name rather
        # than to the list — which is the distinction the position test
        # turns on, and the reason it is a position test.
        "You already have Python, which this posting requires, and Docker as well.",
        "Python is required here, and your Docker work is a bonus on top.",
        "This posting requires Python; Docker is a nice extra you happen to have.",
        # A leading verb reaches only as far as the coordination does:
        # "but you also bring" is not a coordinator, so the claim stops
        # at the first name.
        "The posting requires Python but you also bring Docker.",
    ):
        _accepts(_MATCHED_PREFERRED, summary_fit=phrasing)


def test_a_truthful_negation_still_passes() -> None:
    """A sentence that says a skill is NOT required is the opposite
    claim, and it is the most useful thing an explanation can say about
    a preferred skill."""
    for phrasing in (
        "Docker is preferred rather than required.",
        "Docker is asked for but not required.",
        "Docker is nice-to-have here.",
        "Docker is optional for this posting.",
        "This posting does not require Docker.",
    ):
        _accepts(_MATCHED_PREFERRED, summary_fit=phrasing)


def test_a_contrast_word_no_longer_excuses_an_assertion() -> None:
    """THE OTHER HALF OF THE 6.4c FIX. "mentions", "preferred" and "in
    passing" used to count as contrast on their own, so a sentence
    containing both a contrast word and a requirement claim escaped.
    Live, one did: "the posting mentions AWS as required"."""
    for phrasing in (
        "The posting mentions AWS as required.",
        "AWS is preferred but required for this role.",
        "AWS is mentioned in passing but is required.",
    ):
        assert _refuses(_INFORMATIONAL_GAP, summary_gap=phrasing) is (
            RejectionReason.CONTRADICTS_FACTS
        ), phrasing


def test_gaps_of_different_kinds_may_not_be_merged() -> None:
    """Measured live: the model gets each gap right on its own line and
    then merges them in the summary. "The posting requires Kubernetes
    and AWS" is true of the required one and false of the preferred
    one, and the coordination is what makes it reach both."""
    assert (
        _refuses(
            _MULTI_GAP,
            summary_gap=(
                "The posting requires Kubernetes and AWS, which your profile does not show."
            ),
        )
        is RejectionReason.CONTRADICTS_FACTS
    )
    # Given a sentence each, the same two gaps are perfectly writable.
    _accepts(_MULTI_GAP, summary_gap="The posting requires Kubernetes, and it prefers AWS.")
    # AND SO IS ONE SENTENCE WITH A VERB EACH. This is the shape the
    # real model settled on, and it is true: "both" here distributes the
    # ABSENCE, while "requires" and "prefers" keep their own objects.
    # A quantifier that reaches every claim in its sentence would reject
    # it, which is why the reach is measured from the marker.
    _accepts(
        _MULTI_GAP,
        summary_gap=(
            "The posting requires Kubernetes and prefers AWS, "
            "both of which are not shown in your profile yet."
        ),
    )


def test_the_clause_scope_is_a_real_limit_and_is_stated_as_one() -> None:
    """WHAT THIS CHECK CANNOT DO, pinned rather than implied.

    The rules are scoped to a clause, so a claim that lands in a
    DIFFERENT clause from the skill it is about cannot be attributed to
    it. "You have Python and Docker; each is required here" splits at
    the semicolon and the second half names no skill at all.

    That splitting is not a defect to remove — it is what stops "you
    have Python; you lack Kubernetes" being read as a claim about
    Python. Resolving a pronoun across a clause boundary is not
    something a lexical check does, and pretending otherwise in a
    docstring is how a limit becomes a surprise.
    """
    _accepts(_MATCHED_PREFERRED, summary_fit="You have Python and Docker; each is required here.")


# --- interpretation, and what is still not allowed --------------------


def test_a_grouped_interpretation_is_accepted() -> None:
    """SYNTHESIS IS THE POINT. "Your frontend foundation is strong" is a
    reading of skills that were supplied, and it is what makes the panel
    worth reading instead of a second copy of the score table."""
    _accepts(
        _MATCHED_PREFERRED,
        summary_fit="Your backend foundation is already solid, particularly around Python.",
        strengths=[
            {
                "text": "The day-to-day engineering side of this role is covered by work "
                "you have already done.",
                "evidence_ids": [str(_EVIDENCE_ID)],
            }
        ],
    )


def test_interpretation_may_not_smuggle_in_a_new_fact() -> None:
    """The line between reading the facts and adding to them. A quantity
    nobody supplied is still an invented number, grouped prose or not."""
    assert (
        _refuses(
            _MATCHED_PREFERRED,
            summary_fit="Your backend foundation is strong, with 5 years behind it.",
        )
        is RejectionReason.INVENTED_NUMBER
    )


def test_internal_terminology_is_rejected_as_an_invented_skill_or_number() -> None:
    """Field names and version strings must never reach a reader. The
    version strings are caught as numbers; the prompt forbids the rest,
    and the live smoke asserts the prompt is working."""
    assert _refuses(_MATCHED_PREFERRED, summary_fit="skill_match_v1 scored this 91.") is (
        RejectionReason.INVENTED_NUMBER
    )


def test_the_deterministic_verdict_survives_being_explained() -> None:
    """Read-only, for every class. The explanation layer never writes a
    verdict, so validating one cannot move a score or a category."""
    for facts in _CLASSES.values():
        before = facts.model_dump_json()
        _accepts(facts)
        assert facts.model_dump_json() == before


# --- the decoder pins --------------------------------------------------


def test_at_least_one_next_step_is_pinned_in_the_decoder() -> None:
    """`next_steps` defaults to empty, so a grammar happily emits one —
    and on a complete match a live run did, producing an explanation
    with no advice in it at all."""
    from app.explanation.ollama_provider import OllamaExplanationProvider
    from app.settings import Settings

    provider = OllamaExplanationProvider(Settings(explanation_provider="ollama"))
    for gaps_allowed in (True, False):
        schema = provider.schema_for([str(_EVIDENCE_ID)], allows_gap_claims=gaps_allowed)
        assert schema["properties"]["next_steps"]["minItems"] == 1
        # AND PRESENT AT ALL. `next_steps` has a Pydantic default, so it
        # is not in the base schema's `required` list — and a grammar
        # that lets the key be omitted never reaches `minItems`. Two live
        # runs out of five omitted it entirely and Pydantic filled the
        # empty default back in, so the pin looked enforced while nothing
        # enforced it. Zero out of six after.
        assert "next_steps" in schema["required"]


def test_the_pinned_contract_is_unchanged_for_everything_else() -> None:
    """6.4c adds one decoder pin and touches nothing else about the
    shape a provider is constrained to."""
    from app.explanation.ollama_provider import OllamaExplanationProvider
    from app.settings import Settings

    provider = OllamaExplanationProvider(Settings(explanation_provider="ollama"))
    shut = provider.schema_for([str(_EVIDENCE_ID)], allows_gap_claims=False)

    assert shut["properties"]["summary_gap"] == {"type": "null"}
    assert shut["properties"]["gaps"]["maxItems"] == 0
    assert shut["$defs"]["ExplanationClaim"]["properties"]["evidence_ids"]["items"] == {
        "enum": [str(_EVIDENCE_ID)]
    }
