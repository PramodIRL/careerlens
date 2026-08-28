"""Provider selection and the local-inference client (Prompt 6.4).

FULLY OFFLINE. Every test here runs with no daemon, no model and no
network — the transport is exercised against `httpx.MockTransport`, the
same technique tests/test_github_client.py uses to test timeout and
error handling without reaching GitHub.

The load-bearing tests, which should not be softened:

  * the default is the MOCK, so nothing in the suite can reach a model
  * an unknown provider name raises rather than falling back
  * `httpx` is never retried by the provider — runtime.py is the single
    retry authority, and a provider that retried on its own would
    silently multiply both the wall clock and the attempt count
  * a transport failure maps onto Prompt 6.2's existing taxonomy, with
    no new reason values
  * the schema sent to the daemon is our own Pydantic schema
"""

import json
import uuid
from typing import Any

import httpx
import pytest

from app.explanation.ollama_provider import (
    OLLAMA_PROVIDER_NAME,
    OllamaExplanationProvider,
    OllamaRoadmapProvider,
    describe_unavailable,
)
from app.explanation.prompt import ExplanationRequest
from app.explanation.prompt import build_request as build_explanation_request
from app.explanation.provider import (
    MOCK_PROVIDER_NAME,
    ExplanationTimeout,
    ExplanationUnavailable,
    MockExplanationProvider,
    get_explanation_provider,
)
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
)
from app.explanation.validate import (
    ExplanationRejected,
    RejectionReason,
    validate_explanation,
)
from app.roadmap.prompt import build_request as build_roadmap_request
from app.roadmap.provider import MockRoadmapProvider, get_roadmap_provider
from app.roadmap.schema import (
    RoadmapFacts,
    RoadmapItemFact,
    RoadmapNarrative,
    RoadmapPlanFacts,
)
from app.settings import Settings

_REQUEST = ExplanationRequest(system="instruction", data_json="{}", delimited="{}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(**overrides: Any) -> Settings:
    return Settings(explanation_provider=OLLAMA_PROVIDER_NAME, **overrides)


def _explanation_facts() -> ExplanationFacts:
    """A minimal but REAL fact bundle, built through the same models the
    application uses — so the ids in the schema come from where they
    come from in production."""
    evidence_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    return ExplanationFacts(
        job=JobFacts(saved_job_id=uuid.uuid4(), title="Engineer", company="Acme"),
        score=ScoreFacts(
            formula_version="skill_match_v1",
            overall_score=75,
            earned_weight=3,
            obtainable_weight=4,
            has_requirements=True,
            required_matched=1,
            required_total=1,
            by_level={"required": LevelFact(matched=1, total=1)},
            weights={"required": 3},
        ),
        matched_skills=[
            SkillFact(
                skill_name="Python",
                requirement_level="required",
                candidate_status="confirmed",
                evidence_ids=[evidence_id],
            )
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
                evidence_id=evidence_id,
                source_type="resume",
                source_identifier="resume-1",
                excerpt="Built backend services in Python",
            )
        ],
    )


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
                item_id=str(uuid.uuid4()),
                skill_name=name,
                state="missing_required",
                start_day=start,
                end_day=start + 6,
                week=week,
                score=105,
                why=f"{name} is missing and is required by 1 of your 1 selected job.",
            )
            for week, (name, start) in enumerate((("AWS", 1), ("Docker", 8)), start=1)
        ],
    )


# --- provider selection -------------------------------------------------


def test_the_code_default_is_the_mock() -> None:
    """THE GUARANTEE THE WHOLE TEST SUITE RESTS ON. If this ever flips,
    every test in the repository can reach a model.

    Asserts the CLASS default rather than a constructed `Settings()`,
    because the latter reads the repo-root .env — a developer trying the
    real model locally would flip it and this test would then be
    asserting their environment instead of the code. Pinning the suite
    to the mock regardless is `_force_mock_llm_provider` in conftest.
    """
    assert Settings.model_fields["explanation_provider"].default == MOCK_PROVIDER_NAME

    settings = Settings(explanation_provider=MOCK_PROVIDER_NAME)
    assert isinstance(get_explanation_provider(settings), MockExplanationProvider)
    assert isinstance(get_roadmap_provider(settings), MockRoadmapProvider)


