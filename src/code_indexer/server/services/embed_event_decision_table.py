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

Bug #1305 (semantics reconciliation, no table/classifier change here): Epic
#1288's original phrasing "provider_embed_calls == real transport HTTP-call
count" only holds when every row is a genuine miss/hit -- it diverges for two
rows in this very table (both Path-B, decide_role_and_outcome()-classified):

  - ``shadow_hit`` (outcome=shadow_hit, role=warm_hit, live_batch_id=None):
    shadow cache mode ALWAYS embeds live for comparison before ever checking
    the cache (governed_call._serve_with_cache), so this row's real HTTP call
    happened even though it is excluded from the "needed embed" count.
  - ``failover_primary_fail`` (outcome=error, role=direct, live_batch_id=
    None): the failed attempt hit the wire before raising.

``count_provider_embed_calls()`` (search_embed_event_writer.py) is therefore
the count of successful NEEDED embeds -- UNCHANGED by Bug #1305, since the
#1294 windowed dashboard depends on this exact definition. The separate,
additive ``count_transport_calls()`` on the same module is the real
transport HTTP-call count in all modes EXCEPT one residual case (also adds
the ``bypass`` row, which likewise always calls the provider live on
Path B).

**Residual NOT covered by this table's ``shadow_hit`` row (Path A only):**
a coalesced dispatch-batch member that resolves as a shadow HIT is NEVER
classified via THIS module -- embedding_coalescer.py's dispatch loop calls
``_make_hit_meta("shadow", ...)`` with no outcome/role override, so it is
stored at that helper's DEFAULTS: outcome='hit' (NOT 'shadow_hit'),
role='warm_hit', cache_mode='shadow', live_batch_id=None. It is
indistinguishable from a genuine on-mode warm hit except by cache_mode, and
a per-row count on cache_mode='shadow' would OVERCOUNT a batch with more
than one shadow-hit member (the whole batch made only ONE real HTTP call).
This is REACHABLE IN NORMAL WARM-SHADOW SERVER OPERATION (coalescer-on +
shadow-default + warm cache is the steady state), not a rare edge case, and
is an explicitly out-of-scope, documented limitation of Bug #1305 -- see
search_embed_event_writer.count_transport_calls()'s docstring and
embedding_coalescer.py's dispatch-loop comment (~:1115-1131) for the full
rationale. Neither counter requires any change to DECISION_TABLE's role/
outcome/live_batch_id classification.
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
