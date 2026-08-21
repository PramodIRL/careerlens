from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db

router = APIRouter()


@router.get("/health")
def get_health() -> dict[str, str]:
    """Report that the API process is up and able to serve requests.

    This is a liveness check — it never touches the database. See
    /health/db for a readiness check of the database dependency.
    """
    return {"status": "ok"}


@router.get("/health/db")
async def get_health_db(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Report whether the database is reachable.

    Runs a trivial query with a short connect timeout. Returns 200 when
    it succeeds, or 503 with a generic "degraded" body when it doesn't —
    deliberately never includes connection details, host, or credentials
    in the response.
    """
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "detail": "database unavailable"}
    return {"status": "ok"}
