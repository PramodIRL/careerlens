"""Local inference against an Ollama daemon (Prompt 6.4).

WHY LOCAL, AND WHY THIS SHAPE. CareerLens must cost a user nothing to
run, so there is no hosted provider and no per-request meter. The
weights are open, and inference happens on the machine already serving
the application — which makes three things true at once: the product
has no recurring inference cost, no candidate data leaves the host, and
a demo works with the network unplugged.

THERE IS NO CREDENTIAL IN THIS MODULE AND THERE NEVER WILL BE. Not a
key read from the environment, not a token, not a header. That is the
cleanest possible answer to "never log a secret": there is nothing to
log. Same posture .env.example already documents for GitHub and
embeddings.

NO NEW DEPENDENCY. Ollama's API is a JSON POST, and `httpx` has been a
runtime dependency since Prompt 3.1. Adding an SDK to send one request
would be a package to justify, audit and upgrade for no capability.

A DAEMON, NOT AN IN-PROCESS MODEL — deliberately, and not merely for
convenience. `uvicorn --reload` restarts this process on every file
save, and a five-gigabyte model reloaded on each save is not a
development environment anybody uses. The app is also multi-process
(API plus a Celery worker); a daemon holds one copy of the weights
where in-process inference would hold one per process on a 16 GB
machine.

THIS MODULE ADDS NO RELIABILITY LOGIC. No retry, no backoff, no second
attempt of any kind. Timeouts and retries belong to
app/explanation/runtime.py and stay there — it is the single authority
for both features, and a provider that quietly retried on its own would
multiply both the wall clock and the attempt count runtime.py's tests
assert. `httpx` does not retry by default, which is part of why it fits
here without having to be told not to.

STRUCTURED OUTPUT IS ENFORCED BY THE DAEMON, NOT ASKED FOR IN PROSE.
The JSON schema travels in the request's `format` field and constrains
decoding token by token, so what comes back parses and matches the
schema. That does NOT make it TRUE — well-formedness and grounding are
different questions, and app/explanation/validate.py and
app/roadmap/validate.py remain the only things that decide whether an
answer may be shown.

IDENTIFIERS ARE PINNED TO THE FACTS, per request. This is the fix for a
real failure: `{"type": "string", "format": "uuid"}` constrains the
SHAPE of an evidence id and not its VALUE, so the grammar happily
accepted any 32 hex digits and a 7B model transcribing a 36-character
opaque token got one wrong — producing a perfectly well-formed id that
existed nowhere in the facts. Rewriting those fields as an `enum` of
the actual identifiers makes a fabricated id UNREPRESENTABLE at decode
time rather than merely rejected afterwards, which is the same move
that made "the model cannot add a roadmap item" structural in 6.3.

Note what this is NOT. It is not a relaxation, not a remap and not a
fuzzy match: nothing here repairs a wrong id, and the validators still
check membership independently on the way back. Narrowing what a model
CAN say and verifying what it DID say are two defences, and this adds
the first without touching the second.
"""

import copy
from collections.abc import Sequence
from typing import Any

import httpx

from app.explanation.prompt import ExplanationRequest
from app.explanation.provider import ExplanationTimeout, ExplanationUnavailable
from app.explanation.schema import MatchExplanation
from app.roadmap.schema import RoadmapNarrative
from app.settings import Settings, get_settings

OLLAMA_PROVIDER_NAME = "ollama"

# Relative to the configured base URL, so a daemon on another host or
# port needs no code change.
_CHAT_PATH = "/api/chat"

# Low, but not zero. This is a phrasing task over fixed facts, so
# creativity is not the goal — but greedy decoding on a small model
# tends to repeat itself across items, which is precisely the flat,
# templated feel the mock already has and this provider exists to
# escape.
_TEMPERATURE = 0.3


def _pin_array(node: dict[str, Any], allowed: Sequence[str]) -> None:
    """Constrain an array-of-identifiers field to exactly `allowed`.

    An empty allowlist becomes `maxItems: 0` rather than `enum: []`: an
    empty enum matches nothing at all and is not valid JSON Schema, so a
    grammar built from it would reject every possible response —
    including the correct empty array.
    """
    if allowed:
        node["items"] = {"enum": list(allowed)}
    else:
        node["maxItems"] = 0


def _pin_scalar(node: dict[str, Any], allowed: Sequence[str]) -> None:
    """Constrain a single-identifier field to exactly `allowed`.

    Left alone when the allowlist is empty — a scalar has no "none of
    them" form. The caller bounds the containing array instead.
    """
    if allowed:
        node.clear()
        node["enum"] = list(allowed)


