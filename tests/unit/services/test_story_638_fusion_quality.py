"""
Tests for Story #638: Dual-Provider Fusion Quality Improvements.

Acceptance criteria:
AC1: Over-fetching — limit=6 → each provider gets limit=12, fusion receives up to 24
AC2: Score gate triggers when weaker_max < stronger_max * 0.80
AC3: Score gate is symmetric (detects weaker regardless of which is primary)
AC4: Score gate does NOT trigger when providers are close
AC5: Fully-culled weaker provider falls through gracefully
AC6: Global normalization preserves score gap for fuse_multiply
AC7: Global normalization preserves score gap for fuse_average
AC8: RRF behavior unchanged (fuse_rrf must NOT be changed)
AC9: Single-provider fallback unaffected
"""

from code_indexer.services.query_strategy import (
    QueryResult,
    fuse_rrf,
    fuse_multiply,
    fuse_average,
    _normalize_scores_global,
    PARALLEL_FETCH_MULTIPLIER,
    MAX_PARALLEL_FETCH,
    SCORE_GATE_RATIO,
    SCORE_GATE_FLOOR,
    PARALLEL_TIMEOUT_SECONDS,
)


def _make_result(file_path, score, provider="primary", chunk_id="0", repo="repo"):
    return QueryResult(
        file_path=file_path,
        score=score,
        chunk_id=chunk_id,
        source_provider=provider,
        repository_alias=repo,
    )


# ---------------------------------------------------------------------------
# AC1: Constants for over-fetch dispatch
# ---------------------------------------------------------------------------
class TestOverFetchConstants:
    """AC1: Verify over-fetch constants exist with correct values."""

    def test_parallel_fetch_multiplier_is_2(self):
        assert PARALLEL_FETCH_MULTIPLIER == 2

    def test_max_parallel_fetch_is_40(self):
        assert MAX_PARALLEL_FETCH == 40

    def test_score_gate_ratio_is_0_80(self):
        assert SCORE_GATE_RATIO == 0.80

    def test_score_gate_floor_is_0_70(self):
        assert SCORE_GATE_FLOOR == 0.70

    def test_parallel_timeout_is_20(self):
        assert PARALLEL_TIMEOUT_SECONDS == 20

    def test_over_fetch_limit_calculation_basic(self):
        """limit=6 → provider_fetch_limit = min(6*2, 40) = 12."""
        limit = 6
        provider_fetch_limit = min(
            limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
        )
        assert provider_fetch_limit == 12

    def test_over_fetch_limit_capped_at_max(self):
        """limit=30 → provider_fetch_limit = min(30*2, 40) = 40."""
        limit = 30
        provider_fetch_limit = min(
            limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
        )
        assert provider_fetch_limit == 40

    def test_over_fetch_limit_small(self):
        """limit=1 → provider_fetch_limit = min(1*2, 40) = 2."""
        limit = 1
        provider_fetch_limit = min(
            limit * PARALLEL_FETCH_MULTIPLIER, MAX_PARALLEL_FETCH
        )
        assert provider_fetch_limit == 2


# ---------------------------------------------------------------------------
# AC2: Score gate triggers when weaker_max < stronger_max * SCORE_GATE_RATIO
# ---------------------------------------------------------------------------
class TestScoreGateTriggers:
    """AC2: Score gate activates when weaker provider is clearly inferior."""

    def test_score_gate_filters_weak_secondary_results(self):
        """Secondary provider's max (0.60) < primary max (0.90) * 0.80 = 0.72 → gate fires."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [
            _make_result("a.py", 0.90, provider="voyage-ai"),
            _make_result("b.py", 0.85, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("c.py", 0.60, provider="cohere"),
            _make_result("d.py", 0.30, provider="cohere"),
        ]
        gated_primary, gated_secondary = apply_score_gate(primary, secondary)
        # Primary unchanged
        assert len(gated_primary) == 2
        # Secondary filtered: floor = 0.90 * 0.70 = 0.63, both 0.60 and 0.30 < 0.63
        assert len(gated_secondary) == 0

    def test_score_gate_keeps_strong_secondary_results(self):
        """Secondary results above floor are retained."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [
            _make_result("a.py", 0.90, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("b.py", 0.65, provider="cohere"),  # floor = 0.90 * 0.70 = 0.63
            _make_result("c.py", 0.30, provider="cohere"),  # below floor
        ]
        gated_primary, gated_secondary = apply_score_gate(primary, secondary)
        assert len(gated_primary) == 1
        # 0.65 >= 0.63 so kept; 0.30 < 0.63 so removed
        assert len(gated_secondary) == 1
        assert gated_secondary[0].file_path == "b.py"


