from typing import Protocol


class ResumeStorage(Protocol):
    """Minimal interface for storing a resume file's raw bytes behind an
    opaque key, and reading or removing it again later.

    `app/api/v1/resume.py` (resume domain/route code) only ever talks to
    this interface, never to a concrete implementation — `LocalResumeStorage`
    (app/storage/local.py) is the only one that exists today, per
    docs/project-brief.md's phasing ("local files behind an interface
    during development; S3-compatible object storage only for
    production"). Adding an S3-compatible implementation later is a new
    class plus one branch in `get_resume_storage()`, not a rewrite of
    any route or domain code.

    Deliberately just three methods — the full surface a resume file
    needs. No "list" (that's a database query over `resumes` rows, not a
    storage-layer concern) and no update-in-place (re-uploading creates a
    new resume row instead).

    `key` is always a server-generated, opaque identifier (see
    `resumes.storage_key`) — never derived from user-supplied input —
    so implementations don't need to defend against path traversal in
    `key` itself.
    """

    async def save(self, key: str, content: bytes) -> None:
        """Write `content` under `key`, creating it if needed."""
        ...

    async def read(self, key: str) -> bytes:
        """Return the bytes stored under `key`.

        Raises FileNotFoundError if `key` doesn't exist. Not called by
        any route in Prompt 2.1 (there is no download/view endpoint
        yet) — included because a storage interface with no read path
        isn't a believable one, and Prompt 2.2's extraction step will
        need it.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove the content stored under `key`.

        Idempotent: deleting a `key` that doesn't (or no longer) exist
        is not an error, the same way `POST /auth/logout` with no
        session is a no-op rather than a failure.
        """
        ...
