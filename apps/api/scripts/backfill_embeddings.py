"""Populate the embeddings table from data already in the database
(Prompt 5.2a).

Run it with:
    make embeddings-backfill
(or: cd apps/api && uv run python -m scripts.backfill_embeddings)

WHY A SCRIPT AND NOT A WORKER HOOK. Prompt 5.1 built the write path
(app/embeddings/store.py) but wired no caller, so the table is empty.
The obvious place to call it from is the resume and GitHub ingestion
workers — which is exactly why this is a script instead: those paths are
on Prompt 5.2's do-not-change list, and quietly editing them to add a
side effect would be the kind of scope creep that breaks a Phase 4
acceptance run. Worker integration is its own reviewed slice.

Safe and deterministic to run repeatedly. `upsert_embeddings` compares a
stored SHA-256 `content_hash` before doing anything, so a second run
over unchanged data calls the provider zero times and writes nothing —
the counts printed at the end are what proves it.

READS EVERY USER'S DATA, WRITES EACH USER'S OWN ROWS. This is an
operator tool run against the whole database, not a request handler, so
it deliberately is not scoped to one user. Ownership is still exact:
every document carries the `user_id` of the row it came from
(app/embeddings/content.py), and `user_id` is part of the embedding's
natural key. Retrieval is what must never cross users, and it does not
(app/embeddings/retrieval.py).

NO REAL PROVIDER AND NO CREDENTIAL. It uses whatever
`get_embedding_provider()` is configured to build, which today is only
the deterministic local mock.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import build_session_factory
from app.embeddings.content import (
    EmbeddingDocument,
    evidence_documents,
    job_description_documents,
    repository_documents,
)
from app.embeddings.provider import EmbeddingProvider, get_embedding_provider
from app.embeddings.store import EmbeddingSyncSummary, upsert_embeddings
from app.models.candidate_skill import CandidateSkill
from app.models.github_repository import GitHubRepository
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.settings import get_settings


async def collect_documents(db: AsyncSession) -> list[EmbeddingDocument]:
    """Every document the content helpers say is worth embedding.

    Four bounded queries, not one per row: the evidence pairs are built
    from a single join rather than looking each candidate skill up
    per evidence row. What is and is not embeddable is decided entirely
    by app/embeddings/content.py — this function only supplies rows.
    """
    jobs = (await db.scalars(select(SavedJob))).all()
    repositories = (await db.scalars(select(GitHubRepository))).all()
    pairs = (
        await db.execute(
            select(CandidateSkill, SkillEvidence).join(
                SkillEvidence, SkillEvidence.candidate_skill_id == CandidateSkill.id
            )
        )
    ).all()

    documents: list[EmbeddingDocument] = []
    for job in jobs:
        documents.extend(job_description_documents(job))
    documents.extend(repository_documents(list(repositories)))
    documents.extend(evidence_documents([(row[0], row[1]) for row in pairs]))
    return documents


async def backfill_embeddings(
    db: AsyncSession, provider: EmbeddingProvider
) -> EmbeddingSyncSummary:
    """Embed and store everything currently embeddable.

    Commits once at the end: a failure partway through leaves the table
    exactly as it was, and a re-run starts clean.
    """
    documents = await collect_documents(db)
    summary = await upsert_embeddings(db, provider, documents)
    await db.commit()
    return summary


async def _main() -> None:
    provider = get_embedding_provider()
    factory = build_session_factory(get_settings().database_url)
    async with factory() as db:
        summary = await backfill_embeddings(db, provider)

    print(f"model:    {provider.model_identifier}")
    print(f"computed: {summary.computed}")
    print(f"written:  {summary.written}")
    print(f"skipped:  {summary.skipped} (content unchanged — provider not called)")
    if not summary.computed:
        print("Embeddings already up to date — nothing recomputed.")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
