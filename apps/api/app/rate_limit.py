"""Minimal in-memory rate limiter for auth endpoints.

Single-process, in-memory only: counts live in a plain dict, reset on
restart, and are NOT shared across multiple API processes or instances.
That's an intentional, documented trade-off for this prompt (see
docs/decisions.md) — fine for local development and a single instance,
but a multi-instance production deployment would need a shared store
(e.g. Redis, already provisioned via docker-compose but not used for
this yet) so every instance enforces the same limit.
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from app.settings import get_settings

# key -> timestamps (seconds, time.monotonic()) of recent requests
_request_log: dict[str, list[float]] = defaultdict(list)


def _client_key(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    return f"auth:{client_host}"


async def rate_limit_auth(request: Request) -> None:
    """FastAPI dependency: raise 429 if this client has exceeded the
    configured request rate for auth endpoints (register/login/refresh
    share one budget)."""
    settings = get_settings()
    key = _client_key(request)
    now = time.monotonic()
    window_start = now - settings.auth_rate_limit_window_seconds

    timestamps = _request_log[key]
    timestamps[:] = [t for t in timestamps if t > window_start]

    if len(timestamps) >= settings.auth_rate_limit_max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many requests, try again later",
        )

    timestamps.append(now)
