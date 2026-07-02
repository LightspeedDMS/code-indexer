"""Tests for query strategy and score fusion (Story #488)."""

import pytest

from code_indexer.services.query_strategy import (
    QueryStrategy,
    ScoreFusion,
    QueryResult,
    RRF_K,
    fuse_rrf,
    fuse_multiply,
    fuse_average,
    execute_parallel_query,
    execute_failover_query,
)


def _make_result(file_path, score, provider="primary", chunk_id="0"):
    return QueryResult(
        file_path=file_path,
        score=score,
        chunk_id=chunk_id,
        source_provider=provider,
    )


class TestRRFFusion:
    def test_rrf_basic(self):
        primary = [_make_result("a.py", 0.9), _make_result("b.py", 0.8)]
        secondary = [_make_result("b.py", 0.95), _make_result("c.py", 0.7)]
        fused = fuse_rrf(primary, secondary, limit=10)
        # b.py appears in both → highest RRF score
        assert fused[0].file_path == "b.py"

    def test_rrf_single_provider(self):
        primary = [_make_result("a.py", 0.9)]
        fused = fuse_rrf(primary, [], limit=10)
        assert len(fused) == 1
        assert fused[0].file_path == "a.py"

    def test_rrf_respects_limit(self):
        primary = [_make_result(f"{i}.py", 0.5) for i in range(20)]
        fused = fuse_rrf(primary, [], limit=5)
        assert len(fused) == 5


class TestMultiplyFusion:
    def test_multiply_overlapping(self):
        primary = [_make_result("a.py", 0.9), _make_result("b.py", 0.5)]
        secondary = [_make_result("a.py", 0.8), _make_result("b.py", 0.9)]
        fused = fuse_multiply(primary, secondary, limit=10)
        assert len(fused) == 2

    def test_multiply_missing_uses_neutral(self):
        primary = [_make_result("a.py", 0.9)]
        secondary = [_make_result("b.py", 0.8)]
        fused = fuse_multiply(primary, secondary, limit=10)
        assert len(fused) == 2


class TestAverageFusion:
    def test_average_both_providers(self):
        primary = [_make_result("a.py", 0.8)]
        secondary = [_make_result("a.py", 0.6)]
        fused = fuse_average(primary, secondary, limit=10)
        assert len(fused) == 1
        # Both normalized to 1.0 (single result each), average = 1.0
        assert fused[0].score == 1.0

    def test_average_single_provider(self):
        primary = [_make_result("a.py", 0.9)]
        fused = fuse_average(primary, [], limit=10)
        assert len(fused) == 1


class TestParallelQuery:
    def test_both_succeed(self):
        def primary():
            return [_make_result("a.py", 0.9)]

        def secondary():
            return [_make_result("b.py", 0.8)]

        results = execute_parallel_query(primary, secondary)
        assert len(results) == 2

    def test_secondary_fails(self):
        def primary():
            return [_make_result("a.py", 0.9)]

        def secondary():
            raise RuntimeError("down")

        results = execute_parallel_query(primary, secondary)
        assert len(results) == 1
        assert results[0].file_path == "a.py"


class TestFailoverQuery:
    def test_primary_succeeds(self):
        def primary():
            return [_make_result("a.py", 0.9)]

        def secondary():
            return [_make_result("b.py", 0.8)]

        results = execute_failover_query(primary, secondary)
        assert results[0].file_path == "a.py"

    def test_primary_fails_uses_secondary(self):
        def primary():
            raise RuntimeError("down")

        def secondary():
            return [_make_result("b.py", 0.8)]

        results = execute_failover_query(primary, secondary)
        assert results[0].file_path == "b.py"


class TestEnums:
    def test_strategy_values(self):
        assert QueryStrategy.PRIMARY_ONLY.value == "primary_only"
        assert QueryStrategy.FAILOVER.value == "failover"
        assert QueryStrategy.PARALLEL.value == "parallel"
        assert QueryStrategy.SPECIFIC.value == "specific"

    def test_fusion_values(self):
        assert ScoreFusion.RRF.value == "rrf"
        assert ScoreFusion.MULTIPLY.value == "multiply"
        assert ScoreFusion.AVERAGE.value == "average"


