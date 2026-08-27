"""Grade semantic retrieval against the synthetic benchmark (Prompt 5.3).

Run it with:
    make evaluate-retrieval
(or: cd apps/api && uv run python -m scripts.evaluate_retrieval)

MEASURES, CHANGES NOTHING. It reads no database, writes no row, and
touches no threshold. `semantic_fit_v1`'s FLOOR, CEIL and TOP_K are
imported to report what the CURRENT system does — this script has no
opinion it can act on.

PURE-PYTHON COSINE, NOT A DATABASE ROUND TRIP. Prompt 5.2a already
proves pgvector's ordering is identical to cosine computed in Python
(tests/test_semantic_retrieval.py derives its expected ordering
independently and asserts the query matches). So going through Postgres
here would re-test that, slowly, while measuring the same thing. What is
under examination is the MODEL, and one DB-backed test in
tests/test_retrieval_evaluation.py keeps the equivalence honest rather
than assumed.

REPRODUCIBLE. The dataset is a frozen literal, the model is bit-exact on
repeat, and every metric is a pure function — so two runs on one machine
produce identical numbers, and the header stamps the model and dataset
version that produced them.
"""

from app.embeddings.provider import LocalSentenceEmbeddingProvider
from app.embeddings.semantic_fit import SIMILARITY_CEIL, SIMILARITY_FLOOR, TOP_K
from app.evaluation.dataset import BENCHMARK, DATASET_VERSION, Label, is_relevant, total_items
from app.evaluation.metrics import (
    RankedItem,
    duplicate_crowding_at_k,
    has_result,
    ndcg_at_k,
    precision_at_k,
    precision_ceiling_at_k,
    rank,
    recall_at_k,
    similarity_by_label,
    summarise,
)

_REPORTED_K = (3, 5)


def _ranked_cases(provider: LocalSentenceEmbeddingProvider) -> list[list[RankedItem]]:
    """Every case scored and ranked once, reused by every metric below."""
    cases: list[list[RankedItem]] = []
    for case in BENCHMARK:
        query = provider.embed_text_sync(case.query)
        cases.append(
            rank(
                [
                    RankedItem(
                        text=item.text,
                        label=item.label,
                        similarity=sum(
                            a * b for a, b in zip(query, provider.embed_text_sync(item.text))
                        ),
                        duplicate_group=item.duplicate_group,
                    )
                    for item in case.items
                ]
            )
        )
    return cases


def main() -> None:
    provider = LocalSentenceEmbeddingProvider()
    cases = _ranked_cases(provider)

    print("Semantic retrieval evaluation")
    print(f"  model           {provider.model_identifier}")
    print(f"  dataset         {DATASET_VERSION} — {len(BENCHMARK)} cases, {total_items()} items")
    print(f"  current config  FLOOR={SIMILARITY_FLOOR} CEIL={SIMILARITY_CEIL} TOP_K={TOP_K}")
    print("  NOT real-world hiring validation — developer-authored synthetic cases.")

    print("\nRanking quality")
    print(f"  {'K':>3}  {'P@K':>6} {'ceil':>6}  {'R@K':>6}  {'NDCG@K':>7}  {'coverage':>9}")
    for k in _REPORTED_K:
        precision = sum(precision_at_k(c, k) for c in cases) / len(cases)
        ceiling = sum(precision_ceiling_at_k(c, k) for c in cases) / len(cases)
        recall = sum(recall_at_k(c, k) for c in cases) / len(cases)
        ndcg = sum(ndcg_at_k(c, k) for c in cases) / len(cases)
        covered = sum(1 for c in cases if has_result(c, SIMILARITY_FLOOR, k))
        print(
            f"  {k:>3}  {precision:>6.3f} {ceiling:>6.3f}  {recall:>6.3f}  "
            f"{ndcg:>7.3f}  {covered:>4}/{len(cases):<4}"
        )

    print("\nSimilarity by label")
    pooled: dict[Label, list[float]] = {}
    for ranked_case in cases:
        for label, values in similarity_by_label(ranked_case).items():
            pooled.setdefault(label, []).extend(values)
    for label in Label:
        distribution = summarise(label, pooled[label])
        print(
            f"  {label.value:22} n={distribution.count:>2}  "
            f"min={distribution.minimum:+.3f}  med={distribution.median:+.3f}  "
            f"max={distribution.maximum:+.3f}"
        )

    print("\nThreshold behaviour at the current FLOOR")
    for label in Label:
        values = pooled[label]
        above = sum(1 for value in values if value >= SIMILARITY_FLOOR)
        verdict = "admitted (wanted)" if is_relevant(label) else "admitted (unwanted)"
        print(f"  {label.value:22} {above:>2}/{len(values):<2} {verdict}")
    reachable = sum(1 for value in pooled[Label.RELEVANT_PARAPHRASE] if value >= SIMILARITY_CEIL)
    print(
        f"  paraphrases reaching CEIL={SIMILARITY_CEIL}: "
        f"{reachable}/{len(pooled[Label.RELEVANT_PARAPHRASE])}"
    )

    print("\nDuplicate crowding")
    total_lost = 0
    for benchmark_case, ranked_case in zip(BENCHMARK, cases, strict=True):
        lost = duplicate_crowding_at_k(ranked_case, TOP_K)
        total_lost += lost
        if lost:
            print(f"  {lost} slot(s) lost at K={TOP_K}: {benchmark_case.query!r}")
    print(f"  total slots lost across {len(cases)} cases: {total_lost}")


if __name__ == "__main__":
    main()
