"""
Additive health badge evaluator for external dependency latency.

Story #680: External Dependency Latency Observability

Provides:
- DependencyHealthEvaluator: computes the final health badge status by taking
  the max severity of the existing CPU/memory/disk status and the worst
  dependency latency status. Existing rules are NEVER weakened.
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Valid status values in severity order.
_VALID_STATUSES = frozenset({"healthy", "degraded", "unhealthy"})

# Severity rank: higher = worse. Only valid statuses are present.
_SEVERITY_RANK: Dict[str, int] = {
    "healthy": 0,
    "degraded": 1,
    "unhealthy": 2,
}


def _validate_status(value: str, field_name: str) -> None:
    """Raise ValueError if value is not one of the three valid status strings."""
    if value not in _VALID_STATUSES:
        raise ValueError(
            f"{field_name} must be one of {sorted(_VALID_STATUSES)}, got {value!r}"
        )


def _max_severity(a: str, b: str) -> str:
    """Return whichever status has higher severity. Both must be valid status strings."""
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


class DependencyHealthEvaluator:
    """
    Compute the final dashboard health badge using additive max-severity logic.

    Algorithm: final = max(existing_health_status, worst_dependency_status)

    The existing CPU/memory/disk rules are NEVER weakened — dependency data
    can only raise the severity, never lower it.
    """

    def evaluate(
        self,
        existing_health_status: str,
        all_dependency_stats: Dict[str, Dict],
    ) -> Dict[str, str]:
        """
        Compute the final badge status from existing rules and dependency stats.

        Args:
            existing_health_status: Output of current CPU/memory/disk/DB-connectivity
                                    check. Must be "healthy", "degraded", or "unhealthy".
            all_dependency_stats:   Mapping of dep_name → stats dict. Each stats dict
                                    must contain a "status" key with one of the three
                                    valid status strings. Must not be None.

        Returns:
            Dict with keys:
                "final_status":            max(existing, worst_dep) severity
                "existing_contribution":   the incoming existing_health_status unchanged
                "dependency_contribution": worst status across all dependency stats
                                           ("healthy" when all_dependency_stats is empty)

        Raises:
            TypeError:  If all_dependency_stats is not a dict (e.g. None).
            ValueError: If existing_health_status is not a valid status, or if any
                        dep stats dict is missing or has an invalid "status" key.
        """
        if not isinstance(all_dependency_stats, dict):
            raise TypeError(
                f"all_dependency_stats must be a dict, got {type(all_dependency_stats).__name__}"
            )
        _validate_status(existing_health_status, "existing_health_status")

        for dep_name, stats in all_dependency_stats.items():
            if not isinstance(stats, dict) or "status" not in stats:
                raise ValueError(
                    f"dep stats for {dep_name!r} must be a dict containing a 'status' key"
                )
            _validate_status(stats["status"], f"dependency '{dep_name}' status")

        worst_dep = "healthy"
        for stats in all_dependency_stats.values():
            worst_dep = _max_severity(worst_dep, stats["status"])

        final = _max_severity(existing_health_status, worst_dep)

        return {
            "final_status": final,
            "existing_contribution": existing_health_status,
            "dependency_contribution": worst_dep,
        }
