"""
Unit tests for DependencyLatencyAggregator - percentile computation and dep row building.

Story #680: External Dependency Latency Observability

Tests written FIRST following TDD methodology.

Full scope: percentile computation, trend arrows (up/down/stable/insufficient),
status evaluation (healthy/degraded/unhealthy), build_rows always-visible deps
(embed, storage-mode, clone-backend filtering, registered deps), warm vs cold
promotion, row cap, and status field integration.
"""

import time
from typing import Dict, List, Optional

import pytest

# Module-level import eliminates repeated setup across fixtures and tests.
from code_indexer.server.services.dependency_latency_aggregator import (  # noqa: E402
    DependencyLatencyAggregator,
)

# ── Named constants: dependency names ─────────────────────────────────────────
DEP_VOYAGE_EMBED = "voyageai_embed"
DEP_COHERE_EMBED = "cohere_embed"
DEP_POSTGRES = "postgres"
DEP_SQLITE = "sqlite"
DEP_GITHUB = "github"
DEP_VOYAGE_RERANK = "voyage_rerank"
DEP_COHERE_RERANK = "cohere_rerank"
DEP_COW_DAEMON = "cow_daemon"
DEP_ONTAP = "ontap_flexclone"
DEP_CUSTOM = "custom_dep"
DEP_WARM_FAST = "warm_fast_dep"
DEP_NAME_PREFIX = "dep_"

# ── Named constants: latency values ───────────────────────────────────────────
LATENCY_FAST_MS = 100.0
LATENCY_MED_MS = 500.0
LATENCY_SLOW_MS = 1000.0
LATENCY_CRITICAL_MS = 2500.0
LATENCY_BELOW_WARN_MS = 400.0
FLOAT_TOLERANCE = 0.01

# ── Named constants: thresholds ───────────────────────────────────────────────
WARN_THRESHOLD_MS = 500.0
CRITICAL_THRESHOLD_MS = 2000.0

# ── Named constants: trend values ─────────────────────────────────────────────
TREND_UP = "^"
TREND_DOWN = "v"
TREND_STABLE = "->"
TREND_INSUFFICIENT = ""

# ── Named constants: status values ────────────────────────────────────────────
STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_UNHEALTHY = "unhealthy"

# ── Named constants: storage / clone modes ────────────────────────────────────
STORAGE_SQLITE = "sqlite"
STORAGE_POSTGRES = "postgres"
CLONE_NONE = "none"
CLONE_COW = "cow_daemon"
CLONE_ONTAP = "ontap_flexclone"

# ── Named constants: sample values ────────────────────────────────────────────
DEFAULT_STATUS_CODE = 200
NODE_ID = "node-1"
PREV_SAMPLES_FEW_COUNT = 4  # one fewer than MIN_SAMPLES_FOR_TREND
TREND_UP_RATIO = 1.15
TREND_DOWN_RATIO = 0.85
TREND_STABLE_RATIO = 1.05
ROW_CAP = 10
MULTI_DEP_COUNT = 15
REPEAT_SAMPLES_COUNT = 10

# ── Named constants: percentile boundary values ───────────────────────────────
P50_LOW_BOUND = 49.0
P50_HIGH_BOUND = 51.0
P95_LOW_BOUND = 94.0
P95_HIGH_BOUND = 96.0
P99_LOW_BOUND = 98.0
P99_HIGH_BOUND = 100.0
LATENCY_RANGE_COUNT = 100
LATENCY_RANGE_START = 1

# ── Named constants: row keys ─────────────────────────────────────────────────
P50_KEY = "p50_ms"
P95_KEY = "p95_ms"
P99_KEY = "p99_ms"
STATUS_KEY = "status"
NAME_KEY = "name"

# ── Named constants: index / count ────────────────────────────────────────────
FIRST_INDEX = 0
MIN_ROW_COUNT = 1

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_samples(
    dep_name: str, latencies: List[float], base_time: Optional[float] = None
) -> List[Dict]:
    """Build a list of sample dicts for a dependency."""
    if base_time is None:
        base_time = time.time()
    return [
        {
            "node_id": NODE_ID,
            "dependency_name": dep_name,
            "timestamp": base_time + float(i),
            "latency_ms": lat,
            "status_code": DEFAULT_STATUS_CODE,
        }
        for i, lat in enumerate(latencies)
    ]


