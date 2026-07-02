"""Temporal circuit breaker health tracking (Story #635).

Per-domain health keys separate temporal health from semantic health.
Temporal keys use model names: "temporal:voyage-code-3"
Semantic keys use provider names: "voyage-ai" (unchanged, backward compat)
"""

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Health key prefix for temporal domain
TEMPORAL_HEALTH_PREFIX = "temporal:"

# Collection name prefix used by temporal indexer
_TEMPORAL_COLLECTION_PREFIX = "code-indexer-temporal-"


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