def _pin_int(node: dict[str, Any], allowed: Sequence[int]) -> None:
    """Constrain an integer identifier field to exactly `allowed`.

    `NarrativeWeek.week` was `{"minimum": 1, "maximum": 8}` — bounded but
    open, so a model told the plan spans four weeks themed all four while
    only weeks 1 and 3 held work, and was rejected every time. Pinning
    the real list makes the wrong answer unrepresentable, exactly as the
    item-id enum does. Left alone when empty: the containing array is
    bounded instead.
    """
    if allowed:
        node.clear()
        node["enum"] = list(allowed)


def _pin_required_string(schema: dict[str, Any], name: str) -> None:
    """Force an optional string field to be present and non-null.

    The mirror of `_pin_null`, and needed for the same reason. When the
    result DOES have a gap, `str | None` lets a model answer `null` and
    skip the sentence — which is what a 7B model did on a three-gap job,
    covering the gaps in the list and leaving the summary reading as
    though the match were clean.

    Nullable means optional to a grammar. If the facts say there is a
    gap, the gap half of the summary is not optional.
    """
    node = schema["properties"][name]
    branches = node.get("anyOf")
    if branches:
        # Keep the string branch's own bounds; drop the null one.
        string_branch = next(item for item in branches if item.get("type") == "string")
        node.clear()
        node.update(string_branch)
    node.setdefault("minLength", 1)
    required = schema.setdefault("required", [])
    if name not in required:
        required.append(name)


def _pin_null(node: dict[str, Any]) -> None:
    """Constrain an optional field to exactly `null`.

    `summary_gap` is `str | None`, so the grammar happily accepts a
    sentence there whatever the facts say — and on a result with NO gap
    a 7B model wrote one, because "where you stand, and here is the gap"
    is the shape of every summary it has ever seen. Rewriting the node
    as `{"type": "null"}` makes the gap sentence unrepresentable rather
    than wrong, which is the same move the identifier enums make.

    Prose was tried first and is still there; it is not what this
    relies on.
    """
    node.clear()
    node["type"] = "null"


def _translate(error: Exception) -> Exception:
    """Map a transport failure onto Prompt 6.2's taxonomy.

    TRANSIENT vs TERMINAL is the only distinction runtime.py needs, and
    it is drawn here rather than there so the runtime stays free of any
    knowledge about how a particular provider fails.

    A connect error is the interesting case: locally it almost always
    means "the daemon is not running", which a retry will not fix
    within half a second. It is still classified transient, because the
    alternative is a provider deciding policy — and one retry against a
    closed socket costs milliseconds, while misclassifying a genuinely
    transient blip costs the user their explanation.
    """
    if isinstance(error, httpx.TimeoutException):
        return ExplanationTimeout("ollama did not answer in time")
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status >= 500:
            return ExplanationUnavailable(f"ollama returned {status}")
        # 404 is the one every operator hits: the daemon is up but the
        # model was never pulled. Terminal, because retrying cannot
        # download a model — and the message has to say so, or somebody
        # spends an afternoon looking at the network.
        return RuntimeError(
            f"ollama rejected the request with {status}; a 404 usually means the model "
            f"is not pulled — run `ollama pull <model>`"
        )
    if isinstance(error, httpx.HTTPError):
        return ExplanationUnavailable(f"ollama unreachable ({type(error).__name__})")
    return error


async def complete(
    request: ExplanationRequest,
    *,
    schema: dict[str, Any],
    settings: Settings,
) -> str:
    """One generation, returned as RAW TEXT.

    Deliberately a string rather than a parsed object. The validators
    own parsing, the size ceiling and every grounding rule; handing them
    something already parsed would move part of that decision into this
    module, where no test looks for it.

    The instruction and the untrusted data travel as SEPARATE messages
    rather than one concatenated blob. Same reasoning as Prompt 6.2: the
    excerpts are third-party text, they stay inside their own JSON
    string values, and the boundary is structural rather than a
    delimiter something in the data could imitate.
    """
    payload: dict[str, Any] = {
        "model": settings.explanation_model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.delimited or request.data_json},
        ],
        # Constrained decoding against our own Pydantic schema.
        "format": schema,
        "stream": False,
        # Keeps the weights resident between requests. Without it the
        # daemon evicts the model after a few minutes and the next user
        # pays a multi-second reload on top of generation.
        "keep_alive": settings.explanation_keep_alive,
        "options": {
            "num_predict": settings.explanation_max_output_tokens,
            "temperature": _TEMPERATURE,
        },
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            # The runtime applies its own outer budget; this one exists
            # so the socket is closed cleanly by the client rather than
            # cancelled mid-flight from outside.
            timeout=httpx.Timeout(settings.explanation_timeout_seconds),
        ) as client:
            response = await client.post(_CHAT_PATH, json=payload)
            response.raise_for_status()
            body = response.json()
    except Exception as error:
        raise _translate(error) from error

    try:
        content = body["message"]["content"]
    except (KeyError, TypeError) as error:
        # A well-formed HTTP response that is not an Ollama chat
        # response at all — a proxy's error page, or a different service
        # listening on that port. Terminal: the shape will not change on
        # a second attempt.
        raise RuntimeError("ollama returned an unexpected response shape") from error

    if not isinstance(content, str):  # pragma: no cover - defensive
        raise RuntimeError("ollama returned a non-text message")
    return content


