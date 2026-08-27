"""The retrieval evaluation harness (Prompt 5.3).

THREE GROUPS, WITH DIFFERENT COSTS AND GUARANTEES:

  * Metric arithmetic — hand-computed expected values, no model, no
    database. These are what make the reported numbers trustworthy: a
    metric nobody has checked by hand is just a number.
  * Dataset invariants — the benchmark is fictional, covers all four
    labels, and actually contains the near-duplicates the crowding
    metric claims to measure.
  * One DB-backed equivalence test, and one guarded real-model
    regression check.

The regression bounds are deliberately LOOSE. They exist to catch a
flipped distance sign or a broken provider, not to pin exact scores —
a tight bound on a 12-case synthetic set would fail on an innocuous
model patch and teach everyone to ignore it.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.embeddings.content import EmbeddingDocument
from app.embeddings.provider import LocalSentenceEmbeddingProvider, MockEmbeddingProvider
from app.embeddings.retrieval import find_similar
from app.embeddings.store import upsert_embeddings
from app.evaluation.dataset import (
    BENCHMARK,
    DATASET_VERSION,
    Label,
    is_relevant,
    total_items,
)
from app.evaluation.metrics import (
    RankedItem,
    duplicate_crowding_at_k,
    has_result,
    ndcg_at_k,
    precision_at_k,
    precision_ceiling_at_k,
    rank,
    recall_at_k,
    summarise,
)
from app.models.embedding import EMBEDDING_DIMENSION
from app.models.user import User
from app.schemas.embedding import EmbeddingSourceType
from app.settings import get_settings
from tests.conftest import _SEARCH_PATH_CONNECT_ARGS

_MODEL = "mock-deterministic-v1"


def _item(similarity: float, label: Label, text: str = "", group: str | None = None) -> RankedItem:
    return RankedItem(
        text=text or f"item-{similarity}", label=label, similarity=similarity, duplicate_group=group
    )


_REL = Label.RELEVANT_PARAPHRASE
_IRR = Label.UNRELATED


# --- metric arithmetic, checked by hand --------------------------------


def test_precision_and_its_ceiling() -> None:
    """Two relevant items in the top 3 of a 4-item list -> 2/3. The
    ceiling says the best achievable here is also 2/3, so 0.667 is a
    perfect result rather than a mediocre one."""
    ranked = [_item(0.9, _REL), _item(0.8, _IRR), _item(0.7, _REL), _item(0.1, _IRR)]

    assert precision_at_k(ranked, 3) == pytest.approx(2 / 3)
    assert precision_ceiling_at_k(ranked, 3) == pytest.approx(2 / 3)
    assert precision_at_k(ranked, 0) == 0.0


def test_recall_counts_against_all_relevant_not_the_window() -> None:
    ranked = [_item(0.9, _REL), _item(0.8, _IRR), _item(0.7, _REL)]

    assert recall_at_k(ranked, 1) == pytest.approx(0.5)
    assert recall_at_k(ranked, 3) == pytest.approx(1.0)
    assert recall_at_k([_item(0.5, _IRR)], 3) == 0.0, "no relevant items -> 0, not a crash"


def test_ndcg_rewards_ranking_relevant_items_earlier() -> None:
    """Ideal ordering scores 1.0. Demoting the relevant item to second
    costs exactly 1/log2(3) over the ideal 1/log2(2)."""
    ideal = [_item(0.9, _REL), _item(0.8, _IRR)]
    demoted = [_item(0.9, _IRR), _item(0.8, _REL)]

    assert ndcg_at_k(ideal, 2) == pytest.approx(1.0)
    assert ndcg_at_k(demoted, 2) == pytest.approx((1 / 1.5849625007) / 1.0, rel=1e-6)
    assert ndcg_at_k([_item(0.5, _IRR)], 2) == 0.0


def test_coverage_is_about_the_floor_not_relevance() -> None:
    """An unrelated item above the floor still counts as a result — the
    user sees something. Coverage measures "did we show anything", which
    is a different question from "was it right"."""
    assert has_result([_item(0.30, _IRR)], floor=0.20, k=5) is True
    assert has_result([_item(0.10, _REL)], floor=0.20, k=5) is False


def test_duplicate_crowding_counts_lost_slots_not_duplicates() -> None:
    """Three items from one group in the window is TWO lost slots: the
    first is the legitimate result, the other two are the cost."""
    ranked = [
        _item(0.9, _REL, "a", "g1"),
        _item(0.8, _REL, "b", "g1"),
        _item(0.7, _REL, "c", "g1"),
        _item(0.6, _REL, "d", None),
    ]

    assert duplicate_crowding_at_k(ranked, 4) == 2
    assert duplicate_crowding_at_k(ranked, 1) == 0


def test_rank_is_total_so_reports_are_reproducible() -> None:
    """Equal similarities must not order differently between runs."""
    tied = [_item(0.5, _REL, "b"), _item(0.5, _REL, "a")]

    assert [row.text for row in rank(tied)] == ["a", "b"]


def test_summarise_reports_observed_values_only() -> None:
    distribution = summarise(_REL, [0.4, 0.1, 0.3, 0.2])

    assert (distribution.minimum, distribution.median, distribution.maximum) == (0.1, 0.2, 0.4)
    assert distribution.count == 4


# --- dataset invariants -------------------------------------------------


def test_the_benchmark_is_the_documented_size_and_shape() -> None:
    labels = {item.label for case in BENCHMARK for item in case.items}

    assert DATASET_VERSION == "retrieval-eval-v1"
    assert len(BENCHMARK) == 12
    assert total_items() == 56
    assert labels == set(Label), "every label must appear, or a metric measures nothing"


def test_every_case_has_both_relevant_and_irrelevant_evidence() -> None:
    """A case with only relevant items cannot distinguish a working
    ranker from one that returns everything."""
    for case in BENCHMARK:
        labels = [item.label for item in case.items]
        assert any(is_relevant(label) for label in labels), case.query
        assert any(not is_relevant(label) for label in labels), case.query


def test_the_benchmark_contains_near_duplicates_to_measure() -> None:
    groups = [item.duplicate_group for case in BENCHMARK for item in case.items]
    populated = [group for group in groups if group is not None]

    assert len(populated) > len(set(populated)), "duplicate groups must actually repeat"


def test_the_benchmark_is_fictional() -> None:
    """Same guard as tests/test_mvp_acceptance.py: a real person's
    details pasted in here must fail the suite rather than ship."""
    corpus = " ".join(
        [case.query for case in BENCHMARK]
        + [item.text for case in BENCHMARK for item in case.items]
    ).lower()

    for marker in ("@", "http://", "https://", "linkedin", "github.com/", "+1", "gmail"):
        assert marker not in corpus, f"benchmark must contain no {marker!r}"


# --- pgvector and Python must agree ------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_pgvector_ranks_a_benchmark_case_exactly_as_python_does(db: AsyncSession) -> None:
    """The one DB-backed test, and the assumption the whole harness
    rests on: evaluating with Python cosine is only valid because
    pgvector orders identically.

    Uses the MOCK provider deliberately — the claim under test is about
    the retrieval mechanism, not the model, so this runs everywhere with
    no download and no skip.
    """
    provider = MockEmbeddingProvider(dimension=EMBEDDING_DIMENSION, model_identifier=_MODEL)
    case = BENCHMARK[0]

    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()

    by_source = {str(uuid.uuid4()): item.text for item in case.items}
    await upsert_embeddings(
        db,
        provider,
        [
            EmbeddingDocument(
                user_id=user_id,
                source_type=EmbeddingSourceType.SKILL_EVIDENCE.value,
                source_id=source_id,
                chunk_index=0,
                text=text,
            )
            for source_id, text in by_source.items()
        ],
    )
    await db.commit()

    query = provider.embed_text_sync(case.query)
    expected = [
        source_id
        for source_id, _ in sorted(
            (
                (
                    source_id,
                    sum(a * b for a, b in zip(query, provider.embed_text_sync(text), strict=True)),
                )
                for source_id, text in by_source.items()
            ),
            key=lambda pair: -pair[1],
        )
    ]

    hits = await find_similar(
        db,
        user_id=user_id,
        query_vector=query,
        model_identifier=_MODEL,
        limit=len(by_source),
    )

    assert [hit.source_id for hit in hits] == expected


# --- the real model, guarded -------------------------------------------


def _real_provider() -> LocalSentenceEmbeddingProvider:
    provider = LocalSentenceEmbeddingProvider()
    try:
        provider.embed_text_sync("warm")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"local embedding model unavailable: {exc}")
    return provider


def test_the_benchmark_still_ranks_relevant_evidence_first() -> None:
    """A regression guard, not a quality bar. Loose bounds on purpose:
    tight ones on 12 synthetic cases would break on a harmless model
    update and train everyone to skip the failure."""
    provider = _real_provider()
    cache: dict[str, list[float]] = {}

    def embed(text: str) -> list[float]:
        if text not in cache:
            cache[text] = provider.embed_text_sync(text)
        return cache[text]

    ndcgs, recalls = [], []
    for case in BENCHMARK:
        query = embed(case.query)
        ranked = rank(
            [
                RankedItem(
                    text=item.text,
                    label=item.label,
                    similarity=sum(a * b for a, b in zip(query, embed(item.text), strict=True)),
                    duplicate_group=item.duplicate_group,
                )
                for item in case.items
            ]
        )
        ndcgs.append(ndcg_at_k(ranked, 5))
        # RECALL AT 3, NOT 5. Most cases hold four to six items, so a
        # top-5 window captures nearly everything however it is ordered:
        # measured against random orderings, Recall@5 scores 0.973 and
        # even a worst-case ranker scores 0.951, so a bound there would
        # pass no matter how badly retrieval broke. Recall@3 discriminates
        # — random scores 0.656 against the real system's 0.896.
        recalls.append(recall_at_k(ranked, 3))

    mean_ndcg = sum(ndcgs) / len(ndcgs)
    mean_recall = sum(recalls) / len(recalls)

    # Both bounds sit between measured-random and measured-actual:
    # NDCG@5 random 0.792 / actual 0.972; Recall@3 random 0.656 /
    # actual 0.896. A ranker no better than chance fails both.
    assert mean_ndcg >= 0.85, f"NDCG@5 regressed to {mean_ndcg:.3f}"
    assert mean_recall >= 0.75, f"Recall@3 regressed to {mean_recall:.3f}"
