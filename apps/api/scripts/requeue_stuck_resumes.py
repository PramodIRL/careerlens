"""One-off operational script: re-enqueue resumes stuck in "queued".

Prompt 2.2's migration (266984262a64) renamed Prompt 2.1's "uploaded"
status to "queued" so old rows use the same vocabulary as new ones —
but that rename is pure SQL (an Alembic `op.execute(...)` UPDATE); it
does not, and should not, also reach into Redis/Celery to enqueue those
rows (a migration must not depend on the broker being reachable just to
apply a schema/data change). The result: any resume that already
existed with status "uploaded" before that migration ran becomes
"queued" with no Celery message ever created for it, and so is never
picked up by app/worker.py.

Run this once, by hand, after applying that migration:
    make requeue-stuck-resumes
(or: cd apps/api && uv run python -m scripts.requeue_stuck_resumes)

Deliberately NOT automatic — no Celery Beat, no worker-startup signal,
no other runtime reconciliation mechanism. This is a one-time
historical data gap, not an ongoing condition, so a script an operator
runs when they choose is a smaller, easier-to-reason-about fix than new
permanent runtime behavior. See docs/decisions.md.

Delegates entirely to app.worker.enqueue_extraction — the same function
the upload endpoint itself calls (app/api/v1/resume.py) — rather than
duplicating any Celery/task logic here. This script never writes to
`resumes`: the only place a resume is allowed to leave "queued" is the
atomic claim in app.worker._claim_resume
(`UPDATE ... WHERE status = 'queued'`). This script's job ends at "make
sure a Celery message exists"; everything after that is the worker's
existing, already-tested responsibility.

Safe to run more than once, including while a worker is actively
processing what a previous run enqueued: it only ever reads rows still
in status == "queued" (anything already claimed — processing,
succeeded, failed — is skipped by that filter), and even a redundant
re-enqueue of an already in-flight resume is harmless — _claim_resume's
atomic UPDATE guarantees exactly one of the resulting task executions
does real work, and the others no-op without touching the row.

Sync entry point, async database access (via `asyncio.run`) — the same
split, for the same reason, as app/worker.py's Celery task: enqueueing
is synchronous work (publishing to the broker), so keeping it outside
the event loop avoids blocking it, and keeps the async part to what is
genuinely async — the SQLAlchemy query.
"""

import asyncio
import logging
import uuid

from sqlalchemy import select

from app.db import build_session_factory
from app.models.resume import Resume
from app.schemas.resume import ResumeStatus
from app.settings import get_settings
from app.worker import enqueue_extraction


async def _fetch_queued_resumes() -> list[tuple[uuid.UUID, str]]:
    """Every resume still in status == "queued", as (id, filename)
    pairs — the filename is for operator-facing output only. Read-only;
    see this module's docstring on why this script never writes."""
    factory = build_session_factory(get_settings().database_url)
    async with factory() as db:
        result = await db.execute(
            select(Resume.id, Resume.original_filename).where(
                Resume.status == ResumeStatus.QUEUED.value
            )
        )
        return [(resume_id, filename) for resume_id, filename in result.all()]


def requeue_stuck_resumes() -> int:
    """Re-enqueues every still-"queued" resume via enqueue_extraction.

    Returns how many were found and enqueued — not how many were
    successfully published, since enqueue_extraction is itself
    best-effort and never raises (see its docstring; that is why main()
    configures logging, so its failure path is visible)."""
    resumes = asyncio.run(_fetch_queued_resumes())

    for resume_id, filename in resumes:
        print(f"  re-enqueuing {resume_id}  ({filename})")
        enqueue_extraction(resume_id)

    return len(resumes)


def main() -> None:
    # enqueue_extraction swallows its own failures (e.g. Redis
    # unreachable) into logger.exception rather than raising, so without
    # this an operator would see "Re-enqueued 3 resume(s)" even when all
    # three failed to publish.
    logging.basicConfig(level=logging.INFO)

    count = requeue_stuck_resumes()
    if count == 0:
        print("No resumes are stuck in 'queued' — nothing to do.")
    else:
        print(f"Re-enqueued {count} resume(s). Make sure `make start-worker` is running.")


if __name__ == "__main__":
    main()
