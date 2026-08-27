"""Grade semantic retrieval against the synthetic benchmark (Prompt 5.3).

Three modes:
    make evaluate-retrieval   print the report
    make evaluate-check       compare against the baseline, exit 1 on regression
    make evaluate-baseline    rewrite the baseline (an intentional change)

ALL THREE NEED THE REAL MODEL, so all three are local. CI enforces only
the model-independent half — fingerprints and config — via
tests/test_evaluation_policy.py. A change to app/embeddings/retrieval.py
alters ranking WITHOUT changing any fingerprint, so a green CI run says
nothing about it: run `make evaluate-check` locally before merging one.

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

import sys
from pathlib import Path

from app.embeddings.provider import LocalSentenceEmbeddingProvider
from app.embeddings.semantic_fit import SIMILARITY_CEIL, SIMILARITY_FLOOR, TOP_K
from app.evaluation.dataset import BENCHMARK, DATASET_VERSION, Label, is_relevant, total_items
from app.evaluation.metrics import (
    RankedItem,
    duplicate_crowding_at_k,
    rank,
    similarity_by_label,
    summarise,
)
from app.evaluation.policy import compare, describe
from app.evaluation.report import (
    EvaluationReport,
    build_report,
    from_json,
    to_json,
)

_REPORTED_K = (3, 5)


BASELINE_PATH = Path(__file__).resolve().parents[1] / "app" / "evaluation" / "baseline.json"


def _ranked_cases(
    provider: LocalSentenceEmbeddingProvider,
) -> list[tuple[str, list[RankedItem]]]:
    """Every case scored and ranked once, paired with its query so
    per-case metrics can be keyed by it."""
    cases: list[tuple[str, list[RankedItem]]] = []
    for case in BENCHMARK:
        query = provider.embed_text_sync(case.query)
        cases.append(
            (
                case.query,
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
                ),
            )
        )
    return cases


def _current_report() -> tuple[EvaluationReport, list[list[RankedItem]]]:
    provider = LocalSentenceEmbeddingProvider()
    ranked = _ranked_cases(provider)
    report = build_report(ranked, model_identifier=provider.model_identifier)
    return report, [case for _, case in ranked]


def _print_report(report: EvaluationReport, cases: list[list[RankedItem]]) -> None:
    print("Semantic retrieval evaluation")
    print(f"  model           {report.model_identifier}")
    print(f"  dataset         {DATASET_VERSION} — {len(BENCHMARK)} cases, {total_items()} items")
    print(f"  current config  FLOOR={SIMILARITY_FLOOR} CEIL={SIMILARITY_CEIL} TOP_K={TOP_K}")
    print("  NOT real-world hiring validation — developer-authored synthetic cases.")

    print("\nRanking quality")
    print(f"  {'K':>3}  {'P@K':>6} {'ceil':>6}  {'R@K':>6}  {'NDCG@K':>7}  {'coverage':>9}")
    for k in _REPORTED_K:
        metric = report.metrics[k]
        covered = round(metric.coverage * len(cases))
        print(
            f"  {k:>3}  {metric.precision:>6.3f} {metric.precision_ceiling:>6.3f}  "
            f"{metric.recall:>6.3f}  {metric.ndcg:>7.3f}  {covered:>4}/{len(cases):<4}"
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


def main() -> None:
    """Default prints the report; --check compares; --update-baseline
    rewrites it."""
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    report, cases = _current_report()

    if mode == "--update-baseline":
        BASELINE_PATH.write_text(to_json(report))
        print(f"baseline written: {BASELINE_PATH}")
        print("Commit this IN THE SAME PR as the change that caused the movement —")
        print("a standalone baseline update is indistinguishable from accepting a regression.")
        return

    if mode == "--check":
        if not BASELINE_PATH.exists():
            print(f"no baseline at {BASELINE_PATH} — run `make evaluate-baseline`")
            sys.exit(1)
        result = compare(from_json(BASELINE_PATH.read_text()), report)
        print(describe(result))
        sys.exit(0 if result.ok else 1)

    _print_report(report, cases)


if __name__ == "__main__":
    main()
