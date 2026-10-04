"""Pure ranking helpers for hybrid tool retrieval.

Deliberately free of SQLAlchemy imports so the ranking logic can be unit tested
without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

RRF_K = 60
TIER_WEIGHT: dict[str, float] = {"core": 1.08, "generated": 1.0}


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K, weights: Sequence[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal rank fusion.

    ``rankings`` is an ordered list of candidate lists (best first) coming from
    independent retrievers (vector search, trigram keyword search, ...).  Returns
    ``[(key, score), ...]`` sorted by descending fused score.  Ties fall back to
    the best individual rank, then to the key itself, so the result is stable.
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must have the same length as rankings")
    if k <= 0:
        raise ValueError("k must be positive")

    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for weight, ranking in zip(weights, rankings, strict=True):
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + weight / (k + rank + 1)
            best_rank[key] = min(best_rank.get(key, rank), rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], best_rank[kv[0]], kv[0]))


def apply_tier_weight(scored: Iterable[tuple[str, float]], tiers: dict[str, str]) -> list[tuple[str, float]]:
    """Multiply each score by its tier weight (core tools win ties)."""
    out = [(key, score * TIER_WEIGHT.get(tiers.get(key, "generated"), 1.0)) for key, score in scored]
    out.sort(key=lambda kv: (-kv[1], kv[0]))
    return out


def truncate_keep_best(scored: Sequence[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    return list(scored[: max(0, k)])


def dedupe_keep_best(candidates: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
    """Keep the highest scoring entry per name, preserving order.

    ``candidates`` are ``(name, score)`` pairs; this is the last step before a
    search result is built, so the model never sees two versions of one tool.
    """
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for name, score in candidates:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, score))
    return out
