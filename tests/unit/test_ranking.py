"""Reciprocal rank fusion and tier weighting for hybrid tool retrieval."""

from __future__ import annotations

import pytest

from agent.registry.ranking import (
    RRF_K,
    apply_tier_weight,
    dedupe_keep_best,
    rrf_fuse,
    truncate_keep_best,
)


def test_fuse_prefers_items_ranked_high_by_multiple_retrievers():
    vector = ["alpha", "beta", "gamma"]
    keyword = ["beta", "alpha", "delta"]
    fused = rrf_fuse([vector, keyword])
    order = [name for name, _ in fused]
    assert order[:2] == ["alpha", "beta"]
    assert set(order) == {"alpha", "beta", "gamma", "delta"}


def test_fuse_with_a_single_retriever_preserves_order():
    fused = rrf_fuse([["a", "b", "c"]])
    assert [name for name, _ in fused] == ["a", "b", "c"]


def test_fuse_is_deterministic_for_ties():
    first = rrf_fuse([["a", "b"], ["b", "a"]])
    second = rrf_fuse([["a", "b"], ["b", "a"]])
    assert first == second
    assert {name for name, _ in first} == {"a", "b"}


def test_scores_follow_the_rrf_formula():
    fused = dict(rrf_fuse([["a", "b"]], k=10))
    assert fused["a"] == pytest.approx(1 / (10 + 1))
    assert fused["b"] == pytest.approx(1 / (10 + 2))
    assert RRF_K == 60


def test_weights_shift_the_balance():
    fused = dict(rrf_fuse([["a"], ["b"]], weights=[3.0, 1.0]))
    assert fused["a"] > fused["b"]


def test_weight_length_must_match():
    with pytest.raises(ValueError):
        rrf_fuse([["a"], ["b"]], weights=[1.0])


def test_k_must_be_positive():
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=0)


def test_tier_weight_favours_core_tools_on_a_tie():
    fused = [("generated_tool", 0.02), ("fs.read", 0.02)]
    weighted = apply_tier_weight(fused, {"fs.read": "core", "generated_tool": "generated"})
    assert weighted[0][0] == "fs.read"


def test_tier_weight_keeps_unknown_tiers_neutral():
    weighted = apply_tier_weight([("a", 1.0)], {})
    assert weighted[0][1] == pytest.approx(1.0)


def test_dedupe_keeps_first_occurrence_only():
    scored = [("alpha", 3.0), ("beta", 2.0), ("alpha", 1.0)]
    assert dedupe_keep_best(scored) == [("alpha", 3.0), ("beta", 2.0)]


def test_truncate_keep_best_respects_k():
    scored = [("a", 3.0), ("b", 2.0), ("c", 1.0)]
    assert truncate_keep_best(scored, 2) == [("a", 3.0), ("b", 2.0)]
    assert truncate_keep_best(scored, 0) == []
