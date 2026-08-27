"""Ranking-quality metrics for the retrieval benchmark (Prompt 5.3).

PURE: ranked results in, numbers out. No model, no database, no I/O —
so every metric is checkable by hand against a worked example, which is
what the unit tests do. Nothing in this module is used by production
code paths; it exists to measure them.

BINARY RELEVANCE. `relevant_paraphrase` and `relevant_lexical` count as
relevant; `lexical_distractor` and `unrelated` do not. Graded relevance
would need a judgement about how much MORE relevant a lexical match is
than a paraphrase, and inventing that scale is exactly the kind of
unjustified precision this slice is trying to avoid.
"""

import math
from dataclasses import dataclass

from app.evaluation.dataset import Label, is_relevant


@dataclass(frozen=True)
class RankedItem:
    """One retrieved item, with the label it was authored under and the
    similarity that ranked it."""

    text: str
    label: Label
    similarity: float
    duplicate_group: str | None = None


def rank(items: list[RankedItem]) -> list[RankedItem]:
    """Highest similarity first, ties broken by text.

    The tie-break keeps the ordering total, so the same dataset produces
    the same report on every run rather than depending on how Python
    happened to order equal values.
    """
    return sorted(items, key=lambda item: (-item.similarity, item.text))


def precision_at_k(ranked: list[RankedItem], k: int) -> float:
    """Share of the top k that is relevant.

    READ THIS WITH `precision_ceiling_at_k`. A case with two relevant
    items can never exceed 0.4 at k=5, so a low value here is often a
    fact about the dataset rather than about the retrieval.
    """
    if k <= 0:
        return 0.0
    return sum(1 for item in ranked[:k] if is_relevant(item.label)) / k


def precision_ceiling_at_k(ranked: list[RankedItem], k: int) -> float:
    """The best precision@k this case could possibly achieve."""
    if k <= 0:
        return 0.0
    return min(sum(1 for item in ranked if is_relevant(item.label)), k) / k


def recall_at_k(ranked: list[RankedItem], k: int) -> float:
    """Share of all relevant items that made the top k.

    The metric that actually matters here: the product shows a handful
    of excerpts, so "did the relevant evidence surface at all" is the
    question, not "was the list pure".
    """
    relevant = sum(1 for item in ranked if is_relevant(item.label))
    if not relevant:
        return 0.0
    return sum(1 for item in ranked[:k] if is_relevant(item.label)) / relevant


def ndcg_at_k(ranked: list[RankedItem], k: int) -> float:
    """Normalised discounted cumulative gain, binary gains.

    Rewards putting relevant items EARLY, not merely inside the window —
    the one metric here that notices a distractor ranked above a genuine
    paraphrase. 1.0 means the ideal ordering; 0.0 means nothing relevant
    in the window.
    """
    gains = [1.0 if is_relevant(item.label) else 0.0 for item in ranked]
    actual = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains[:k]))
    ideal = sum(
        gain / math.log2(index + 2) for index, gain in enumerate(sorted(gains, reverse=True)[:k])
    )
    return actual / ideal if ideal else 0.0


def has_result(ranked: list[RankedItem], floor: float, k: int) -> bool:
    """Whether anything in the top k clears the similarity floor.

    Drives the coverage / no-result rate: a case where nothing clears
    the floor shows the user "no closely related evidence found", which
    is a legitimate answer but a bad one to give too often.
    """
    return any(item.similarity >= floor for item in ranked[:k])


def duplicate_crowding_at_k(ranked: list[RankedItem], k: int) -> int:
    """How many of the top k slots are repeats of a group already shown.

    Near-duplicate evidence is common in practice — the same project
    described in a resume and again in a README. Each repeat costs a
    slot that could have shown something new, so this counts the slots
    lost rather than the duplicates present: three items from one group
    in the top k is two lost slots, not three.
    """
    seen: set[str] = set()
    crowded = 0
    for item in ranked[:k]:
        if item.duplicate_group is None:
            continue
        if item.duplicate_group in seen:
            crowded += 1
        else:
            seen.add(item.duplicate_group)
    return crowded


def similarity_by_label(ranked: list[RankedItem]) -> dict[Label, list[float]]:
    """Every similarity seen, grouped by the label it was authored
    under. The diagnostic behind any threshold argument."""
    grouped: dict[Label, list[float]] = {}
    for item in ranked:
        grouped.setdefault(item.label, []).append(item.similarity)
    return grouped


@dataclass(frozen=True)
class Distribution:
    """Min / median / max for one label's similarities."""

    label: Label
    count: int
    minimum: float
    median: float
    maximum: float


def summarise(label: Label, values: list[float]) -> Distribution:
    ordered = sorted(values)
    return Distribution(
        label=label,
        count=len(ordered),
        minimum=ordered[0],
        # Lower median on an even count — deliberately not interpolated,
        # so every number in the report is a value actually observed.
        median=ordered[(len(ordered) - 1) // 2],
        maximum=ordered[-1],
    )