async def check_available(settings: Settings | None = None) -> bool:
    """Whether the daemon is reachable and the model is present.

    Used by the live smoke test to SKIP rather than fail, the same way
    tests/test_semantic_fit.py skips when the embedding model has not
    been downloaded: a missing local model is an environment fact, not a
    defect in this code.
    """
    settings = settings or get_settings()
    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url, timeout=httpx.Timeout(5.0)
        ) as client:
            response = await client.post("/api/show", json={"model": settings.explanation_model})
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def describe_unavailable(settings: Settings) -> str:
    """The message a misconfigured deployment gets, in full.

    Names the model, the URL and the command that fixes it — and says
    that the product still works without a model at all. An error
    reading only "provider unavailable" sends somebody to the wrong
    place, and this is the failure every first-time setup hits.
    """
    return (
        f"explanation provider is configured as {OLLAMA_PROVIDER_NAME!r} but the daemon at "
        f"{settings.ollama_base_url} did not respond. Start it with `ollama serve`, and make "
        f"sure the model is pulled: `ollama pull {settings.explanation_model}`. To run "
        f"without a model instead, set EXPLANATION_PROVIDER=mock — the scores, gaps and "
        f"roadmap are deterministic and never needed one."
    )


# --------------------------------------------------------------------
# The two providers.
#
# THIN ON PURPOSE. Each binds one Pydantic schema to the shared
# `complete()` above and does nothing else — no request shaping, no
# error handling, no retry. Two features, one client, one place where a
# transport failure is classified. The alternative was two copies of
# `_translate`, and app/matching/resolve.py already documents what
# happens to a judgement implemented twice.
#
# The schema is computed ONCE per provider instance rather than per
# request: it is a pure function of a frozen Pydantic model, and
# regenerating it on every generation would be work with no output.
# --------------------------------------------------------------------


