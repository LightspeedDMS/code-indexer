"""
Percentile aggregation and dashboard row builder for external dependency latency.

Story #680: External Dependency Latency Observability

Provides:
- DependencyLatencyAggregator: computes p50/p95/p99, trend arrows,
  health status, and builds ordered dashboard rows respecting always-visible
  sets, clone-backend filtering, registered deps, warm/cold promotion, and
  the 10-row cap.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Percentile method ─────────────────────────────────────────────────────────
# Nearest-rank method: rank = ceil(p/100 * n), 1-indexed.

# ── Trend thresholds ──────────────────────────────────────────────────────────
_TREND_UP_RATIO = 1.10
_TREND_DOWN_RATIO = 0.90
_MIN_SAMPLES_FOR_TREND = 5

# ── Default health thresholds (ms) ────────────────────────────────────────────
_DEFAULT_WARN_MS = 500.0
_DEFAULT_CRITICAL_MS = 2000.0

# ── Trend / status sentinel values ───────────────────────────────────────────
_TREND_UP = "^"
_TREND_DOWN = "v"
_TREND_STABLE = "->"
_TREND_INSUFFICIENT = ""

_STATUS_HEALTHY = "healthy"
_STATUS_DEGRADED = "degraded"
_STATUS_UNHEALTHY = "unhealthy"

# ── Always-visible dependency sets (tuples for deterministic ordering) ────────
# These deps always appear in the dashboard regardless of latency.
_ALWAYS_EMBED = ("voyageai_embed", "cohere_embed")
_ALWAYS_RERANK = ("voyage_rerank", "cohere_rerank")

# Storage-mode-dependent always-visible deps (keyed by storage_mode value).
_STORAGE_MODE_DEPS: Dict[str, str] = {
    "sqlite": "sqlite",
    "postgres": "postgres",
}

# Clone-backend-dependent always-visible deps (keyed by clone_backend value).
_CLONE_BACKEND_DEPS: Dict[str, str] = {
    "cow_daemon": "cow_daemon",
    "ontap_flexclone": "ontap_flexclone",
}

# ── Row cap ───────────────────────────────────────────────────────────────────
_ROW_CAP = 10


def _percentile(sorted_values: List[float], p: int) -> float:
    """
    Compute the p-th percentile using the nearest-rank method.

    Args:
        sorted_values: Non-empty list of floats sorted ascending.
        p:             Percentile to compute (0 < p <= 100).

    Returns:
        The nearest-rank percentile value.
    """
    import math

    n = len(sorted_values)
    rank = math.ceil(p / 100.0 * n)
    # Clamp to valid index range (rank is 1-indexed).
    rank = max(1, min(rank, n))
    return sorted_values[rank - 1]


class DependencyLatencyAggregator:
    """
    Compute percentile statistics and build ordered dashboard rows for
    external dependency latency observability.

    Instances are stateless with respect to sample data — all sample data is
    passed in as arguments to each method, making the class safe to share
    across requests without synchronization.
    """

    def __init__(
        self,
        warn_ms: float = _DEFAULT_WARN_MS,
        critical_ms: float = _DEFAULT_CRITICAL_MS,
    ) -> None:
        """
        Args:
            warn_ms:     p95 threshold (ms) at or above which status = 'degraded'.
            critical_ms: p95 threshold (ms) at or above which status = 'unhealthy'.
        """
        self._warn_ms = warn_ms
        self._critical_ms = critical_ms

    # ── Public API ─────────────────────────────────────────────────────────────

    def compute_stats(self, samples: List[Dict]) -> Dict[str, Dict[str, float]]:
        """
        Compute p50/p95/p99 for each dependency in the sample list.

        Args:
            samples: List of sample dicts with keys: dependency_name, latency_ms.

        Returns:
            Dict mapping dependency_name → {"p50_ms": float, "p95_ms": float,
            "p99_ms": float}. Empty dict if samples is empty.
        """
        if not samples:
            return {}

        # Group latencies by dependency name.
        by_dep: Dict[str, List[float]] = {}
        for sample in samples:
            dep = sample.get("dependency_name", "")
            if not dep:
                continue
            by_dep.setdefault(dep, []).append(float(sample.get("latency_ms", 0.0)))

        result: Dict[str, Dict[str, float]] = {}
        for dep, latencies in by_dep.items():
            sorted_lats = sorted(latencies)
            result[dep] = {
                "p50_ms": _percentile(sorted_lats, 50),
                "p95_ms": _percentile(sorted_lats, 95),
                "p99_ms": _percentile(sorted_lats, 99),
            }
        return result

    def compute_trend(
        self,
        current_p95: float,
        prev_samples: List[Dict],
    ) -> str:
        """
        Compute a trend arrow by comparing current p95 to the previous window's p95.

        Args:
            current_p95:  Current window p95 latency in ms.
            prev_samples: Raw samples from the previous time window.

        Returns:
            "^" (up), "v" (down), "->" (stable), or "" (insufficient data).
        """
        if len(prev_samples) < _MIN_SAMPLES_FOR_TREND:
            return _TREND_INSUFFICIENT

        prev_latencies = sorted(float(s.get("latency_ms", 0.0)) for s in prev_samples)
        prev_p95 = _percentile(prev_latencies, 95)

        if prev_p95 == 0.0:
            return _TREND_INSUFFICIENT

        ratio = current_p95 / prev_p95
        if ratio > _TREND_UP_RATIO:
            return _TREND_UP
        if ratio < _TREND_DOWN_RATIO:
            return _TREND_DOWN
        return _TREND_STABLE

    def evaluate_status(self, p95_ms: float) -> str:
        """
        Return health status string based on p95 against configured thresholds.

        Args:
            p95_ms: p95 latency in milliseconds.

        Returns:
            "unhealthy" if p95_ms >= critical_ms,
            "degraded"  if p95_ms >= warn_ms,
            "healthy"   otherwise.
        """
        if p95_ms >= self._critical_ms:
            return _STATUS_UNHEALTHY
        if p95_ms >= self._warn_ms:
            return _STATUS_DEGRADED
        return _STATUS_HEALTHY

    def build_rows(
        self,
        all_stats: Dict[str, Dict[str, float]],
        storage_mode: str,
        clone_backend: str,
        registered_dep_names: List[str],
    ) -> List[Dict]:
        """
        Build ordered dashboard rows respecting always-visible sets, clone-backend
        filtering, registered deps, warm/cold promotion, and the 10-row cap.

        Always-visible deps (embed, rerank, storage-mode-specific,
        clone-backend-specific, registered) appear first in deterministic order.
        Cold deps with p95 above the warn threshold are promoted into remaining
        slots (up to _ROW_CAP total), ranked by p95 descending.

        Args:
            all_stats:           Mapping of dep_name → stats dict from compute_stats.
            storage_mode:        Current storage mode ("sqlite" or "postgres").
            clone_backend:       Current clone backend ("none", "cow_daemon",
                                 "ontap_flexclone").
            registered_dep_names: Dep names from registered golden repos.

        Returns:
            Ordered list of row dicts, each with keys:
            "name", "p50_ms", "p95_ms", "p99_ms", "status".
            At most _ROW_CAP rows.
        """
        # ── Step 1: build the "core" always-visible tier ─────────────────────
        # Core = embed, rerank, storage-mode dep, clone-backend dep.
        # These always appear first in deterministic order.
        core_deps: List[str] = []
        core_seen: set = set()

        def _add_core(dep_name: str) -> None:
            if dep_name not in core_seen:
                core_deps.append(dep_name)
                core_seen.add(dep_name)

        for dep in _ALWAYS_EMBED:
            _add_core(dep)
        for dep in _ALWAYS_RERANK:
            _add_core(dep)

        storage_dep: Optional[str] = _STORAGE_MODE_DEPS.get(storage_mode)
        if storage_dep:
            _add_core(storage_dep)

        clone_dep: Optional[str] = _CLONE_BACKEND_DEPS.get(clone_backend)
        if clone_dep:
            _add_core(clone_dep)

        rows: List[Dict] = []
        for dep in core_deps:
            stats = all_stats.get(dep)
            if stats is None:
                continue
            rows.append(self._make_row(dep, stats))

        # ── Step 2: build secondary tier (registered + promoted cold) ─────────
        # Registered deps and cold deps with high p95 are ranked together by
        # p95 descending, so slow deps surface above fast registered deps.
        all_seen = set(core_seen)
        secondary: List[tuple] = []

        # Add registered deps (always-visible but not core).
        for dep in registered_dep_names:
            if dep not in all_seen:
                dep_stats = all_stats.get(dep)
                if dep_stats is not None:
                    secondary.append((dep, dep_stats.get("p95_ms") or 0.0))
                    all_seen.add(dep)

        # Promote cold deps (not in any always-visible set) above warn threshold.
        for dep, dep_stats in all_stats.items():
            if dep not in all_seen:
                p95 = dep_stats.get("p95_ms") or 0.0
                if p95 >= self._warn_ms:
                    secondary.append((dep, p95))
                    all_seen.add(dep)

        # Sort secondary tier by p95 descending so slowest deps appear first.
        secondary.sort(key=lambda pair: pair[1], reverse=True)

        remaining_slots = _ROW_CAP - len(rows)
        for dep, _ in secondary[:remaining_slots]:
            dep_stats = all_stats[dep]
            rows.append(self._make_row(dep, dep_stats))

        return rows[:_ROW_CAP]

    # ── Private helpers ────────────────────────────────────────────────────────

    def _make_row(self, dep_name: str, stats: Dict[str, float]) -> Dict:
        """Build a single dashboard row dict from dep name and stats."""
        p95 = stats.get("p95_ms", 0.0) or 0.0
        return {
            "name": dep_name,
            "p50_ms": stats.get("p50_ms", 0.0) or 0.0,
            "p95_ms": p95,
            "p99_ms": stats.get("p99_ms", 0.0) or 0.0,
            "status": self.evaluate_status(p95),
        }
