"""The roadmap's deterministic mock provider (Prompt 6.3).

REUSES 6.1's PROTOCOL, NOT ITS CONTENT. `ExplanationProvider` is a
structural type — one property, one async method taking a request and
returning text — so it describes any provider of any LLM-facing path in
this product. That is worth sharing. What is NOT shared is what goes in
and what comes out: the facts and the narrative schema here are this
feature's own, because forcing one contract to serve both would make
each harder to read and couple every roadmap change to 6.1's shape.

NO REAL PROVIDER AND NO CREDENTIAL, exactly as in 6.1. There is one
setting, `explanation_provider`, and "mock" remains the only known
value.
"""

import json

from app.explanation.prompt import ExplanationRequest
from app.explanation.provider import MOCK_PROVIDER_NAME, ExplanationProvider
from app.roadmap.schema import SCHEMA_VERSION, RoadmapFacts
from app.settings import Settings, get_settings

# What the mock writes for each kind of gap. Templated and flat: a fake
# should be obviously a fake. Its real job is exercising the whole path
# — every sentence it emits is one the validator has to accept, so a
# grounding rule that is too strict fails here rather than in front of a
# user.
_TASK_BY_STATE = {
    "missing_required": (
        "Build and ship a small working piece of software that uses {skill}, "
        "then write up what you built and how you ran it."
    ),
    "missing_preferred": (
        "Add {skill} to something you have already built, and record what "
        "changed and why you chose it."
    ),
    # WEAK IS NOT MISSING. The candidate may well have this already —
    # what is missing is reviewed evidence, so the task asks for
    # evidence, not for learning it again.
    "weak_evidence": (
        "Document an existing project that uses {skill} so the evidence is "
        "reviewable, and confirm it in your skill profile."
    ),
}

# WHAT THEY END UP HOLDING — an artefact, not a feeling of progress.
_OUTCOME_BY_STATE = {
    "missing_required": (
        "A running project that uses {skill}, and a written page covering how to run it."
    ),
    "missing_preferred": (
        "An existing project of yours that now uses {skill}, with a note on what it changed."
    ),
    "weak_evidence": (
        "A written description of where you have already used {skill}, "
        "attached to your profile as reviewed evidence."
    ),
}

# WHAT THEY SHOULD BE ABLE TO DO — checkable alone, and the kind of
# thing an interviewer asks.
_CRITERIA_BY_STATE = {
    "missing_required": (
        "Someone else can follow your write-up and run it without asking you a question."
    ),
    "missing_preferred": (
        "You can explain out loud what {skill} changed in the project and what it cost."
    ),
    "weak_evidence": (
        "{skill} shows as confirmed on your profile, backed by evidence you can point at."
    ),
}

_WEEK_FOCUS = "Working on {skills}"

# Phrased as a capability rather than a task, because "did you finish
# it" and "can you now do it" are different questions and only the
# second is worth checking.
_WEEK_CHECKPOINT = (
    "By the end of this week you should be able to explain what you built with "
    "{skills} and why you built it that way."
)


class MockRoadmapProvider:
    """A deterministic fake, for tests and local development.

    NOT RANDOM AND NOT A MODEL. The output is a pure function of the
    facts, so identical facts produce a byte-identical string in a
    different process and on a different machine — which is what lets a
    test assert on a roadmap at all.

    IT READS THE FACTS, NOT THE INSTRUCTION. The system prompt is
    ignored, which is correct for a fake and also means these tests
    cannot start depending on prompt wording.
    """

    @property
    def name(self) -> str:
        return MOCK_PROVIDER_NAME

    async def complete(self, request: ExplanationRequest) -> str:
        return self.complete_sync(request)

    def complete_sync(self, request: ExplanationRequest) -> str:
        payload = json.loads(request.data_json)["untrusted_data"]
        facts = RoadmapFacts.model_validate(payload)
        return json.dumps(_render(facts), sort_keys=True)


def _render(facts: RoadmapFacts) -> dict[str, object]:
    plan = facts.plan
    overview = (
        f"Over {plan.duration_days} days at {plan.hours_per_day} hours a day, "
        f"you have about {plan.total_hours} hours. This plan spends them on "
        f"{len(facts.items)} things, ordered by what your "
        f"{plan.selected_job_count} selected jobs ask for most."
    )

    by_week: dict[int, list[str]] = {}
    for item in facts.items:
        by_week.setdefault(item.week, []).append(item.skill_name)

    return {
        "schema_version": SCHEMA_VERSION,
        "overview": overview,
        # ONE ENTRY PER WEEK THAT HOLDS WORK, matching what the schedule
        # decided. The mock could not invent a week if it tried.
        "weeks": [
            {
                "week": week,
                "focus": _WEEK_FOCUS.format(skills=_join(skills)),
                "checkpoint": _WEEK_CHECKPOINT.format(skills=_join(skills)),
            }
            for week, skills in sorted(by_week.items())
        ],
        # ONE ENTRY PER SUPPLIED ITEM, in the supplied order. There is
        # nowhere in the response to put an item that has no id here.
        "items": [
            {
                "item_id": item.item_id,
                "task": _TASK_BY_STATE[item.state].format(skill=item.skill_name),
                "outcome": _OUTCOME_BY_STATE[item.state].format(skill=item.skill_name),
                "success_criteria": _CRITERIA_BY_STATE[item.state].format(skill=item.skill_name),
            }
            for item in facts.items
        ],
    }


def _join(names: list[str]) -> str:
    """ "A", "A and B", "A, B and C" — the way a person writes a list."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def get_roadmap_provider(settings: Settings | None = None) -> ExplanationProvider:
    """Build the configured provider.

    Raises on an unknown name rather than falling back to the mock, for
    the same reason app/embeddings/provider.py does: a typo that
    silently produced fake output would be discovered much later, as
    text somebody had already trusted.
    """
    settings = settings or get_settings()
    if settings.explanation_provider == MOCK_PROVIDER_NAME:
        return MockRoadmapProvider()
    raise ValueError(
        f"unknown explanation provider {settings.explanation_provider!r} "
        f"(known providers: {MOCK_PROVIDER_NAME})"
    )
