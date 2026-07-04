"""Story #1293 (path x outcome) -> (role, live_batch_id) decision table.

Epic #1288 / Story #1293 (Query-Embedding Decision Event Recording). This
module is the single source of truth for classifying one query-embedding
decision into a durable ``search_embed_event`` row's ``(outcome, role,
live_batch_id)`` triple.

Two things live here:

  DECISION_TABLE -- the FULL 11-row reference table from the story's
      Algorithm 2 (documentation-as-code; unit-tested exhaustively per the
      story's Testing Requirements). It covers rows that are only reachable
      once the coalescer's owner/joiner wiring lands (Story #1293 S1b, item
      A3) as well as the rows reachable today (S1a).

  decide_role_and_outcome() -- the S1a-REACHABLE pure classifier. It covers
      exactly the rows produced by governed_call.py's own "Path B" (no
      coalescer) constructions: direct live call, direct cache hit, direct
      shadow live, shadow cache hit, bypass, and failover (error). It takes
      no coalescer-awareness parameter because, for these rows, live_batch_id
      is always None (never part of a coalesced provider HTTP batch) — the
      caller decides at emit_embed_event() call sites whether to invoke this
      function at all (S1a callers only invoke it for Path B; the Path A
      coalescer-owner/joiner rows are wired in S1b directly on the returned
      EmbeddingCacheMetadata's role/outcome fields, bypassing this classifier
      entirely).
"""

from typing import Dict, Optional, Tuple

# Full (path x outcome) -> (role, live_batch_id_kind) reference table.
#
# live_batch_id_kind:
#   "new"   -- the owner assigns a freshly generated live_batch_id (S1b).
#   "owner" -- a joiner reuses the owner's live_batch_id (S1b).
#   None    -- never coalesced-batch scoped; live_batch_id is always NULL.
DECISION_TABLE: Dict[str, Tuple[str, str, Optional[str]]] = {
    "coalescer_owner_cold": ("miss", "owner", "new"),
    "coalescer_joiner": ("hit", "joiner", "owner"),
    "warm_hit": ("hit", "warm_hit", None),
    "direct_live": ("miss", "direct", None),
    "direct_hit": ("hit", "warm_hit", None),
    "coalesced_shadow_live": ("shadow_miss", "owner", "new"),
    "direct_shadow_live": ("shadow_miss", "direct", None),
    "shadow_hit": ("shadow_hit", "warm_hit", None),
    "bypass": ("bypass", "direct", None),
    "failover_primary_fail": ("error", "direct", None),
    "failover_secondary_ok": ("miss", "direct", None),
}


def decide_role_and_outcome(
    *,
    cache_hit: Optional[bool],
    cache_mode: Optional[str],
    bypass: bool = False,
    error: bool = False,
) -> Tuple[str, str]:
    """Classify one Path-B (non-coalesced) query-embedding call.

    Args:
        cache_hit: The EmbeddingCacheMetadata.key_found value. True means a
            genuine cache hit (warm or shadow); False or None means a live
            provider call was made (no cache, cache miss, or bypass).
        cache_mode: The EmbeddingCacheMetadata.cache_mode value ("on",
            "shadow", or None when no cache was consulted).
        bypass: True when the caller requested no_embedding_cache_shortcut
            (S4 read-skip) for this call.
        error: True when the call raised (failover primary-attempt failure).

    Returns:
        (outcome, role) — live_batch_id is always None for every row this
        function can produce (it only classifies non-coalesced rows).
    """
    if error:
        outcome, role, _kind = DECISION_TABLE["failover_primary_fail"]
        return outcome, role
    if bypass:
        outcome, role, _kind = DECISION_TABLE["bypass"]
        return outcome, role

    is_shadow = cache_mode == "shadow"
    if cache_hit:
        row_key = "shadow_hit" if is_shadow else "direct_hit"
    else:
        row_key = "direct_shadow_live" if is_shadow else "direct_live"
    outcome, role, _kind = DECISION_TABLE[row_key]
    return outcome, role
