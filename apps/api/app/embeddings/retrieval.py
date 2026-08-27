"""Nearest-neighbour lookup over stored embeddings (Prompt 5.2a).

Reads what app/embeddings/store.py wrote. Splits from that module the
same way it split from content.py: this one only ASKS questions of the
table, never writes to it.

WHAT THIS DELIBERATELY DOES NOT DO. It returns evidence a query text
sits near in vector space — nothing else. It does not decide that a
candidate has a skill, does not mark a requirement satisfied, does not
compute a score, and is not called by `skill_match_v1` or
`skill_gap_v1`. `SemanticHit` carries no skill id and no satisfied
flag, so there is no way to express "this hit satisfies that
requirement" with the types this module returns — required-skill safety
is a property of the shape, not a check someone has to remember. The
scoring component is a later slice.

COSINE DISTANCE, ASCENDING. pgvector's `<=>` is a DISTANCE (0 = same
direction, 2 = opposite), so nearest means SMALLEST and the ORDER BY is
ascending. `similarity` in the results is `1 - distance`, which is the
number a human expects to read. Getting this backwards returns the
least relevant rows in a plausible-looking order, which is why the
conversion happens once, here, rather than at each call site.

NO THRESHOLD IS BUILT IN. `min_similarity` defaults to None because
this project has no way to calibrate one yet: the only provider is
app/embeddings/provider.py's deterministic mock, whose vectors are
measurably indistinguishable from random (its docstring is explicit
that it carries no semantic structure). A cutoff chosen against that
distribution would be numerology dressed up as a constant. Callers may
pass one once a real provider exists and there is something to
calibrate against.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Float, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.embedding import Embedding
from app.models.skill_evidence import SkillEvidence
from app.schemas.embedding import EmbeddingSourceType


@dataclass(frozen=True)
class SemanticHit:
    """One stored embedding a query vector sits near.

    Everything needed to trace the hit back to the row it came from and
    to explain it to a reader — and nothing that could be mistaken for a
    claim about the candidate. There is deliberately no `skill_id`, no
    `satisfied`, and no score.
    """

    embedding_id: uuid.UUID
    source_type: str
    source_id: str
    chunk_index: int
    similarity: float
    model_identifier: str


@dataclass(frozen=True)
class EvidenceHit:
    """A `SemanticHit` joined to the skill evidence it points at.

    `excerpt` is read from the evidence row, NOT stored on the
    embedding — app/models/embedding.py keeps no copy of source text, so
    this join is how a chunk gets quoted back. That is the whole reason
    this type exists rather than callers hydrating hits themselves.
    """

    hit: SemanticHit
    evidence_id: uuid.UUID
    excerpt: str | None
    evidence_source_type: str
    evidence_source_identifier: str


async def find_similar(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    query_vector: list[float],
    model_identifier: str,
    limit: int,
    source_types: Sequence[str] | None = None,
    min_similarity: float | None = None,
) -> list[SemanticHit]:
    """Nearest stored embeddings to `query_vector`, closest first.

    `user_id` and `model_identifier` are REQUIRED keywords, both always
    in the WHERE clause:

      * user_id — retrieval must never cross users. Combined with
        `user_id` being part of the natural key, another user's rows are
        not merely filtered out, they are unaddressable through this
        function.
      * model_identifier — two models' vectors occupy different spaces,
        so a distance between them is a meaningless number rather than a
        weak signal. Mixing them would silently rank noise.

    Ties are broken by `embedding_id` so that equal distances come back
    in a stable order; without it PostgreSQL may return them in any
    order and two identical requests could disagree.
    """
    # 1 - distance, computed in SQL so ordering and the returned number
    # can never disagree about which row is nearest.
    distance = Embedding.embedding.cosine_distance(query_vector)
    similarity = (1 - distance).cast(Float).label("similarity")

    statement = select(
        Embedding.id,
        Embedding.source_type,
        Embedding.source_id,
        Embedding.chunk_index,
        Embedding.model_identifier,
        similarity,
    ).where(
        Embedding.user_id == user_id,
        Embedding.model_identifier == model_identifier,
    )

    if source_types is not None:
        statement = statement.where(Embedding.source_type.in_(list(source_types)))
    if min_similarity is not None:
        statement = statement.where(similarity >= min_similarity)

    statement = statement.order_by(distance.asc(), Embedding.id.asc()).limit(limit)

    return [
        SemanticHit(
            embedding_id=row.id,
            source_type=row.source_type,
            source_id=row.source_id,
            chunk_index=row.chunk_index,
            similarity=float(row.similarity),
            model_identifier=row.model_identifier,
        )
        for row in (await db.execute(statement)).all()
    ]


async def find_similar_evidence(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    query_vector: list[float],
    model_identifier: str,
    limit: int,
    min_similarity: float | None = None,
) -> list[EvidenceHit]:
    """`find_similar`, restricted to skill evidence and joined to it.

    TWO QUERIES, ALWAYS — one to rank, one to hydrate every matched
    evidence row via a single `IN (...)`. Not one query per hit: the
    ranking query returns up to `limit` rows, and looking each one up on
    its own is the N+1 this function exists to avoid.

    Hydration preserves the ranking order rather than the order
    PostgreSQL returns the evidence rows in. A hit whose evidence row
    has since been deleted is dropped rather than yielded with an empty
    excerpt — `source_id` is a polymorphic string with no foreign key
    behind it (see app/models/embedding.py), so a dangling reference is
    possible and must not surface as a hit a reader cannot verify.
    """
    hits = await find_similar(
        db,
        user_id=user_id,
        query_vector=query_vector,
        model_identifier=model_identifier,
        limit=limit,
        source_types=[EmbeddingSourceType.SKILL_EVIDENCE.value],
        min_similarity=min_similarity,
    )
    if not hits:
        return []

    evidence_ids = [uuid.UUID(hit.source_id) for hit in hits]
    evidence = {
        row.id: row
        for row in (
            await db.scalars(select(SkillEvidence).where(SkillEvidence.id.in_(evidence_ids)))
        ).all()
    }

    results: list[EvidenceHit] = []
    for hit in hits:
        row = evidence.get(uuid.UUID(hit.source_id))
        if row is None:
            continue
        results.append(
            EvidenceHit(
                hit=hit,
                evidence_id=row.id,
                excerpt=row.excerpt,
                evidence_source_type=row.source_type,
                evidence_source_identifier=row.source_identifier,
            )
        )
    return results
