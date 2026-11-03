"""AI career assistant and resume tailor, bring your own OpenRouter key.

    POST /api/v1/ai/chat     -> the assistant's reply + tools it used
    POST /api/v1/ai/tailor   -> a tailored resume draft for one saved job
    GET  /api/v1/ai/key      -> "Test key": is the key valid, does the model exist

OPT-IN. The key comes from the `X-OpenRouter-Key` header (the browser's
Settings panel) or the empty-by-default `openrouter_api_key` setting.
With neither, every route here answers 400 "Add your OpenRouter key in
Settings" before calling anything. The model comes from
`X-OpenRouter-Model`, falling back to `openrouter_model`.

AUTH REQUIRED, OWNERSHIP STRUCTURAL. Same `get_current_user` as every
other route, so this is never an open proxy for someone's key, and the
assistant's tools read only the caller's rows (app/assistant/tools.py).
`/key` needs a session too, for the same reason.

NOTHING HERE WRITES A ROW. The conversation lives in the browser and is
sent whole on every message; no chat history, draft or key is stored.
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.assistant.agent import run_chat
from app.assistant.openrouter import (
    MISSING_KEY_MESSAGE,
    OpenRouterClient,
    OpenRouterCredentials,
    OpenRouterError,
    OpenRouterKeyMissing,
    get_openrouter_client,
    resolve_credentials,
)
from app.assistant.tailor import run_tailor
from app.db import get_db
from app.models.user import User
from app.schemas.assistant import (
    ChatRequest,
    ChatResponse,
    KeyCheckResponse,
    TailorRequest,
    TailorResponse,
)

router = APIRouter(tags=["ai"])


def get_credentials(
    x_openrouter_key: str | None = Header(None, alias="X-OpenRouter-Key"),
    x_openrouter_model: str | None = Header(None, alias="X-OpenRouter-Model"),
) -> OpenRouterCredentials:
    try:
        return resolve_credentials(x_openrouter_key, x_openrouter_model)
    except OpenRouterKeyMissing as exc:
        raise HTTPException(status_code=400, detail=MISSING_KEY_MESSAGE) from exc


def _as_http(error: OpenRouterError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    credentials: OpenRouterCredentials = Depends(get_credentials),
    client: OpenRouterClient = Depends(get_openrouter_client),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    history = [{"role": m.role, "content": m.content} for m in body.messages]
    try:
        result = await run_chat(client, credentials, db, current_user, history)
    except OpenRouterError as exc:
        raise _as_http(exc) from exc
    return ChatResponse(reply=result.reply, tools_used=result.tools_used, model=credentials.model)


@router.post("/tailor", response_model=TailorResponse)
async def tailor(
    body: TailorRequest,
    current_user: User = Depends(get_current_user),
    credentials: OpenRouterCredentials = Depends(get_credentials),
    client: OpenRouterClient = Depends(get_openrouter_client),
    db: AsyncSession = Depends(get_db),
) -> TailorResponse:
    try:
        return await run_tailor(
            client, credentials, db, current_user, body.saved_job_id, body.resume_id
        )
    except OpenRouterError as exc:
        raise _as_http(exc) from exc


@router.get("/key", response_model=KeyCheckResponse)
async def check_key(
    current_user: User = Depends(get_current_user),
    credentials: OpenRouterCredentials = Depends(get_credentials),
    client: OpenRouterClient = Depends(get_openrouter_client),
) -> KeyCheckResponse:
    """Costs nothing: OpenRouter's key and model lookups generate no
    tokens. The key itself is never part of the response."""
    try:
        info = await client.key_info(credentials)
        supports_tools = await client.model_supports_tools(credentials)
    except OpenRouterError as exc:
        raise _as_http(exc) from exc

    def _number(name: str) -> float | None:
        value = info.get(name)
        return float(value) if isinstance(value, int | float) else None

    free = info.get("is_free_tier")
    return KeyCheckResponse(
        model=credentials.model,
        model_found=supports_tools is not None,
        model_supports_tools=supports_tools,
        usage=_number("usage"),
        limit=_number("limit"),
        limit_remaining=_number("limit_remaining"),
        is_free_tier=free if isinstance(free, bool) else None,
    )
