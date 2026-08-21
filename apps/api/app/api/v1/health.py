from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def get_health() -> dict[str, str]:
    """Report that the API process is up and able to serve requests."""
    return {"status": "ok"}