class OllamaExplanationProvider:
    """A real model writing match explanations, locally.

    Holds the BASE schema and pins a copy of it per request: the
    identifiers differ every time, so the allowlist cannot be computed
    once. `deepcopy` because the pin rewrites nested nodes and mutating
    the cached base would leak one request's evidence ids into the next.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_schema = MatchExplanation.model_json_schema()

    @property
    def name(self) -> str:
        return OLLAMA_PROVIDER_NAME

    def schema_for(
        self, allowed_ids: Sequence[str], allows_gap_claims: bool = True
    ) -> dict[str, Any]:
        """The output schema for one request, pinned to these ids.

        Public because it is what the offline tests assert on — the
        guarantee is about the schema that reaches the daemon, and
        proving it through a mocked transport alone would leave the
        interesting half untested.
        """
        schema = copy.deepcopy(self._base_schema)
        _pin_array(schema["$defs"]["ExplanationClaim"]["properties"]["evidence_ids"], allowed_ids)
        _pin_array(schema["properties"]["cited_evidence_ids"], allowed_ids)
        # AT LEAST ONE NEXT STEP (Prompt 6.4c). `next_steps` defaults to
        # an empty list, so a grammar happily emits one — and on a
        # complete match a live run did exactly that, producing an
        # explanation with no advice in it at all. There is always
        # something worth doing; on a job with no gaps it is proof
        # rather than learning, which the instruction says per request.
        #
        # Pinned in the DECODER only. `MatchExplanation` keeps its
        # permissive default, so a rejected narrative and the mock are
        # unaffected and the public contract does not move.
        schema["properties"]["next_steps"]["minItems"] = 1
        # AND REQUIRED, WHICH IS THE HALF THAT ACTUALLY BITES. Pydantic
        # gives `next_steps` a default, so it is absent from the schema's
        # `required` list — and a grammar that lets the key be omitted
        # never reaches `minItems` at all. Measured: two live runs out of
        # five on a complete match simply left the key out, and Pydantic
        # filled in the empty default on the way back, so the ceiling
        # looked enforced while nothing enforced it.
        required = schema.setdefault("required", [])
        if "next_steps" not in required:
            required.append("next_steps")
        if not allows_gap_claims:
            # NO GAP MEANS NO GAP SENTENCE, STRUCTURALLY (Prompt 6.4b).
            #
            # This is the 6.4 failure the browser test found: a 100%
            # match with nothing missing, and an explanation reading
            # "REST APIs is a required skill and your profile does not
            # show it" about a skill that was matched and merely
            # mentioned. Every word was in vocabulary and no number was
            # invented, so the existing layers passed it.
            #
            # The instruction now says so twice — in the RESULT block and
            # in the rules — but a summary is "where you stand, and here
            # is the gap", and as long as the second slot EXISTS a small
            # model fills it. Removing the slot is the fix; saying "leave
            # it empty" is a request.
            #
            # Same move as `strengths` with no evidence below, and the
            # validator's `_check_consistency` still checks the property
            # independently on the way back.
            _pin_null(schema["properties"]["summary_gap"])
            schema["properties"]["gaps"]["maxItems"] = 0
        else:
            # AND THE MIRROR. A nullable field is an optional one to a
            # grammar, so on a three-gap job the model answered `null`
            # and the summary read as though the match were clean. If
            # the facts say there is a gap, writing about it is not
            # optional.
            _pin_required_string(schema, "summary_gap")
        if not allowed_ids:
            # NO EVIDENCE MEANS NO STRENGTHS, structurally.
            #
            # Every strength must cite at least one evidence id; with
            # none supplied, every possible strength is uncitable and the
            # only valid answer is an empty list. Telling the model that
            # in prose was tried twice — in the style section and again
            # in the RULES block — and a 7B model wrote a strength anyway
            # 4 times out of 4, because "you matched Python" is right
            # there in the facts and writing about it is the obvious
            # thing to do.
            #
            # Bounding the array is the same move that fixed evidence
            # ids, weeks and duplicate weeks: make the wrong answer
            # unrepresentable instead of rejecting it afterwards. The
            # match still reaches the reader — the summary covers it —
            # and `_check_citations` is untouched and still independently
            # refuses an uncited strength.
            schema["properties"]["strengths"]["maxItems"] = 0
        return schema

    async def complete(self, request: ExplanationRequest) -> str:
        return await complete(
            request,
            schema=self.schema_for(request.allowed_ids, request.allows_gap_claims),
            settings=self._settings,
        )


class OllamaRoadmapProvider:
    """A real model writing roadmap narratives, locally.

    `item_id` is a bare string in the base schema — even less
    constrained than an evidence id, which at least had a UUID shape —
    so the same per-request pin applies, and with no items at all the
    containing array is bounded to zero instead.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_schema = RoadmapNarrative.model_json_schema()

    @property
    def name(self) -> str:
        return OLLAMA_PROVIDER_NAME

    def schema_for(
        self,
        allowed_ids: Sequence[str],
        allowed_weeks: Sequence[int] = (),
        allowed_step_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """The output schema for one request, pinned to these item ids,
        these step ids, and the weeks that actually hold work."""
        schema = copy.deepcopy(self._base_schema)
        _pin_scalar(schema["$defs"]["NarrativeItem"]["properties"]["item_id"], allowed_ids)
        _pin_scalar(schema["$defs"]["NarrativeStep"]["properties"]["step_id"], allowed_step_ids)
        _pin_int(schema["$defs"]["NarrativeWeek"]["properties"]["week"], allowed_weeks)
        # AN ENUM CONSTRAINS EACH ELEMENT, NOT THE ARRAY. A six-item plan
        # came back with weeks [1, 2, 3, 2] — every value legal, the
        # array not: week 2 twice. Bounding the length to the number of
        # permitted values makes a duplicate overflow the array, which
        # the grammar does enforce, where `uniqueItems` is not reliably
        # supported by schema-to-grammar converters.
        #
        # Zero permitted values means an empty array, which the same
        # expression gives for free — a scalar has no "none of them"
        # form, so the array is where both cases are handled.
        schema["properties"]["items"]["maxItems"] = len(allowed_ids)
        schema["properties"]["weeks"]["maxItems"] = len(allowed_weeks)
        schema["properties"]["steps"]["maxItems"] = len(allowed_step_ids)
        return schema

    async def complete(self, request: ExplanationRequest) -> str:
        return await complete(
            request,
            schema=self.schema_for(
                request.allowed_ids,
                request.allowed_weeks,
                request.allowed_step_ids,
            ),
            settings=self._settings,
        )