def _uniform_stats(p95_ms: float) -> Dict:
    """Return a stats dict with p50=p95=p99 all equal to p95_ms for simplicity."""
    return {P50_KEY: p95_ms, P95_KEY: p95_ms, P99_KEY: p95_ms}


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def agg() -> DependencyLatencyAggregator:
    """Default DependencyLatencyAggregator with default thresholds."""
    return DependencyLatencyAggregator()


@pytest.fixture
def agg_with_thresholds() -> DependencyLatencyAggregator:
    """DependencyLatencyAggregator with explicit WARN and CRITICAL thresholds."""
    return DependencyLatencyAggregator(
        warn_ms=WARN_THRESHOLD_MS, critical_ms=CRITICAL_THRESHOLD_MS
    )


# ── Tests: percentile computation ─────────────────────────────────────────────


@pytest.mark.slow
class TestDependencyLatencyAggregatorPercentiles:
    """Tests for p50/p95/p99 percentile computation."""

    def test_percentiles_computed_from_samples(self, agg) -> None:
        """Aggregator computes p50/p95/p99 from current window samples."""
        latencies = [
            float(i) for i in range(LATENCY_RANGE_START, LATENCY_RANGE_COUNT + 1)
        ]
        samples = _make_samples(
            DEP_VOYAGE_EMBED, latencies, base_time=time.time() - 10.0
        )
        dep_stats = agg.compute_stats(samples)[DEP_VOYAGE_EMBED]
        assert P50_LOW_BOUND <= dep_stats[P50_KEY] <= P50_HIGH_BOUND
        assert P95_LOW_BOUND <= dep_stats[P95_KEY] <= P95_HIGH_BOUND
        assert P99_LOW_BOUND <= dep_stats[P99_KEY] <= P99_HIGH_BOUND

    def test_empty_samples_returns_empty_stats(self, agg) -> None:
        """Aggregator returns empty dict when no samples are provided."""
        assert agg.compute_stats([]) == {}

    def test_single_sample_has_equal_percentiles(self, agg) -> None:
        """With one sample, p50=p95=p99 all equal the sample latency."""
        samples = _make_samples(DEP_POSTGRES, [LATENCY_FAST_MS])
        dep_stats = agg.compute_stats(samples)[DEP_POSTGRES]
        assert abs(dep_stats[P50_KEY] - LATENCY_FAST_MS) < FLOAT_TOLERANCE
        assert abs(dep_stats[P95_KEY] - LATENCY_FAST_MS) < FLOAT_TOLERANCE
        assert abs(dep_stats[P99_KEY] - LATENCY_FAST_MS) < FLOAT_TOLERANCE


# ── Tests: trend arrows ───────────────────────────────────────────────────────


@pytest.mark.slow
class TestDependencyLatencyAggregatorTrendArrows:
    """Tests for trend arrow computation."""

    def test_trend_up_when_p95_increases_significantly(self, agg) -> None:
        """Trend is '^' when current_p95 / previous_p95 > 1.10."""
        prev = _make_samples(DEP_VOYAGE_EMBED, [LATENCY_FAST_MS] * REPEAT_SAMPLES_COUNT)
        assert (
            agg.compute_trend(
                current_p95=LATENCY_FAST_MS * TREND_UP_RATIO, prev_samples=prev
            )
            == TREND_UP
        )

    def test_trend_down_when_p95_decreases_significantly(self, agg) -> None:
        """Trend is 'v' when current_p95 / previous_p95 < 0.90."""
        prev = _make_samples(DEP_VOYAGE_EMBED, [LATENCY_SLOW_MS] * REPEAT_SAMPLES_COUNT)
        assert (
            agg.compute_trend(
                current_p95=LATENCY_SLOW_MS * TREND_DOWN_RATIO, prev_samples=prev
            )
            == TREND_DOWN
        )

    def test_trend_stable_when_p95_changes_slightly(self, agg) -> None:
        """Trend is '->' when current_p95 / previous_p95 is within 10%."""
        prev = _make_samples(DEP_VOYAGE_EMBED, [LATENCY_MED_MS] * REPEAT_SAMPLES_COUNT)
        assert (
            agg.compute_trend(
                current_p95=LATENCY_MED_MS * TREND_STABLE_RATIO, prev_samples=prev
            )
            == TREND_STABLE
        )

    def test_trend_insufficient_when_prev_window_too_few_samples(self, agg) -> None:
        """Trend is '' when previous window has fewer than 5 samples."""
        prev = _make_samples(
            DEP_VOYAGE_EMBED, [LATENCY_FAST_MS] * PREV_SAMPLES_FEW_COUNT
        )
        assert (
            agg.compute_trend(current_p95=LATENCY_SLOW_MS, prev_samples=prev)
            == TREND_INSUFFICIENT
        )

    def test_trend_insufficient_when_prev_window_empty(self, agg) -> None:
        """Trend is '' when there are no previous window samples."""
        assert (
            agg.compute_trend(current_p95=LATENCY_SLOW_MS, prev_samples=[])
            == TREND_INSUFFICIENT
        )


