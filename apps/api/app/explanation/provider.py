"""The provider-agnostic explanation interface, and a deterministic mock.

SAME SHAPE AS app/embeddings/provider.py, for the same reason: nothing
in the application constructs a provider directly, it asks
`get_explanation_provider()`, so a real one can be added later without a
single call site changing.

ASYNC on purpose. The mock is pure string building, but every realistic
provider is an HTTP call, and a synchronous signature here would have to
be broken later.

ONE IMPLEMENTATION IN 6.1. `MockExplanationProvider` is deterministic by
construction and calls nothing. THERE IS NO HOSTED PROVIDER AND NO API
KEY NAMED IN THIS MODULE. Adding one is a later slice, and it brings its
own decisions about timeouts, retries, rate limits and where its
credential lives — none of which are answered here.
"""

import json
from typing import Protocol

from app.explanation.prompt import ExplanationRequest
from app.explanation.schema import (
    MAX_CLAIM_CITATIONS,
    SCHEMA_VERSION,
    ExplanationFacts,
    SkillFact,
)
from app.settings import Settings, get_settings

MOCK_PROVIDER_NAME = "mock"

# Declared here rather than imported from the ollama module, so the
# factory can name it in an error without importing the provider it is
# refusing to build.
OLLAMA_PROVIDER_NAME = "ollama"


# --------------------------------------------------------------------
# Provider failures (Prompt 6.2)
#
# THE SPLIT IS TRANSIENT vs TERMINAL, and it is the same one
# app/worker.py draws for resume extraction and app/github/base.py draws
# for the GitHub client: a timeout or an unreachable service may well
# succeed on a second attempt, and everything else will fail the same
# way twice. Only the two below are retried.
#
# A provider that raises anything NOT descended from
# `ExplanationProviderError` is treated as terminal. That is deliberate:
# an unexpected exception type is a bug or a misconfiguration, and
# retrying a bug just does it twice.
# --------------------------------------------------------------------


class ExplanationProviderError(Exception):
    """Base for every failure a provider reports."""


class ExplanationTimeout(ExplanationProviderError):
    """The provider did not answer within the configured budget.

    Raised by the adapter's own `asyncio.wait_for`, so it applies to
    every provider including one that never learned to time itself out.
    """


class ExplanationUnavailable(ExplanationProviderError):
    """The provider was unreachable, returned a server error, or sent
    something unusable at the transport level.

    NOT for a response that arrived and failed validation — that is
    app/explanation/validate.py's business and is never retried.
    """


class ExplanationProvider(Protocol):
    """What the application depends on instead of a concrete provider."""

    @property
    def name(self) -> str:
        """Returned in the response so a reader knows what produced the
        prose. "mock" says it plainly."""
        ...

    async def complete(self, request: ExplanationRequest) -> str:
        """Raw text. Deliberately NOT a parsed object: parsing and
        validating are app/explanation/validate.py's job, and a provider
        that returned a `MatchExplanation` would be asserting its own
        output is valid."""
        ...


def _claim(text: str, facts: list[SkillFact]) -> dict[str, object]:
    return {"text": text, "evidence_ids": _ids(facts)}


def _ids(facts: list[SkillFact]) -> list[str]:
    return [str(evidence_id) for fact in facts for evidence_id in fact.evidence_ids][:5]


def _join(facts: list[SkillFact]) -> str:
    return ", ".join(fact.skill_name for fact in facts)


class MockExplanationProvider:
    """A deterministic fake, for tests and local development.

    NOT RANDOM AND NOT A MODEL. The output is a pure function of the
    facts, so the same facts produce a byte-identical string in a
    different process and on a different machine — which is what lets a
    test assert on an explanation at all.

    It is a fake, not a writer: the prose is templated and says nothing a
    person would call insightful. That is the honest thing for a mock to
    be. What it IS good for is exercising the real path end to end —
    every sentence it emits is one the validator has to accept, so a
    grounding rule that is too strict fails here rather than in front of
    a user.

    IT READS THE FACTS, NOT THE INSTRUCTION. The system prompt is
    ignored, which is the correct behaviour for a fake and also means
    these tests cannot accidentally start depending on prompt wording.
    """

    @property
    def name(self) -> str:
        return MOCK_PROVIDER_NAME

    async def complete(self, request: ExplanationRequest) -> str:
        return self.complete_sync(request)

    def complete_sync(self, request: ExplanationRequest) -> str:
        payload = json.loads(request.data_json)["untrusted_data"]
        facts = ExplanationFacts.model_validate(payload)
        return json.dumps(_render(facts), sort_keys=True)


def _level_phrase(facts: list[SkillFact]) -> str:
    """ "required skill" or "required skills", by count. Grammar, not a
    judgement — a sentence that says "1 required skills" reads as
    machine output, which is the whole thing this prose is fixing."""
    required = [fact for fact in facts if fact.requirement_level == "required"]
    return "required skill" if len(required) == 1 else "required skills"


