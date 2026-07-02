"""
Unit tests for DependencyHealthEvaluator - additive health badge logic.

Story #680: External Dependency Latency Observability

Algorithm 5: additive max(existing_health_status, worst_dependency_status).
Existing CPU/memory/disk rules are NEVER weakened.

Tests written FIRST following TDD methodology.
"""

import pytest

from code_indexer.server.services.dependency_health_evaluator import (
    DependencyHealthEvaluator,
)

# ── Named constants: status values ────────────────────────────────────────────
STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_UNHEALTHY = "unhealthy"

# ── Named constants: result keys ──────────────────────────────────────────────
KEY_FINAL = "final_status"
KEY_EXISTING = "existing_contribution"
KEY_DEP = "dependency_contribution"

# ── Named constants: dependency names ─────────────────────────────────────────
DEP_VOYAGE = "voyageai_embed"
DEP_COHERE = "cohere_embed"
DEP_POSTGRES = "postgres"

# ── Named constants: stats dict keys ──────────────────────────────────────────
STATS_STATUS_KEY = "status"


def _dep_stats(status: str) -> dict:
    """Build a minimal dep stats dict with the given status."""
    return {STATS_STATUS_KEY: status}


@pytest.fixture
def evaluator() -> DependencyHealthEvaluator:
    """Default DependencyHealthEvaluator instance."""
    return DependencyHealthEvaluator()


# ── Full 9-combination status matrix ─────────────────────────────────────────
# Parameters: (existing, dep_status, expected_final, expected_dep_contribution)
# All 9 pairs of {healthy, degraded, unhealthy} x {healthy, degraded, unhealthy}.
_STATUS_MATRIX = [
    # existing=healthy
    (STATUS_HEALTHY, STATUS_HEALTHY, STATUS_HEALTHY, STATUS_HEALTHY),
    (STATUS_HEALTHY, STATUS_DEGRADED, STATUS_DEGRADED, STATUS_DEGRADED),
    (STATUS_HEALTHY, STATUS_UNHEALTHY, STATUS_UNHEALTHY, STATUS_UNHEALTHY),
    # existing=degraded
    (STATUS_DEGRADED, STATUS_HEALTHY, STATUS_DEGRADED, STATUS_HEALTHY),
    (STATUS_DEGRADED, STATUS_DEGRADED, STATUS_DEGRADED, STATUS_DEGRADED),
    (STATUS_DEGRADED, STATUS_UNHEALTHY, STATUS_UNHEALTHY, STATUS_UNHEALTHY),
    # existing=unhealthy
    (STATUS_UNHEALTHY, STATUS_HEALTHY, STATUS_UNHEALTHY, STATUS_HEALTHY),
    (STATUS_UNHEALTHY, STATUS_DEGRADED, STATUS_UNHEALTHY, STATUS_DEGRADED),
    (STATUS_UNHEALTHY, STATUS_UNHEALTHY, STATUS_UNHEALTHY, STATUS_UNHEALTHY),
]


@pytest.mark.slow
class TestDependencyHealthEvaluatorStatusMatrix:
    """Full 9-combination status matrix and multi-dep worst-case tests."""

    @pytest.mark.parametrize(
        "existing,dep_status,expected_final,expected_dep",
        _STATUS_MATRIX,
        ids=[f"{e}/{d}" for e, d, _, _ in _STATUS_MATRIX],
    )
    def test_status_matrix(
        self,
        evaluator,
        existing: str,
        dep_status: str,
        expected_final: str,
        expected_dep: str,
    ) -> None:
        """All 9 (existing x dep) status combinations produce correct final, existing, dep contributions."""
        result = evaluator.evaluate(
            existing_health_status=existing,
            all_dependency_stats={DEP_VOYAGE: _dep_stats(dep_status)},
        )
        assert result[KEY_FINAL] == expected_final
        assert result[KEY_EXISTING] == existing
        assert result[KEY_DEP] == expected_dep

    def test_worst_dep_status_is_max_across_all_deps(self, evaluator) -> None:
        """Worst status across multiple deps is taken (not first or last)."""
        result = evaluator.evaluate(
            existing_health_status=STATUS_HEALTHY,
            all_dependency_stats={
                DEP_VOYAGE: _dep_stats(STATUS_HEALTHY),
                DEP_COHERE: _dep_stats(STATUS_DEGRADED),
                DEP_POSTGRES: _dep_stats(STATUS_UNHEALTHY),
            },
        )
        assert result[KEY_FINAL] == STATUS_UNHEALTHY
        assert result[KEY_EXISTING] == STATUS_HEALTHY
        assert result[KEY_DEP] == STATUS_UNHEALTHY


@pytest.mark.slow
class TestDependencyHealthEvaluatorEdgeCases:
    """Edge cases: empty deps and result dict structure."""

    def test_empty_deps_preserves_existing_status(self, evaluator) -> None:
        """With no dependency stats, final equals existing status; dep contribution is healthy."""
        result = evaluator.evaluate(
            existing_health_status=STATUS_DEGRADED,
            all_dependency_stats={},
        )
        assert result[KEY_FINAL] == STATUS_DEGRADED
        assert result[KEY_EXISTING] == STATUS_DEGRADED
        assert result[KEY_DEP] == STATUS_HEALTHY

    def test_result_dict_contains_all_three_keys(self, evaluator) -> None:
        """Result dict always contains final_status, existing_contribution, dependency_contribution."""
        result = evaluator.evaluate(
            existing_health_status=STATUS_HEALTHY,
            all_dependency_stats={},
        )
        assert KEY_FINAL in result
        assert KEY_EXISTING in result
        assert KEY_DEP in result


class TestDependencyHealthEvaluatorValidation:
    """Input validation: invalid statuses and bad argument types raise immediately."""

    def test_invalid_existing_status_raises(self, evaluator) -> None:
        """evaluate() raises ValueError when existing_health_status is not a valid status."""
        with pytest.raises(ValueError):
            evaluator.evaluate(
                existing_health_status="unknown_status",
                all_dependency_stats={},
            )

    def test_missing_status_key_in_dep_stats_raises(self, evaluator) -> None:
        """evaluate() raises ValueError when a dep stats dict is missing the 'status' key."""
        with pytest.raises(ValueError):
            evaluator.evaluate(
                existing_health_status=STATUS_HEALTHY,
                all_dependency_stats={DEP_VOYAGE: {"p95_ms": 100.0}},
            )

    def test_none_all_dependency_stats_raises(self, evaluator) -> None:
        """evaluate() raises TypeError or ValueError when all_dependency_stats is None."""
        with pytest.raises((TypeError, ValueError)):
            evaluator.evaluate(
                existing_health_status=STATUS_HEALTHY,
                # Deliberately passing None to verify that runtime validation
                # rejects non-dict input before iteration.
                all_dependency_stats=None,  # type: ignore[arg-type]
            )