# ---------------------------------------------------------------------------
# AC3: Score gate is symmetric (works regardless of which provider is weaker)
# ---------------------------------------------------------------------------
class TestScoreGateSymmetry:
    """AC3: Score gate detects weaker provider regardless of primary/secondary position."""

    def test_score_gate_when_primary_is_weaker(self):
        """Primary max (0.50) < secondary max (0.90) * 0.80 = 0.72 → primary gets filtered."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [
            _make_result("a.py", 0.50, provider="voyage-ai"),
            _make_result("b.py", 0.40, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("c.py", 0.90, provider="cohere"),
            _make_result("d.py", 0.85, provider="cohere"),
        ]
        gated_primary, gated_secondary = apply_score_gate(primary, secondary)
        # Secondary (stronger) unchanged
        assert len(gated_secondary) == 2
        # Primary (weaker): floor = 0.90 * 0.70 = 0.63, both 0.50 and 0.40 < 0.63
        assert len(gated_primary) == 0

    def test_score_gate_output_order_preserved(self):
        """apply_score_gate returns (primary_results, secondary_results) in original order."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [
            _make_result("a.py", 0.90, provider="voyage-ai"),
            _make_result("b.py", 0.88, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("c.py", 0.70, provider="cohere"),
        ]
        gated_p, gated_s = apply_score_gate(primary, secondary)
        assert [r.file_path for r in gated_p] == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# AC4: Score gate does NOT trigger when providers are close
# ---------------------------------------------------------------------------
class TestScoreGateNoTrigger:
    """AC4: Score gate must not fire when both providers produce similar quality."""

    def test_score_gate_no_trigger_when_providers_close(self):
        """Secondary max (0.85) >= primary max (0.90) * 0.80 = 0.72 → no gating."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [
            _make_result("a.py", 0.90, provider="voyage-ai"),
            _make_result("b.py", 0.80, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("c.py", 0.85, provider="cohere"),
            _make_result("d.py", 0.75, provider="cohere"),
        ]
        gated_primary, gated_secondary = apply_score_gate(primary, secondary)
        # No gating: all results pass through unchanged
        assert len(gated_primary) == 2
        assert len(gated_secondary) == 2

    def test_score_gate_at_exact_ratio_boundary_no_trigger(self):
        """secondary_max = primary_max * 0.80 exactly → NOT < ratio, no gating."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [_make_result("a.py", 1.0, provider="voyage-ai")]
        secondary = [_make_result("b.py", 0.80, provider="cohere")]
        gated_p, gated_s = apply_score_gate(primary, secondary)
        assert len(gated_p) == 1
        assert len(gated_s) == 1