def _render(facts: ExplanationFacts) -> dict[str, object]:
    score = facts.score
    if score.has_requirements:
        summary_fit = (
            f"{facts.job.company} — {facts.job.title}: {score.formula_version} scored this "
            f"match {score.overall_score}, with {score.required_matched} of "
            f"{score.required_total} required skills matched."
        )
    else:
        summary_fit = (
            f"{facts.job.company} — {facts.job.title}: no skill requirements were "
            f"recognised in this posting, so {score.formula_version} has nothing to score."
        )

    # THE GAP HALF IS None WHEN THERE IS NO GAP. The mock has to obey
    # the same rule the real provider is pinned to, or the one path that
    # runs on every test would never exercise it.
    summary_gap = (
        "The skills this posting asks for that your profile does not show are listed below."
        if facts.verdict.has_any_gap
        else None
    )

    # ONE STRENGTH NAMING THE SKILLS TOGETHER, not one repetitive claim
    # per skill. The earlier shape emitted "Your stored evidence covers
    # X." once for every match, which read as a database dump rather
    # than as somebody telling you where you stand — and the excerpts
    # behind it belong in a disclosure the reader opens, not in the
    # first paragraph they see.
    #
    # Only skills WITH evidence are named: a strength has to cite, and
    # the validator enforces it. Citations are capped by
    # MAX_CLAIM_CITATIONS, so a job with many matches names them all and
    # cites the first few — every name is still in the facts, which is
    # what `_check_skills` verifies.
    cited = [fact for fact in facts.matched_skills if fact.evidence_ids]
    strengths: list[dict[str, object]] = []
    if cited:
        strengths.append(
            _claim(
                f"You already have evidence for {_join(cited)}"
                + (
                    f", including the {_level_phrase(cited)} this posting asks for."
                    if any(fact.requirement_level == "required" for fact in cited)
                    else "."
                ),
                cited[:MAX_CLAIM_CITATIONS],
            )
        )

    gaps: list[dict[str, object]] = []
    if facts.missing_required_skills:
        gaps.append(
            _claim(
                f"This posting requires {_join(facts.missing_required_skills)}, "
                f"which your profile does not currently show.",
                [],
            )
        )
    if facts.missing_other_skills:
        gaps.append(
            _claim(
                f"Also asked for, but not required: {_join(facts.missing_other_skills)}.",
                [],
            )
        )
    if facts.gaps.needs_confirmation:
        gaps.append(
            _claim(
                "Evidence exists but is unreviewed for: "
                f"{', '.join(facts.gaps.needs_confirmation)}.",
                [],
            )
        )

    next_steps = []
    if facts.gaps.needs_confirmation:
        next_steps.append("Review the unconfirmed skills on your profile.")
    if facts.missing_required_skills:
        next_steps.append(f"Look at what {_join(facts.missing_required_skills)} would involve.")
    if not next_steps:
        next_steps.append("Keep your profile evidence up to date.")

    # NO GAP MEANS NO GAP CLAIMS, mirroring what the real provider's
    # schema is pinned to when `has_any_gap` is false. The only entry
    # this can drop is the unreviewed-evidence note, which is not a
    # missing skill and belongs in `next_steps` — where it already is.
    if not facts.verdict.has_any_gap:
        gaps = []

    cited_ids = sorted({evidence_id for fact in cited[:5] for evidence_id in _ids([fact])})
    return {
        "schema_version": SCHEMA_VERSION,
        "summary_fit": summary_fit,
        "summary_gap": summary_gap,
        "strengths": strengths,
        "gaps": gaps[:5],
        "next_steps": next_steps[:5],
        "cited_evidence_ids": cited_ids,
    }


def get_explanation_provider(settings: Settings | None = None) -> ExplanationProvider:
    """Build the configured provider.

    Raises on an unknown name rather than falling back to the mock: a
    typo that silently served templated placeholder prose as though a
    model had written it is exactly the failure this product cannot
    afford.
    """
    settings = settings or get_settings()
    if settings.explanation_provider == MOCK_PROVIDER_NAME:
        return MockExplanationProvider()
    if settings.explanation_provider == OLLAMA_PROVIDER_NAME:
        # Imported HERE, not at module scope. `httpx` is cheap, but the
        # rule is the one app/embeddings/provider.py already follows for
        # `fastembed`: nothing that only uses the mock — the entire unit
        # suite included — should load a module it will never call.
        from app.explanation.ollama_provider import OllamaExplanationProvider

        return OllamaExplanationProvider(settings)
    raise ValueError(
        f"unknown explanation provider {settings.explanation_provider!r} "
        f"(known providers: {MOCK_PROVIDER_NAME}, {OLLAMA_PROVIDER_NAME})"
    )
