"""OpenRouter credentials and the one HTTP client that uses them.

    POST {base}/chat/completions   (assistant + tailor)
    GET  {base}/key                (the Settings panel's "Test key")
    GET  {base}/models/{id}/endpoints  (does the chosen model exist)

WHERE THE KEY COMES FROM. The `X-OpenRouter-Key` request header first,
typed by the user into the browser; otherwise the `openrouter_api_key`
setting, which is empty by default. Neither present is a 400 the UI can
show as-is, raised before anything is called.

THE KEY NEVER LEAVES THIS MODULE EXCEPT IN THE AUTHORIZATION HEADER.
`OpenRouterCredentials` hides it from `repr`, nothing here logs a
request or a response body, and every failure is translated into
`OpenRouterError` with a message written here. The one upstream text
that is passed through (a 400/404's reason, which is how a mistyped
model id is explained) has the key scrubbed from it first.

NO RETRY HERE. `httpx` does not retry by default. A model call is slow
and billed to the user, so re-sending one is the caller's decision, and
only the tailor makes it, on invalid output.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.settings import Settings, get_settings

MISSING_KEY_MESSAGE = "Add your OpenRouter key in Settings"

# A generous budget for one completion. A tool-calling step with a large
# resume in context can take tens of seconds on a hosted model.
_TIMEOUT_SECONDS = 60.0


class OpenRouterKeyMissing(Exception):
    """No key in the request header and none configured on the server."""


class OpenRouterError(Exception):
    """A call failed. `status_code` is what OUR API should answer with,
    and `message` is safe to show the user: it never contains the key."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class OpenRouterCredentials:
    api_key: str = field(repr=False)
    model: str


def resolve_credentials(
    header_key: str | None, header_model: str | None, settings: Settings | None = None
) -> OpenRouterCredentials:
    """Header first, then the server fallback. Raises before any call
    when neither supplies a key."""
    settings = settings or get_settings()
    api_key = (header_key or "").strip() or settings.openrouter_api_key.strip()
    if not api_key:
        raise OpenRouterKeyMissing(MISSING_KEY_MESSAGE)
    model = (header_model or "").strip() or settings.openrouter_model
    return OpenRouterCredentials(api_key=api_key, model=model)


def _upstream_reason(response: httpx.Response, api_key: str) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return message.replace(api_key, "[redacted]")[:300]


def _translate_status(response: httpx.Response, api_key: str) -> OpenRouterError:
    """Map OpenRouter's status onto what our API answers.

    Problems the user can fix in Settings (a bad key, no credits, a
    model id that does not exist) are 400s with a message that says
    which. A 401 is deliberately NOT passed through as a 401: the web
    client treats our 401 as "your CareerLens session expired".
    """
    status = response.status_code
    if status in (401, 403):
        return OpenRouterError(400, "OpenRouter rejected this key. Check it in Settings.")
    if status == 402:
        return OpenRouterError(400, "Your OpenRouter account is out of credits.")
    if status == 429:
        return OpenRouterError(429, "OpenRouter is rate limiting this key. Try again shortly.")
    if status in (400, 404):
        reason = _upstream_reason(response, api_key)
        detail = f": {reason}" if reason else ""
        return OpenRouterError(400, f"OpenRouter could not run this request{detail}")
    return OpenRouterError(502, f"OpenRouter returned an error ({status}). Try again shortly.")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenRouterError(502, "OpenRouter returned a response that is not JSON.") from exc
    if not isinstance(body, dict):
        raise OpenRouterError(502, "OpenRouter returned an unexpected response.")
    return body


class OpenRouterClient:
    """OpenAI-compatible chat client. The transport is injectable so
    tests drive it through `httpx.MockTransport` with no network."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def _request(
        self, credentials: OpenRouterCredentials, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {credentials.api_key}", "X-Title": "CareerLens"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                return await http.request(
                    method, f"{self._base_url}{path}", headers=headers, **kwargs
                )
        except httpx.TimeoutException as exc:
            raise OpenRouterError(504, "OpenRouter did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise OpenRouterError(502, "Could not reach OpenRouter.") from exc

    async def _send(
        self, credentials: OpenRouterCredentials, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        response = await self._request(credentials, method, path, **kwargs)
        if response.status_code >= 400:
            raise _translate_status(response, credentials.api_key)
        return _json_object(response)

    async def complete(
        self,
        credentials: OpenRouterCredentials,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One chat completion. Returns the first choice's `message`."""
        payload: dict[str, Any] = {"model": credentials.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        body = await self._send(credentials, "POST", "/chat/completions", json=payload)
        # OpenRouter can answer 200 with an error object when the routed
        # provider fails mid-request.
        if isinstance(body.get("error"), dict):
            raise OpenRouterError(502, "The model provider failed on this request. Try again.")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenRouterError(502, "OpenRouter returned no answer.")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise OpenRouterError(502, "OpenRouter returned no answer.")
        return message

    async def key_info(self, credentials: OpenRouterCredentials) -> dict[str, Any]:
        """OpenRouter's own description of the key: label, usage, limit.
        Costs nothing and generates no tokens."""
        body = await self._send(credentials, "GET", "/key")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def model_supports_tools(self, credentials: OpenRouterCredentials) -> bool | None:
        """Whether any endpoint serving the chosen model accepts `tools`.
        None when OpenRouter does not know the model id at all, which is
        how a typo in the free-text model field shows up before a chat."""
        response = await self._request(credentials, "GET", f"/models/{credentials.model}/endpoints")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise _translate_status(response, credentials.api_key)
        data = _json_object(response).get("data")
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        if not isinstance(endpoints, list):
            return None
        return any(
            isinstance(endpoint, dict) and "tools" in (endpoint.get("supported_parameters") or [])
            for endpoint in endpoints
        )


def get_openrouter_client() -> OpenRouterClient:
    """FastAPI dependency. Tests replace it through
    `app.dependency_overrides` with a client on a MockTransport."""
    return OpenRouterClient(base_url=get_settings().openrouter_base_url)