def test_the_suite_is_pinned_to_the_mock_whatever_the_environment_says() -> None:
    """The conftest fixture, asserted. Without it, setting
    EXPLANATION_PROVIDER=ollama in .env to try the real model would
    point every test in the repository at a local daemon."""
    from app.settings import get_settings

    assert get_settings().explanation_provider == MOCK_PROVIDER_NAME


def test_ollama_is_selected_only_when_configured() -> None:
    settings = _settings()

    assert isinstance(get_explanation_provider(settings), OllamaExplanationProvider)
    assert isinstance(get_roadmap_provider(settings), OllamaRoadmapProvider)


@pytest.mark.parametrize("name", ["openai", "anthropic", "Mock", "", "ollama2"])
def test_an_unknown_provider_raises_rather_than_falling_back(name: str) -> None:
    """Never a silent fallback. Serving templated placeholder prose
    while an operator believes a real model is running is the one
    failure this product cannot afford."""
    settings = Settings(explanation_provider=name)

    for factory in (get_explanation_provider, get_roadmap_provider):
        with pytest.raises(ValueError) as error:
            factory(settings)
        # The message names what IS available, so a typo is one read away
        # from being fixed.
        assert MOCK_PROVIDER_NAME in str(error.value)
        assert OLLAMA_PROVIDER_NAME in str(error.value)


def test_the_unavailable_message_says_how_to_fix_it() -> None:
    message = describe_unavailable(_settings())

    assert "ollama serve" in message
    assert "ollama pull" in message
    # And that the product works without a model at all.
    assert "EXPLANATION_PROVIDER=mock" in message


def test_no_credential_setting_exists() -> None:
    """There is no API key in this product, and no field that could
    hold one — which is what makes "never log a credential" trivially
    true rather than a rule somebody has to remember."""
    fields = set(Settings.model_fields)
    secret_shaped = {
        name for name in fields if "api_key" in name or "secret" in name or name.endswith("_token")
    }

    # The auth secrets that legitimately exist are named explicitly, so
    # a new one cannot be added to this product without this assertion
    # noticing.
    assert secret_shaped == {"jwt_secret"}
    assert "explanation_api_key" not in fields
    assert "ollama_api_key" not in fields


# --- the request the daemon receives ------------------------------------


def _transport(
    captured: list[dict[str, Any]], *, content: str = "{}", status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json={"message": {"content": content}})

    return httpx.MockTransport(handler)


