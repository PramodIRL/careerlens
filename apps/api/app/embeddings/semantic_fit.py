"""Semantic relevance for one saved job (Prompt 5.2b).

WHAT THIS IS NOT. `semantic_fit_v1` IS NOT A CALIBRATED HIRING OR
RANKING SCORE, and nothing may present it as one. It is a rough,
provisionally-tuned summary of how close a candidate's stored evidence
sits to a job's wording in vector space. It does not measure competence,
seniority, or suitability, and it must never be compared between
candidates or used to rank people.

WHAT IT DOES NOT TOUCH. `skill_match_v1` and `skill_gap_v1` are not
imported here, not recomputed, and not adjusted. `overall_score` is
produced by app/matching/score.py and is byte-identical whether or not
this module runs. A semantic hit NEVER marks a requirement satisfied:
`EvidenceHit` carries no skill id and no satisfied flag, so "this
evidence is relevant" cannot be turned into "this candidate has that
skill" by any code path here. Deterministic skill identity remains the
only thing that decides skill ownership.

WHY IT IS A SEPARATE ENDPOINT rather than a field on `/match`: a
semantic answer depends on a model that may be unconfigured, cold, or
slow, and none of that may degrade or delay the deterministic score.
Keeping them apart makes "the baseline is unchanged" structural.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings.retrieval import EvidenceHit, find_similar_evidence
from app.models.embedding import Embedding
from app.schemas.embedding import EmbeddingSourceType

FORMULA_VERSION = "semantic_fit_v1"

# How many pieces of evidence contribute. Small on purpose: the point is
# "here is the relevant evidence a person should read", and a list of
# twenty excerpts is not something anyone reads.
TOP_K = 5

# PROVISIONAL CALIBRATION — NOT VALIDATED.
#
# These two constants were derived from a SEVEN-SENTENCE hand-written
# fixture, measured against all-MiniLM-L6-v2:
#
#     related    "Built Kubernetes-based microservices"      0.456
#                "Designed CI/CD pipelines with Docker"      0.564
#                "Deployed services to AWS ECS"              0.194
#     unrelated  "Managed a coffee shop inventory"           0.124
#                "Taught piano lessons"                      0.098
#                "Wrote marketing copy for a bakery"        -0.015
#
# FLOOR sits in the gap between the highest unrelated (0.124) and the
# lowest related (0.194); CEIL near the top of the observed related
# range. Seven sentences is not a calibration set — it is barely an
# anecdote. These numbers are a starting point that makes the shape of
# the formula testable, NOT evidence that the thresholds are correct.
# Prompt 5.3 is where they get a real evaluation set; until then, treat
# any particular value of `fit` as indicative only.
SIMILARITY_FLOOR = 0.20
SIMILARITY_CEIL = 0.60

_MAX_FIT = 20

# Band cut-offs over the 0..20 fit. Coarse words rather than a precise
# number, because the underlying calibration does not justify precision.
_STRONG_MIN = 14
_MODERATE_MIN = 7


@dataclass(frozen=True)
class SemanticFit:
    """The semantic answer for one job, and the evidence behind it.

    `hits` is the whole justification: every number here is derived from
    those rows, and a reader can check the excerpts by eye. There is
    deliberately no skill list and no satisfied count.
    """

    formula_version: str
    fit: int
    band: str
    hits: list[EvidenceHit]
    model_identifier: str
    considered: int


def _band(fit: int) -> str:
    if fit >= _STRONG_MIN:
        return "strong"
    if fit >= _MODERATE_MIN:
        return "moderate"
    return "weak" if fit > 0 else "none"


def _centroid(vectors: list[list[float]]) -> list[float]:
    """Mean of the job's chunk vectors, re-normalised to unit length.

    One query vector instead of one per chunk, so retrieval stays a
    single call into the existing 5.2a path rather than a loop that
    would grow with description length.

    The cost is honest and worth stating: a description covering several
    unrelated topics averages into a point that represents none of them
    well. Per-chunk retrieval with per-chunk attribution is a refinement
    for a later slice; it is not needed to establish the shape here.
    """
    width = len(vectors[0])
    summed = [0.0] * width
    for vector in vectors:
        for index, component in enumerate(vector):
            summed[index] += component
    mean = [component / len(vectors) for component in summed]
    norm = sum(component * component for component in mean) ** 0.5
    scale = 1.0 / norm if norm else 1.0
    return [component * scale for component in mean]


async def compute_semantic_fit(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    saved_job_id: uuid.UUID,
    model_identifier: str,
) -> SemanticFit:
    """Relevant candidate evidence for one saved job.

    EMBEDS NOTHING. Both sides were embedded by the backfill, so this
    reads stored vectors only — no model is loaded and no provider is
    called in the request path. A job with no stored embeddings yet
    simply has nothing to compare against and returns an empty result,
    which is the truthful answer rather than an error.

    Two queries: the job's chunk vectors, then one retrieval.
    """
    chunk_rows = (
        await db.scalars(
            select(Embedding.embedding)
            .where(
                Embedding.user_id == user_id,
                Embedding.source_type == EmbeddingSourceType.SAVED_JOB_DESCRIPTION.value,
                Embedding.source_id == str(saved_job_id),
                Embedding.model_identifier == model_identifier,
            )
            .order_by(Embedding.chunk_index)
        )
    ).all()

    if not chunk_rows:
        return SemanticFit(
            formula_version=FORMULA_VERSION,
            fit=0,
            band="none",
            hits=[],
            model_identifier=model_identifier,
            considered=0,
        )

    hits = await find_similar_evidence(
        db,
        user_id=user_id,
        query_vector=_centroid([list(row) for row in chunk_rows]),
        model_identifier=model_identifier,
        limit=TOP_K,
        min_similarity=SIMILARITY_FLOOR,
    )

    if not hits:
        # No evidence cleared the floor. Reported as 0/"none", which says
        # "nothing relevant was found" — NOT "this candidate is a poor
        # match", a claim this module has no basis to make.
        return SemanticFit(
            formula_version=FORMULA_VERSION,
            fit=0,
            band="none",
            hits=[],
            model_identifier=model_identifier,
            considered=len(chunk_rows),
        )

    raw = sum(hit.hit.similarity for hit in hits) / len(hits)
    span = SIMILARITY_CEIL - SIMILARITY_FLOOR
    normalised = min(max((raw - SIMILARITY_FLOOR) / span, 0.0), 1.0)
    fit = round(normalised * _MAX_FIT)

    return SemanticFit(
        formula_version=FORMULA_VERSION,
        fit=fit,
        band=_band(fit),
        hits=hits,
        model_identifier=model_identifier,
        considered=len(chunk_rows),
    )