class TestRRFFusionScoreProvenance:
    """Tests for fusion_score and contributing_providers fields on fuse_rrf output."""

    def test_rrf_both_providers_result_has_fusion_score(self):
        """fuse_rrf result found in both providers must have non-None fusion_score."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_rrf(primary, secondary, limit=10)
        assert fused[0].fusion_score is not None

    def test_rrf_both_providers_result_has_contributing_providers(self):
        """fuse_rrf result found in both providers must list both providers."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_rrf(primary, secondary, limit=10)
        assert set(fused[0].contributing_providers) == {"cohere", "voyage-ai"}

    def test_rrf_primary_only_result_has_single_contributing_provider(self):
        """fuse_rrf result found only in primary must list only primary provider."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("b.py", 0.8, provider="cohere")]
        fused = fuse_rrf(primary, secondary, limit=10)
        a_result = next(r for r in fused if r.file_path == "a.py")
        assert a_result.contributing_providers == ["voyage-ai"]
        assert a_result.fusion_score is not None
        expected = 1.0 / (RRF_K + 1)
        assert abs(a_result.fusion_score - expected) < 1e-10

    def test_rrf_secondary_only_result_has_single_contributing_provider(self):
        """fuse_rrf result found only in secondary must list only secondary provider."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("b.py", 0.8, provider="cohere")]
        fused = fuse_rrf(primary, secondary, limit=10)
        b_result = next(r for r in fused if r.file_path == "b.py")
        assert b_result.contributing_providers == ["cohere"]
        assert b_result.fusion_score is not None
        expected = 1.0 / (RRF_K + 1)
        assert abs(b_result.fusion_score - expected) < 1e-10

    def test_rrf_fusion_score_matches_rrf_formula_both_providers(self):
        """fuse_rrf result in both providers has fusion_score = 2 * 1/(RRF_K+1)."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_rrf(primary, secondary, limit=10)
        expected = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1)
        assert abs(fused[0].fusion_score - expected) < 1e-10

    def test_rrf_source_provider_remains_fused(self):
        """fuse_rrf source_provider is 'fused' even with contributing_providers set."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_rrf(primary, secondary, limit=10)
        assert fused[0].source_provider == "fused"


@pytest.mark.parametrize("fuse_fn", [fuse_multiply, fuse_average])
class TestNonRRFFusionScoreProvenance:
    """Tests for fusion_score, contributing_providers, and source_provider on fuse_multiply and fuse_average."""

    def test_result_has_fusion_score(self, fuse_fn):
        """Result must have non-None fusion_score."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_fn(primary, secondary, limit=10)
        assert fused[0].fusion_score is not None

    def test_both_providers_result_has_contributing_providers(self, fuse_fn):
        """Result found in both providers must list both providers."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_fn(primary, secondary, limit=10)
        assert set(fused[0].contributing_providers) == {"cohere", "voyage-ai"}

    def test_primary_only_result_has_single_contributing_provider(self, fuse_fn):
        """Result found only in primary must list only primary provider."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("b.py", 0.8, provider="cohere")]
        fused = fuse_fn(primary, secondary, limit=10)
        a_result = next(r for r in fused if r.file_path == "a.py")
        assert a_result.contributing_providers == ["voyage-ai"]
        assert a_result.fusion_score is not None

    def test_secondary_only_result_has_single_contributing_provider(self, fuse_fn):
        """Result found only in secondary must list only secondary provider."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("b.py", 0.8, provider="cohere")]
        fused = fuse_fn(primary, secondary, limit=10)
        b_result = next(r for r in fused if r.file_path == "b.py")
        assert b_result.contributing_providers == ["cohere"]
        assert b_result.fusion_score is not None

    def test_source_provider_remains_fused(self, fuse_fn):
        """source_provider is 'fused' even with contributing_providers set."""
        primary = [_make_result("a.py", 0.9, provider="voyage-ai")]
        secondary = [_make_result("a.py", 0.8, provider="cohere")]
        fused = fuse_fn(primary, secondary, limit=10)
        assert fused[0].source_provider == "fused"


class TestEdgeCases:
    def test_fuse_rrf_empty_inputs(self):
        assert fuse_rrf([], []) == []

    def test_fuse_multiply_empty_inputs(self):
        assert fuse_multiply([], []) == []

    def test_fuse_average_empty_inputs(self):
        assert fuse_average([], []) == []

    def test_parallel_both_fail(self):
        def fail_primary():
            raise RuntimeError("primary down")

        def fail_secondary():
            raise RuntimeError("secondary down")

        result = execute_parallel_query(fail_primary, fail_secondary)
        assert result == []

    def test_failover_both_fail(self):
        def fail_primary():
            raise RuntimeError("primary down")

        def fail_secondary():
            raise RuntimeError("secondary down")

        with pytest.raises(RuntimeError, match="secondary down"):
            execute_failover_query(fail_primary, fail_secondary)

    def test_fusion_does_not_mutate_inputs(self):
        r1 = QueryResult(file_path="a.py", score=0.9, chunk_id="1", source_provider="p")
        r2 = QueryResult(file_path="b.py", score=0.7, chunk_id="2", source_provider="s")
        original_score_r1 = r1.score
        original_score_r2 = r2.score
        original_provider_r1 = r1.source_provider
        original_provider_r2 = r2.source_provider
        fuse_rrf([r1], [r2])
        assert r1.score == original_score_r1, "fuse_rrf mutated input score"
        assert r2.score == original_score_r2, "fuse_rrf mutated input score"
        assert r1.source_provider == original_provider_r1, (
            "fuse_rrf mutated input source_provider"
        )
        assert r2.source_provider == original_provider_r2, (
            "fuse_rrf mutated input source_provider"
        )
