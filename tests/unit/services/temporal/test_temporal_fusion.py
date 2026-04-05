"""Tests for temporal RRF fusion (Story #633).

Covers:
- fuse_rrf_multi() with single and multi-provider inputs
- make_temporal_dedup_key() format
- TEMPORAL_OVERFETCH_MULTIPLIER constant value
- TemporalSearchResult fusion fields existence and defaults
"""

from code_indexer.services.temporal.temporal_fusion import (
    fuse_rrf_multi,
    make_temporal_dedup_key,
    TEMPORAL_OVERFETCH_MULTIPLIER,
    DEFAULT_RRF_K,
)
from code_indexer.services.temporal.temporal_search_service import TemporalSearchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_result(
    file_path: str,
    chunk_index: int,
    score: float,
    commit_hash: str = "abc123",
    content: str = "some content",
) -> TemporalSearchResult:
    """Build a minimal TemporalSearchResult for testing."""
    return TemporalSearchResult(
        file_path=file_path,
        chunk_index=chunk_index,
        content=content,
        score=score,
        metadata={},
        temporal_context={"commit_hash": commit_hash},
    )


# ---------------------------------------------------------------------------
# TemporalSearchResult fusion fields
# ---------------------------------------------------------------------------


def test_temporal_search_result_has_fusion_fields():
    """TemporalSearchResult must expose fusion fields with None defaults."""
    result = make_result("foo.py", 0, 0.9)
    assert hasattr(result, "temporal_chunk_id")
    assert hasattr(result, "source_provider")
    assert hasattr(result, "fusion_score")
    assert hasattr(result, "contributing_providers")
    assert result.temporal_chunk_id is None
    assert result.source_provider is None
    assert result.fusion_score is None
    assert result.contributing_providers is None


# ---------------------------------------------------------------------------
# TEMPORAL_OVERFETCH_MULTIPLIER
# ---------------------------------------------------------------------------


def test_overfetch_multiplier_constant():
    """TEMPORAL_OVERFETCH_MULTIPLIER must equal 3."""
    assert TEMPORAL_OVERFETCH_MULTIPLIER == 3


# ---------------------------------------------------------------------------
# make_temporal_dedup_key
# ---------------------------------------------------------------------------


def test_make_temporal_dedup_key_format():
    """Dedup key must be '{commit_hash}:{file_path}:{chunk_index}'."""
    result = make_result("src/auth.py", 2, 0.8, commit_hash="deadbeef")
    key = make_temporal_dedup_key(result)
    assert key == "deadbeef:src/auth.py:2"


def test_make_temporal_dedup_key_missing_commit_hash():
    """Dedup key with missing commit_hash falls back to empty string."""
    result = TemporalSearchResult(
        file_path="foo.py",
        chunk_index=0,
        content="",
        score=0.5,
        metadata={},
        temporal_context={},  # no commit_hash
    )
    key = make_temporal_dedup_key(result)
    assert key == ":foo.py:0"


# ---------------------------------------------------------------------------
# fuse_rrf_multi — empty input
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_empty_input():
    """Empty provider dict must return empty list."""
    result = fuse_rrf_multi({}, dedup_key=make_temporal_dedup_key, limit=10)
    assert result == []


