"""Story #1400 HIGH item: unified fusion_fetch_limit computation.

FINAL LOCKED DESIGN: "Unify fusion_fetch_limit computation into ONE shared
function both doors call (adopt MCP's more-correct access-filter-aware
formula as the canonical one -- REST's current formula silently under-
fetches relative to it). Keep fusion_fetch_limit IN the dedup signature --
once unified, an identical logical query from either door now genuinely
produces the identical value, so Scenario 12's 'same job' claim becomes
true for identical requests landing on the same node."

Reproduces MCP's exact two-step formula (search.py's
_compute_effective_limit + _compute_rerank_limit) as the SINGLE
implementation both MCP and REST temporal adapters call:

    1. effective_limit = access_filtering_service.calculate_over_fetch_limit(
           requested_limit) for a non-admin user with an access service,
       else requested_limit unchanged.
    2. If rerank_query is present, apply reranking.calculate_overfetch_limit
       on top, with access_filter_extra = effective_limit - requested_limit.
"""

from typing import Any, Optional

_DEFAULT_OVERFETCH_MULTIPLIER = 5


def compute_temporal_fusion_fetch_limit(
    requested_limit: int,
    rerank_query: Optional[str],
    access_filtering_service: Any,
    username: str,
    config_service: Any,
) -> int:
    """Return the fusion_fetch_limit both MCP and REST temporal adapters
    must use -- the single shared formula that makes an identical logical
    query produce an identical dedup signature regardless of which door it
    arrives through (Scenario 12, same-node case).

    Raises:
        ValueError: requested_limit is not a positive int, or rerank_query
            is present but config_service is None (required to read the
            rerank overfetch multiplier).
    """
    if not isinstance(requested_limit, int) or isinstance(requested_limit, bool):
        raise ValueError(f"requested_limit must be an int, got {requested_limit!r}")
    if requested_limit <= 0:
        raise ValueError(f"requested_limit must be > 0, got {requested_limit}")
    if rerank_query and config_service is None:
        raise ValueError(
            "config_service is required when rerank_query is present "
            "(needed to read the rerank overfetch multiplier)"
        )

    effective_limit = requested_limit
    if (
        access_filtering_service is not None
        and not access_filtering_service.is_admin_user(username)
    ):
        effective_limit = access_filtering_service.calculate_over_fetch_limit(
            requested_limit
        )

    if not rerank_query:
        return effective_limit

    from code_indexer.server.mcp.reranking import calculate_overfetch_limit

    rerank_config = config_service.get_config().rerank_config
    overfetch_mul = (
        rerank_config.overfetch_multiplier
        if rerank_config
        else _DEFAULT_OVERFETCH_MULTIPLIER
    )
    access_filter_extra = effective_limit - requested_limit
    result: int = calculate_overfetch_limit(
        requested_limit, overfetch_mul, access_filter_extra
    )
    return result