async def _complete(
    provider: OllamaExplanationProvider | OllamaRoadmapProvider,
    transport: httpx.MockTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Run one generation against a fake transport.

    Patches the client constructor rather than the module's logic, so
    what is under test is the real request-building and error-mapping
    path.
    """
    original = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return await provider.complete(_REQUEST)


@pytest.mark.anyio
async def test_the_request_carries_our_own_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constrained decoding against the SAME Pydantic model the
    validator will check the answer against — not a hand-written copy
    that could drift from it."""
    captured: list[dict[str, Any]] = []
    provider = OllamaExplanationProvider(_settings())

    await _complete(provider, _transport(captured), monkeypatch)

    provider_schema = OllamaExplanationProvider(_settings()).schema_for(_REQUEST.allowed_ids)
    assert captured[0]["format"] == provider_schema
    # Still derived from our own Pydantic model, not a hand-written copy.
    assert captured[0]["format"]["$defs"].keys() == (
        MatchExplanation.model_json_schema()["$defs"].keys()
    )
    assert captured[0]["model"] == "qwen2.5:7b-instruct"
    assert captured[0]["stream"] is False


@pytest.mark.anyio
async def test_the_roadmap_provider_sends_the_roadmap_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    await _complete(OllamaRoadmapProvider(_settings()), _transport(captured), monkeypatch)

    expected = OllamaRoadmapProvider(_settings()).schema_for(_REQUEST.allowed_ids)
    assert captured[0]["format"] == expected
    assert captured[0]["format"]["$defs"].keys() == (
        RoadmapNarrative.model_json_schema()["$defs"].keys()
    )


@pytest.mark.anyio
async def test_the_instruction_and_the_untrusted_data_stay_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt 6.2's boundary, preserved on the wire: the facts are their
    own message, not concatenated into the instruction."""
    captured: list[dict[str, Any]] = []

    await _complete(OllamaExplanationProvider(_settings()), _transport(captured), monkeypatch)

    roles = [message["role"] for message in captured[0]["messages"]]
    assert roles == ["system", "user"]
    assert captured[0]["messages"][0]["content"] == "instruction"


@pytest.mark.anyio
async def test_raw_text_is_returned_unparsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validators own parsing, the size ceiling and every grounding
    rule. A provider that pre-parsed would move part of that decision
    somewhere no test looks."""
    body = '{"schema_version": "match_explanation_v1"}'

    result = await _complete(
        OllamaExplanationProvider(_settings()), _transport([], content=body), monkeypatch
    )

    assert result == body


@pytest.mark.anyio
async def test_the_model_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size/latency trade-off is a deployment decision, not a code
    change: a slow demo machine moves to 3b with one variable."""
    captured: list[dict[str, Any]] = []
    provider = OllamaExplanationProvider(_settings(explanation_model="qwen2.5:3b-instruct"))

    await _complete(provider, _transport(captured), monkeypatch)

    assert captured[0]["model"] == "qwen2.5:3b-instruct"


# --- failure mapping ----------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx.ConnectTimeout("slow"), ExplanationTimeout),
        (httpx.ReadTimeout("slow"), ExplanationTimeout),
        (httpx.ConnectError("daemon down"), ExplanationUnavailable),
        (httpx.RemoteProtocolError("truncated"), ExplanationUnavailable),
    ],
)
async def test_transport_failures_map_onto_the_existing_taxonomy(
    raised: Exception, expected: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No new reason values. Prompt 6.2's transient/terminal split was
    built for exactly this shape and needs no widening."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    provider = OllamaExplanationProvider(_settings())
    with pytest.raises(expected):
        await _complete(provider, httpx.MockTransport(handler), monkeypatch)


@pytest.mark.anyio
async def test_a_server_error_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaExplanationProvider(_settings())

    with pytest.raises(ExplanationUnavailable):
        await _complete(provider, _transport([], status=503), monkeypatch)


@pytest.mark.anyio
async def test_a_missing_model_is_terminal_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 means the daemon is up but the model was never pulled.
    Retrying cannot download a model, and the message has to say that or
    somebody goes looking at the network."""
    provider = OllamaExplanationProvider(_settings())

    with pytest.raises(RuntimeError) as error:
        await _complete(provider, _transport([], status=404), monkeypatch)

    assert "ollama pull" in str(error.value)
    assert not isinstance(error.value, ExplanationUnavailable | ExplanationTimeout)


@pytest.mark.anyio
async def test_an_unexpected_body_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 that is not an Ollama chat response — a proxy page, or
    another service on that port. The shape will not change on a second
    attempt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    provider = OllamaExplanationProvider(_settings())
    with pytest.raises(RuntimeError):
        await _complete(provider, httpx.MockTransport(handler), monkeypatch)


@pytest.mark.anyio
async def test_the_provider_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """RUNTIME.PY IS THE SINGLE RETRY AUTHORITY. A provider that retried
    on its own would multiply the wall clock and quietly break the
    attempt counts app/explanation/runtime.py's tests assert."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "busy"})

    provider = OllamaExplanationProvider(_settings())
    with pytest.raises(ExplanationUnavailable):
        await _complete(provider, httpx.MockTransport(handler), monkeypatch)

    assert calls == 1


# --- identifier pinning (Prompt 6.4a) -----------------------------------
#
# THE FIX FOR A REAL FAILURE. `{"type": "string", "format": "uuid"}`
# constrains the SHAPE of an evidence id and not its VALUE, so the
# grammar accepted any 32 hex digits and a 7B model transcribing a
# 36-character opaque token got one wrong — a well-formed id that
# existed nowhere in the facts, rejected as `unknown_evidence_id`.
#
# Rewriting those fields as an `enum` of the actual identifiers makes a
# fabricated id unrepresentable at decode time. The validator is
# untouched and still checks membership independently: these tests pin
# the new defence, and `test_the_validator_still_rejects_a_fabricated_id`
# pins that the old one did not go away.


def _explanation_ids(schema: dict[str, Any]) -> Any:
    return schema["$defs"]["ExplanationClaim"]["properties"]["evidence_ids"]


def test_the_explanation_enum_is_exactly_the_supplied_evidence_ids() -> None:
    ids = [str(uuid.uuid4()) for _ in range(3)]

    schema = OllamaExplanationProvider(_settings()).schema_for(ids)

    assert _explanation_ids(schema)["items"] == {"enum": ids}
    # Both places an id can appear, or the unpinned one becomes the way
    # a fabricated id gets in.
    assert schema["properties"]["cited_evidence_ids"]["items"] == {"enum": ids}


def test_the_roadmap_enum_is_exactly_the_supplied_item_ids() -> None:
    ids = [str(uuid.uuid4()) for _ in range(2)]

    schema = OllamaRoadmapProvider(_settings()).schema_for(ids)

    assert schema["$defs"]["NarrativeItem"]["properties"]["item_id"] == {"enum": ids}


def test_zero_evidence_bounds_the_array_instead_of_emptying_the_enum() -> None:
    """`enum: []` matches nothing and is not valid JSON Schema — a
    grammar built from it would reject every possible response,
    including the correct empty array."""
    schema = OllamaExplanationProvider(_settings()).schema_for([])

    assert _explanation_ids(schema)["maxItems"] == 0
    assert schema["properties"]["cited_evidence_ids"]["maxItems"] == 0
    assert _explanation_ids(schema).get("items") != {"enum": []}


def test_zero_roadmap_items_bounds_the_items_array() -> None:
    """A scalar has no "none of them" form, so the containing array is
    what gets bounded."""
    schema = OllamaRoadmapProvider(_settings()).schema_for([])

    assert schema["properties"]["items"]["maxItems"] == 0


def test_the_enum_matches_the_deterministic_catalogue() -> None:
    """SCHEMA AND VALIDATOR CANNOT DRIFT. What the decoder is allowed to
    emit is derived from the same facts the validator checks against —
    if these two ever disagree, every response is rejected."""
    facts = _explanation_facts()
    request = build_explanation_request(facts)

    schema = OllamaExplanationProvider(_settings()).schema_for(request.allowed_ids)

    allowed = set(_explanation_ids(schema)["items"]["enum"])
    assert allowed == {str(key) for key in facts.evidence_by_id()}


def test_the_roadmap_enum_matches_the_decided_items() -> None:
    facts = _roadmap_facts()
    request = build_roadmap_request(facts)

    schema = OllamaRoadmapProvider(_settings()).schema_for(request.allowed_ids)

    pinned = schema["$defs"]["NarrativeItem"]["properties"]["item_id"]["enum"]
    assert set(pinned) == facts.item_ids()


def test_pinning_one_request_does_not_leak_into_the_next() -> None:
    """The base schema is shared across requests; the pin rewrites
    nested nodes. Without a deep copy, one candidate's evidence ids
    would constrain the next candidate's answer."""
    provider = OllamaExplanationProvider(_settings())
    first = [str(uuid.uuid4())]
    second = [str(uuid.uuid4())]

    schema_a = provider.schema_for(first)
    schema_b = provider.schema_for(second)

    assert _explanation_ids(schema_a)["items"] == {"enum": first}
    assert _explanation_ids(schema_b)["items"] == {"enum": second}


@pytest.mark.anyio
async def test_the_pinned_schema_is_what_reaches_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guarantee is about the bytes on the wire, not an internal
    helper."""
    captured: list[dict[str, Any]] = []
    facts = _explanation_facts()
    provider = OllamaExplanationProvider(_settings())

    original = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = _transport(captured)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await provider.complete(build_explanation_request(facts))

    sent = captured[0]["format"]["$defs"]["ExplanationClaim"]["properties"]["evidence_ids"]
    assert sent["items"] == {"enum": [str(key) for key in facts.evidence_by_id()]}


def test_the_validator_still_rejects_a_fabricated_id() -> None:
    """THE BACKSTOP DID NOT GO AWAY. Constraining the decoder narrows
    what a model CAN say; the validator still verifies what it DID say,
    and grounding was not loosened to accommodate a smaller model."""
    facts = _explanation_facts()
    fabricated = str(uuid.uuid4())
    payload = {
        "schema_version": "match_explanation_v1",
        "summary_fit": "You cover most of what this posting asks for.",
        "strengths": [{"text": "You have evidence for Python.", "evidence_ids": [fabricated]}],
        "gaps": [],
        "next_steps": [],
        "cited_evidence_ids": [fabricated],
    }

    with pytest.raises(ExplanationRejected) as error:
        validate_explanation(json.dumps(payload), facts=facts, taxonomy=frozenset())

    assert error.value.reason is RejectionReason.UNKNOWN_EVIDENCE_ID
