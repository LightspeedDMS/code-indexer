"""Global temporal indexing floor date resolution (Story #1404).

A single global, DB-backed "temporal indexing floor date" bounds ALL
FUTURE `cidx index --index-commits` runs across the fleet, composing with
the pre-existing per-repo `temporal_options.since_date` override as
"more restrictive wins" (the later/newer date governs). This module is
the single shared resolution point used by all four corrected launch
sites (server/repositories/golden_repo_manager.py -- two sites,
server/services/activated_repo_index_manager.py,
global_repos/refresh_scheduler.py, server/mcp/handlers/repos.py's
_build_temporal_index_cmd) so the precedence rule is implemented exactly
once, never duplicated per call site.
"""

from typing import Optional


def resolve_temporal_floor_date() -> Optional[str]:
    """Read the global temporal indexing floor date from DB-backed server
    config.

    Returns:
        The configured floor date string ("YYYY-MM-DD"), or None when
        unset/empty (unbounded -- byte-identical to pre-feature
        full-history behavior).
    """
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    temporal_indexing = config.temporal_indexing_config
    if temporal_indexing is None:
        return None
    floor_date: Optional[str] = temporal_indexing.index_floor_date
    if not floor_date:
        return None
    return floor_date


def resolve_effective_floor_date(
    global_floor_date: Optional[str], per_repo_since_date: Optional[str]
) -> Optional[str]:
    """Compose the global floor date with a per-repo since_date override as
    "more restrictive wins" (Story #1404 spec-corrections item 2 /
    Scenario 6).

    The EFFECTIVE floor for any given repo/launch is
    max(global_floor_date, per_repo_since_date) -- the later/more
    restrictive of the two "YYYY-MM-DD" date strings (lexicographic max is
    chronological max for this format). Exactly one value is ever
    produced -- callers must emit exactly one --since-date/since_date,
    never two. If either input is unset/None/empty, the other governs
    alone. If both are unset, returns None (unbounded, pre-feature no-op,
    unchanged).

    Args:
        global_floor_date: The global floor date, or None/"" if unset.
        per_repo_since_date: The per-repo since_date override, or
            None/"" if unset.

    Returns:
        The effective (more restrictive) date string, or None if both
        inputs are unset/empty.
    """
    candidates = [d for d in (global_floor_date, per_repo_since_date) if d]
    if not candidates:
        return None
    return max(candidates)
