"""Temporal circuit breaker health tracking (Story #635).

Per-domain health keys separate temporal health from semantic health.
Temporal keys use model names: "temporal:voyage-code-3"
Semantic keys use provider names: "voyage-ai" (unchanged, backward compat)
"""

import logging
import threading
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Health key prefix for temporal domain
TEMPORAL_HEALTH_PREFIX = "temporal:"

# Collection name prefix used by temporal indexer
_TEMPORAL_COLLECTION_PREFIX = "code-indexer-temporal-"

# Story #1457 AC8 Step 6: pin-acquisition exhaustion counters, DELIBERATELY
# separate from ProviderHealthMonitor -- a lost resolve/validate race
# against a concurrent alias swap is a transient lock-contention event, not
# a provider-health signal, and must never feed record_temporal_failure's
# circuit-breaker scoring.
#
# 2026-07-23 code review MEDIUM #12 (CLAUDE.md cluster-aware-state scope
# disclosure): this dict is PROCESS-LOCAL ONLY -- in a multi-node cluster,
# each node has its own independent tally, and get_temporal_pin_exhaustion_
# count() below returns ONLY the calling node's count, never a cluster-wide
# aggregate. It is intentionally NOT promoted to a PostgreSQL/SQLite dual-
# backend table (this codebase's established pattern for genuinely shared
# cross-node counters, e.g. golden_repo_reconcile_breaker_state): pin
# exhaustion is a rare, already-durably-observable event via the WARNING
# log emitted below, which flows into this server's DB-backed log store
# (already cluster-aware, queryable cluster-wide via admin_logs_query /
# the Post-E2E Log Audit gate) -- the actual established shared
# observability mechanism for this class of rare-event operator signal.
# get_temporal_pin_exhaustion_count() has zero production callers today
# (test-only); should a future health/admin surface need a genuine
# cluster-wide aggregate, route it through the log store's query API or a
# dedicated dual-backend table at that time, rather than trusting this
# process-local value across nodes.
_pin_exhaustion_counts: Dict[str, int] = {}
_pin_exhaustion_lock = threading.Lock()


def record_temporal_pin_exhaustion(model_or_collection: str) -> None:
    """Record a pin-acquisition exhaustion event for observability.

    Story #1457 AC8 Step 6: called when TemporalShardResolver.pin() lost
    _PIN_MAX_ATTEMPTS consecutive resolve/validate races. Deliberately does
    NOT call ProviderHealthMonitor -- a lost pointer race is a transient
    lock-contention event, not a provider-health signal, so it must not
    degrade the provider circuit breaker record_temporal_failure() drives.

    The WARNING log emitted here (not the in-process counter) is the
    durable, cluster-wide-queryable observability record -- see the
    process-local scope disclosure above _pin_exhaustion_counts.
    """
    key = make_temporal_health_key(model_or_collection)
    with _pin_exhaustion_lock:
        _pin_exhaustion_counts[key] = _pin_exhaustion_counts.get(key, 0) + 1
        count = _pin_exhaustion_counts[key]
    logger.warning(
        "[temporal-pin-exhausted] recorded for %s (process-local cumulative count=%d)",
        key,
        count,
    )


def get_temporal_pin_exhaustion_count(model_or_collection: str) -> int:
    """Return THIS NODE's process-local cumulative pin-exhaustion count.

    NOT a cluster-wide aggregate -- see the scope disclosure above
    _pin_exhaustion_counts. In a multi-node deployment, other nodes'
    exhaustion events are invisible to this call.
    """
    key = make_temporal_health_key(model_or_collection)
    with _pin_exhaustion_lock:
        return _pin_exhaustion_counts.get(key, 0)


def make_temporal_health_key(model_or_collection: str) -> str:
    """Build temporal-domain health key.

    Args:
        model_or_collection: Model name or collection name

    Returns:
        Health key like "temporal:voyage-code-3"
    """
    name = model_or_collection
    if name.startswith(_TEMPORAL_COLLECTION_PREFIX):
        name = name[len(_TEMPORAL_COLLECTION_PREFIX) :]
    return f"{TEMPORAL_HEALTH_PREFIX}{name}"


def record_temporal_success(model_or_collection: str, latency_ms: float) -> None:
    """Record a successful temporal query for health monitoring."""
    try:
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        key = make_temporal_health_key(model_or_collection)
        ProviderHealthMonitor.get_instance().record_call(key, latency_ms, success=True)
    except Exception:
        logger.debug(
            "Health monitoring unavailable for temporal success recording",
            exc_info=True,
        )


def record_temporal_failure(model_or_collection: str, latency_ms: float) -> None:
    """Record a failed temporal query for health monitoring."""
    try:
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        key = make_temporal_health_key(model_or_collection)
        ProviderHealthMonitor.get_instance().record_call(key, latency_ms, success=False)
    except Exception:
        logger.debug(
            "Health monitoring unavailable for temporal failure recording",
            exc_info=True,
        )


def is_temporal_provider_healthy(model_or_collection: str) -> bool:
    """Check if a temporal provider's circuit breaker is closed (healthy).

    Returns True if healthy or if health monitor is unavailable.
    """
    try:
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        key = make_temporal_health_key(model_or_collection)
        health = ProviderHealthMonitor.get_instance().get_health(key)
        status = health.get(key)
        if status is None:
            return True
        return bool(status.status != "down")
    except Exception:
        logger.debug(
            "Health monitor unavailable for temporal provider check, defaulting to healthy",
            exc_info=True,
        )
        return True


def filter_healthy_temporal_providers(
    collections: List[Tuple[str, object]],
) -> Tuple[List[Tuple[str, object]], List[Tuple[str, object]]]:
    """Filter temporal collections to only healthy providers.

    Args:
        collections: List of (collection_name, provider_hint) tuples

    Returns:
        Tuple of (healthy_collections, skipped_collections)
    """
    healthy = []
    skipped = []

    for coll_name, hint in collections:
        if is_temporal_provider_healthy(coll_name):
            healthy.append((coll_name, hint))
        else:
            logger.warning(
                "Skipping %s for temporal query: circuit breaker open",
                coll_name,
            )
            skipped.append((coll_name, hint))

    # If ALL providers unhealthy, attempt anyway with warning
    if not healthy and skipped:
        logger.warning(
            "All temporal providers have open circuit breakers. "
            "Attempting query anyway."
        )
        return list(skipped), []

    return healthy, skipped