# ---------------------------------------------------------------------------
# AC5: Fully-culled weaker provider falls through gracefully
# ---------------------------------------------------------------------------
class TestScoreGateFullyCulled:
    """AC5: When all weaker results are culled, result is empty list (not error)."""

    def test_score_gate_fully_culled_returns_empty(self):
        """All secondary results below floor → returns empty list, no exception."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [_make_result("a.py", 0.95, provider="voyage-ai")]
        secondary = [
            _make_result("b.py", 0.10, provider="cohere"),
            _make_result("c.py", 0.05, provider="cohere"),
        ]
        gated_p, gated_s = apply_score_gate(primary, secondary)
        assert len(gated_p) == 1
        assert len(gated_s) == 0

    def test_score_gate_with_empty_provider_no_crash(self):
        """apply_score_gate with one empty provider must not raise."""
        from code_indexer.services.query_strategy import apply_score_gate

        primary = [_make_result("a.py", 0.90, provider="voyage-ai")]
        secondary: list = []
        gated_p, gated_s = apply_score_gate(primary, secondary)
        assert len(gated_p) == 1
        assert len(gated_s) == 0


# ---------------------------------------------------------------------------
# AC6 & AC7: Global normalization preserves score gap for multiply and average
# ---------------------------------------------------------------------------
class TestGlobalNormalization:
    """AC6/AC7: _normalize_scores_global normalizes across the combined result pool."""

    def test_global_normalize_scores_single_pool(self):
        """All results normalized together — min becomes 0, max becomes 1."""
        results = [
            _make_result("a.py", 0.9),
            _make_result("b.py", 0.5),
            _make_result("c.py", 0.1),
        ]
        normed = _normalize_scores_global(results)
        assert abs(normed["repo:a.py:0"] - 1.0) < 1e-9
        assert abs(normed["repo:c.py:0"] - 0.0) < 1e-9
        assert abs(normed["repo:b.py:0"] - 0.5) < 1e-9

    def test_global_normalize_all_same_score(self):
        """All same scores → all normalize to 1.0."""
        results = [
            _make_result("a.py", 0.7),
            _make_result("b.py", 0.7),
        ]
        normed = _normalize_scores_global(results)
        for v in normed.values():
            assert v == 1.0

    def test_global_normalize_empty_returns_empty(self):
        assert _normalize_scores_global([]) == {}

    def test_fuse_multiply_global_norm_preserves_gap(self):
        """
        AC6: When primary has [0.9, 0.1] and secondary has [0.9, 0.1],
        global normalization across all 4 docs gives a.py score > b.py score.
        """
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
            _make_result("b.py", 0.1, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("a.py", 0.9, provider="cohere"),
            _make_result("b.py", 0.1, provider="cohere"),
        ]
        fused = fuse_multiply(primary, secondary, limit=10)
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        # a.py should score higher than b.py
        assert a_result.score > b_result.score

    def test_fuse_multiply_global_norm_asymmetric_providers(self):
        """
        AC6: Primary has [a.py=0.9, b.py=0.5], secondary has [c.py=0.9, d.py=0.5].
        No overlap between providers. Global norm: max=0.9, min=0.5.
        a.py (primary only): norm=1.0, * 0.5 neutral = 0.5
        b.py (primary only): norm=0.0, * 0.5 neutral = 0.0
        c.py (secondary only): 0.5 neutral * norm=1.0 = 0.5
        d.py (secondary only): 0.5 neutral * norm=0.0 = 0.0
        a.py and c.py must outscore b.py and d.py.
        """
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
            _make_result("b.py", 0.5, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("c.py", 0.9, provider="cohere"),
            _make_result("d.py", 0.5, provider="cohere"),
        ]
        fused = fuse_multiply(primary, secondary, limit=10)
        assert len(fused) == 4
        # High-score results (a.py, c.py) must rank above low-score (b.py, d.py)
        scores_by_file = {r.file_path: r.score for r in fused}
        assert scores_by_file["a.py"] > scores_by_file["b.py"]
        assert scores_by_file["c.py"] > scores_by_file["d.py"]

    def test_fuse_average_global_norm_preserves_gap(self):
        """
        AC7: When primary [0.9, 0.1] and secondary [0.9, 0.1],
        global norm across all 4 → a.py scores higher than b.py.
        """
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
            _make_result("b.py", 0.1, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("a.py", 0.9, provider="cohere"),
            _make_result("b.py", 0.1, provider="cohere"),
        ]
        fused = fuse_average(primary, secondary, limit=10)
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        assert a_result.score > b_result.score

    def test_fuse_average_single_provider_uses_half_consensus(self):
        """
        AC7: Single-provider result uses (global_norm + 0.5) / 2.
        With primary=[0.9] and secondary=[0.8 for different doc],
        primary-only doc a.py: global_norm(a.py) = 1.0 (max in combined pool).
        Score = (1.0 + 0.5) / 2 = 0.75.
        """
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("b.py", 0.8, provider="cohere"),
        ]
        fused = fuse_average(primary, secondary, limit=10)
        assert len(fused) == 2
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        # a.py: global_norm=1.0 → (1.0 + 0.5)/2 = 0.75
        # b.py: global_norm=0.0 → (0.0 + 0.5)/2 = 0.25
        assert abs(a_result.score - 0.75) < 1e-9
        assert abs(b_result.score - 0.25) < 1e-9

    def test_fuse_average_consensus_result_uses_global_norm(self):
        """
        AC7: When both providers have the same doc, score = global_norm[key].
        """
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
        ]
        secondary = [
            _make_result("a.py", 0.9, provider="cohere"),
        ]
        fused = fuse_average(primary, secondary, limit=10)
        assert len(fused) == 1
        # Only one doc → global_norm = 1.0 → score = 1.0
        assert abs(fused[0].score - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# AC8: RRF behavior is unchanged
# ---------------------------------------------------------------------------
class TestRRFUnchanged:
    """AC8: fuse_rrf must continue to work exactly as before (rank-based only)."""

    def test_rrf_still_uses_rank_not_score(self):
        """RRF score depends only on rank position, not raw scores."""
        primary = [_make_result("a.py", 0.1), _make_result("b.py", 0.9)]
        secondary = [_make_result("a.py", 0.2)]
        fused = fuse_rrf(primary, secondary, limit=10)
        # a.py at rank 0 in primary (1/(60+1)) + rank 0 in secondary (1/(60+1))
        # b.py at rank 1 in primary (1/(60+2))
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        assert a_result.score > b_result.score

    def test_rrf_ignores_raw_scores(self):
        """fuse_rrf: even if secondary has very high scores, rank still dominates."""
        primary = [_make_result("low.py", 0.01)]
        secondary = [_make_result("high.py", 0.99)]
        fused = fuse_rrf(primary, secondary, limit=10)
        # Both rank 0 in their provider → equal RRF scores
        assert len(fused) == 2
        assert abs(fused[0].score - fused[1].score) < 1e-9

    def test_rrf_does_not_use_global_normalization(self):
        """fuse_rrf must NOT call _normalize_scores_global."""
        # Verifying by checking that score formula matches 1/(k+rank) strictly
        from code_indexer.services.query_strategy import RRF_K

        primary = [_make_result("a.py", 0.5), _make_result("b.py", 0.4)]
        secondary = []  # type: ignore[var-annotated]
        fused = fuse_rrf(primary, secondary, limit=10)
        expected_a = 1.0 / (RRF_K + 1)
        expected_b = 1.0 / (RRF_K + 2)
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        assert abs(a_result.score - expected_a) < 1e-10
        assert abs(b_result.score - expected_b) < 1e-10


# ---------------------------------------------------------------------------
# AC9: Single-provider fallback unaffected
# ---------------------------------------------------------------------------
class TestSingleProviderFallback:
    """AC9: When only one provider active, behavior identical to before."""

    def test_fuse_multiply_empty_secondary_unchanged(self):
        """fuse_multiply with empty secondary returns primary results unmodified."""
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
            _make_result("b.py", 0.5, provider="voyage-ai"),
        ]
        fused = fuse_multiply(primary, [], limit=10)
        # With only primary, global_norm of a.py=1.0, b.py=0.0
        # multiply: p * 0.5 (neutral) → a.py=1.0*0.5=0.5, b.py=0.0*0.5=0.0
        assert len(fused) == 2
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        # a.py must still rank above b.py
        assert a_result.score >= b_result.score

    def test_fuse_average_empty_secondary_unchanged(self):
        """fuse_average with empty secondary returns primary results, order preserved."""
        primary = [
            _make_result("a.py", 0.9, provider="voyage-ai"),
            _make_result("b.py", 0.3, provider="voyage-ai"),
        ]
        fused = fuse_average(primary, [], limit=10)
        assert len(fused) == 2
        a_result = next(r for r in fused if r.file_path == "a.py")
        b_result = next(r for r in fused if r.file_path == "b.py")
        assert a_result.score > b_result.score

    def test_fuse_rrf_empty_secondary_unchanged(self):
        """fuse_rrf with empty secondary returns primary results."""
        primary = [
            _make_result("a.py", 0.9),
            _make_result("b.py", 0.5),
        ]
        fused = fuse_rrf(primary, [], limit=10)
        assert len(fused) == 2
        assert fused[0].file_path == "a.py"