# ---------------------------------------------------------------------------
# fuse_rrf_multi — single provider pass-through
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_single_provider_passthrough():
    """Single provider: results returned with attribution, no fusion math."""
    r1 = make_result("a.py", 0, 0.9)
    r2 = make_result("b.py", 0, 0.7)
    results = fuse_rrf_multi(
        {"voyage": [r1, r2]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
    )
    assert len(results) == 2
    for r in results:
        assert r.source_provider == "voyage"
        assert r.contributing_providers == ["voyage"]
        assert r.fusion_score is not None


# ---------------------------------------------------------------------------
# fuse_rrf_multi — two providers, same chunk
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_two_providers_merge_same_chunk():
    """Same commit+file+chunk from two providers must produce exactly one result."""
    r_voyage = make_result("auth.py", 0, 0.9, commit_hash="abc")
    r_openai = make_result("auth.py", 0, 0.8, commit_hash="abc")

    results = fuse_rrf_multi(
        {"voyage": [r_voyage], "openai": [r_openai]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
    )
    assert len(results) == 1
    assert results[0].file_path == "auth.py"


# ---------------------------------------------------------------------------
# fuse_rrf_multi — two providers, different chunks
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_two_providers_different_chunks():
    """Different chunks from two providers must produce separate results."""
    r1 = make_result("a.py", 0, 0.9, commit_hash="abc")
    r2 = make_result("b.py", 1, 0.8, commit_hash="abc")

    results = fuse_rrf_multi(
        {"voyage": [r1], "openai": [r2]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
    )
    assert len(results) == 2
    paths = {r.file_path for r in results}
    assert paths == {"a.py", "b.py"}


# ---------------------------------------------------------------------------
# fuse_rrf_multi — RRF score computation
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_rrf_score_computation():
    """RRF score for rank-0 item must be 1/(k+1)."""
    r = make_result("x.py", 0, 0.9, commit_hash="c1")
    results = fuse_rrf_multi(
        {"voyage": [r]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
        k=DEFAULT_RRF_K,
    )
    # Single provider: fusion_score == score (pass-through), but let's check math
    # For single provider the fusion_score is set to the raw score.
    # For the two-provider case we can verify the formula independently.
    assert results[0].fusion_score is not None

    # Two-provider: same chunk at rank 0 in both lists.
    # Expected RRF = 1/(60+0+1) + 1/(60+0+1) = 2/61
    r_v = make_result("y.py", 0, 0.9, commit_hash="c2")
    r_o = make_result("y.py", 0, 0.8, commit_hash="c2")
    results2 = fuse_rrf_multi(
        {"voyage": [r_v], "openai": [r_o]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
        k=60,
    )
    assert len(results2) == 1
    expected = 1.0 / (60 + 0 + 1) + 1.0 / (60 + 0 + 1)
    assert abs(results2[0].fusion_score - expected) < 1e-9


# ---------------------------------------------------------------------------
# fuse_rrf_multi — source_provider tracks highest score
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_source_provider_is_highest_score():
    """source_provider must be the provider with the highest individual score."""
    r_voyage = make_result("a.py", 0, 0.6, commit_hash="h1")
    r_openai = make_result("a.py", 0, 0.95, commit_hash="h1")

    results = fuse_rrf_multi(
        {"voyage": [r_voyage], "openai": [r_openai]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
    )
    assert len(results) == 1
    assert results[0].source_provider == "openai"


# ---------------------------------------------------------------------------
# fuse_rrf_multi — contributing_providers lists both providers
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_contributing_providers_both_listed():
    """contributing_providers must include all providers that had the chunk."""
    r_voyage = make_result("a.py", 0, 0.9, commit_hash="h2")
    r_openai = make_result("a.py", 0, 0.8, commit_hash="h2")

    results = fuse_rrf_multi(
        {"voyage": [r_voyage], "openai": [r_openai]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
    )
    assert len(results) == 1
    contributors = set(results[0].contributing_providers)
    assert contributors == {"voyage", "openai"}


# ---------------------------------------------------------------------------
# fuse_rrf_multi — respects limit
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_respects_limit():
    """fuse_rrf_multi must return at most `limit` results."""
    provider_results = [
        make_result(f"file{i}.py", 0, 1.0 - i * 0.05, commit_hash=f"commit{i}")
        for i in range(20)
    ]
    results = fuse_rrf_multi(
        {"voyage": provider_results},
        dedup_key=make_temporal_dedup_key,
        limit=5,
    )
    assert len(results) == 5


# ---------------------------------------------------------------------------
# fuse_rrf_multi — sorted by fusion_score descending
# ---------------------------------------------------------------------------


def test_fuse_rrf_multi_sorted_by_fusion_score():
    """Results must be ordered by fusion_score descending."""
    # Two providers with the same chunks at different ranks:
    # rank 0 in both → highest RRF
    # rank 1 in both → lower RRF
    r_v_0 = make_result("top.py", 0, 0.95, commit_hash="c0")
    r_v_1 = make_result("mid.py", 0, 0.80, commit_hash="c1")
    r_o_0 = make_result("top.py", 0, 0.90, commit_hash="c0")
    r_o_1 = make_result("mid.py", 0, 0.75, commit_hash="c1")

    results = fuse_rrf_multi(
        {"voyage": [r_v_0, r_v_1], "openai": [r_o_0, r_o_1]},
        dedup_key=make_temporal_dedup_key,
        limit=10,
    )
    assert len(results) == 2
    assert results[0].file_path == "top.py"
    assert results[1].file_path == "mid.py"
    assert results[0].fusion_score >= results[1].fusion_score