# ── Tests: status evaluation ──────────────────────────────────────────────────


@pytest.mark.slow
class TestDependencyLatencyAggregatorStatusEvaluation:
    """Tests for health status evaluation based on p95 thresholds."""

    def test_status_healthy_below_warn_threshold(self, agg_with_thresholds) -> None:
        """Status is 'healthy' when p95 < warn_ms."""
        assert (
            agg_with_thresholds.evaluate_status(p95_ms=LATENCY_BELOW_WARN_MS)
            == STATUS_HEALTHY
        )

    def test_status_degraded_at_exact_warn_threshold(self, agg_with_thresholds) -> None:
        """Status is 'degraded' when p95 == warn_ms (exact boundary)."""
        assert (
            agg_with_thresholds.evaluate_status(p95_ms=WARN_THRESHOLD_MS)
            == STATUS_DEGRADED
        )

    def test_status_unhealthy_at_exact_critical_threshold(
        self, agg_with_thresholds
    ) -> None:
        """Status is 'unhealthy' when p95 == critical_ms (exact boundary)."""
        assert (
            agg_with_thresholds.evaluate_status(p95_ms=CRITICAL_THRESHOLD_MS)
            == STATUS_UNHEALTHY
        )


# ── Tests: build_rows ─────────────────────────────────────────────────────────


