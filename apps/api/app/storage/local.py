"""Local-filesystem ResumeStorage implementation, used in development
(see app/storage/base.py and docs/project-brief.md's storage phasing).
"""

import asyncio
from functools import lru_cache
from pathlib import Path

from app.settings import get_settings
from app.storage.base import ResumeStorage


class LocalResumeStorage:
    """Writes resume files under a base directory on disk, using `key`
    as the path relative to it (e.g. "{user_id}/{resume_id}.pdf").

    File I/O is genuinely blocking, so every operation runs in a thread
    (`asyncio.to_thread`) rather than on the event loop directly — this
    is an async codebase throughout (SQLAlchemy's async engine, etc.),
    and a blocking `write_bytes()` call would stall every other request
    the server is handling for as long as the write takes.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _path_for(self, key: str) -> Path:
        return self._base_dir / key

    async def save(self, key: str, content: bytes) -> None:
        await asyncio.to_thread(self._save_sync, key, content)

    def _save_sync(self, key: str, content: bytes) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path_for(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    def _delete_sync(self, key: str) -> None:
        self._path_for(key).unlink(missing_ok=True)


@lru_cache
def get_resume_storage() -> ResumeStorage:
    """FastAPI dependency (see app/api/v1/resume.py). A plain function
    rather than only a module-level singleton — like app/db.py's
    get_db — so tests can override it (`app.dependency_overrides`) to
    point at a temporary directory instead of the real
    RESUME_STORAGE_DIR, the same way tests override `get_db` to point
    at an isolated schema instead of the real database."""
    settings = get_settings()
    return LocalResumeStorage(Path(settings.resume_storage_dir))
