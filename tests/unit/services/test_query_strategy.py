"""Tests for query strategy and score fusion (Story #488)."""

from code_indexer.services.query_strategy import (
    QueryStrategy,
    ScoreFusion,
    QueryResult,
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