@pytest.mark.slow
class TestDependencyLatencyAggregatorBuildRows:
    """Tests for build_rows: always-visible, clone-backend, promotion, row cap, status."""

    def test_always_visible_embed_deps_present(self, agg) -> None:
        """voyageai_embed and cohere_embed always appear in rows."""
        all_stats = {
            DEP_VOYAGE_EMBED: _uniform_stats(LATENCY_FAST_MS),
            DEP_COHERE_EMBED: _uniform_stats(LATENCY_FAST_MS),
        }
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        names = [r[NAME_KEY] for r in rows]
        assert DEP_VOYAGE_EMBED in names
        assert DEP_COHERE_EMBED in names

    def test_sqlite_dep_always_visible_when_storage_sqlite(self, agg) -> None:
        """sqlite dep is always-visible when storage_mode='sqlite'."""
        all_stats = {DEP_SQLITE: _uniform_stats(LATENCY_FAST_MS)}
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        assert DEP_SQLITE in [r[NAME_KEY] for r in rows]

    def test_postgres_dep_always_visible_when_storage_postgres(self, agg) -> None:
        """postgres dep is always-visible when storage_mode='postgres'."""
        all_stats = {DEP_POSTGRES: _uniform_stats(LATENCY_FAST_MS)}
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_POSTGRES,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        assert DEP_POSTGRES in [r[NAME_KEY] for r in rows]

    def test_cow_daemon_always_visible_when_clone_backend_cow(self, agg) -> None:
        """cow_daemon dep is always-visible when clone_backend='cow_daemon'."""
        all_stats = {DEP_COW_DAEMON: _uniform_stats(LATENCY_FAST_MS)}
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_COW,
            registered_dep_names=[],
        )
        assert DEP_COW_DAEMON in [r[NAME_KEY] for r in rows]

    def test_ontap_always_visible_when_clone_backend_ontap(self, agg) -> None:
        """ontap_flexclone dep is always-visible when clone_backend='ontap_flexclone'."""
        all_stats = {DEP_ONTAP: _uniform_stats(LATENCY_FAST_MS)}
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_ONTAP,
            registered_dep_names=[],
        )
        assert DEP_ONTAP in [r[NAME_KEY] for r in rows]

    def test_clone_deps_excluded_when_clone_backend_none(self, agg) -> None:
        """cow_daemon and ontap_flexclone are excluded when clone_backend='none'."""
        all_stats = {
            DEP_COW_DAEMON: _uniform_stats(LATENCY_FAST_MS),
            DEP_ONTAP: _uniform_stats(LATENCY_FAST_MS),
        }
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        names = [r[NAME_KEY] for r in rows]
        assert DEP_COW_DAEMON not in names
        assert DEP_ONTAP not in names

    def test_github_included_when_registered(self, agg) -> None:
        """github dep is included when it appears in registered_dep_names."""
        all_stats = {DEP_GITHUB: _uniform_stats(LATENCY_FAST_MS)}
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[DEP_GITHUB],
        )
        assert DEP_GITHUB in [r[NAME_KEY] for r in rows]

    def test_row_cap_is_ten(self, agg) -> None:
        """build_rows returns at most 10 rows regardless of dep count."""
        all_stats = {
            f"{DEP_NAME_PREFIX}{i}": _uniform_stats(LATENCY_FAST_MS)
            for i in range(MULTI_DEP_COUNT)
        }
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[
                f"{DEP_NAME_PREFIX}{i}" for i in range(MULTI_DEP_COUNT)
            ],
        )
        assert len(rows) <= ROW_CAP

    def test_slow_cold_dep_promoted_to_rows(self, agg) -> None:
        """A cold dep with high p95 is promoted into rows."""
        all_stats = {
            DEP_VOYAGE_EMBED: _uniform_stats(LATENCY_FAST_MS),
            DEP_COHERE_EMBED: _uniform_stats(LATENCY_FAST_MS),
            DEP_CUSTOM: _uniform_stats(LATENCY_CRITICAL_MS),
        }
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        assert DEP_CUSTOM in [r[NAME_KEY] for r in rows]

    def test_fast_warm_dep_not_promoted_without_slow_dep(self, agg) -> None:
        """A fast warm dep not in always-visible or registered set is NOT promoted."""
        # warm dep is not in always-visible set, not registered, and has low latency.
        all_stats = {DEP_WARM_FAST: _uniform_stats(LATENCY_FAST_MS)}
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        assert DEP_WARM_FAST not in [r[NAME_KEY] for r in rows]

    def test_slow_dep_ranked_before_fast_warm_dep(self, agg) -> None:
        """Slow cold dep appears before fast warm dep when both appear in rows."""
        all_stats = {
            DEP_VOYAGE_EMBED: _uniform_stats(LATENCY_FAST_MS),
            DEP_COHERE_EMBED: _uniform_stats(LATENCY_FAST_MS),
            DEP_WARM_FAST: _uniform_stats(LATENCY_FAST_MS),
            DEP_CUSTOM: _uniform_stats(LATENCY_CRITICAL_MS),
        }
        rows = agg.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[DEP_WARM_FAST],
        )
        names = [r[NAME_KEY] for r in rows]
        assert DEP_CUSTOM in names
        if DEP_WARM_FAST in names:
            assert names.index(DEP_CUSTOM) <= names.index(DEP_WARM_FAST)

    def test_row_status_field_reflects_p95(self, agg_with_thresholds) -> None:
        """Each row's 'status' field matches p95 threshold evaluation."""
        all_stats = {DEP_VOYAGE_EMBED: _uniform_stats(CRITICAL_THRESHOLD_MS)}
        rows = agg_with_thresholds.build_rows(
            all_stats=all_stats,
            storage_mode=STORAGE_SQLITE,
            clone_backend=CLONE_NONE,
            registered_dep_names=[],
        )
        voyage_rows = [r for r in rows if r[NAME_KEY] == DEP_VOYAGE_EMBED]
        assert len(voyage_rows) >= MIN_ROW_COUNT
        assert voyage_rows[FIRST_INDEX][STATUS_KEY] == STATUS_UNHEALTHY
