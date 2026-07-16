"""Shared per-read post-processor for temporal query snapshots.

Story #1400 Phase 5 (FINAL LOCKED DESIGN). Applied fresh on EVERY read
(handoff, poll_search_job, REST result endpoint) for the reader's
protocol + permissions, in ONE canonical order:

    access-filter FIRST -> dedup -> rerank (terminal reads only, over the
    full candidate pool, top_k=requested_limit) -> requested_limit
    truncation -> protocol wrap

Access-filter first = never rerank/return unauthorized data.

Terminal-only RERANK is implemented: when `terminal=True`, `ctx.rerank_query`
is present, and a `config_service` is supplied, this module actually invokes
the reranker (reranking._apply_reranking_sync) over the full deduped
candidate pool, with `deadline_monotonic` (CRITICAL 5) propagated through to
cap the provider HTTP timeout / 429-retry backoff sleeps. `unranked` is then
derived from the ACTUAL reranker outcome via derive_unranked() -- never from
mere rerank_query presence. When rerank is not requested, the read is not
terminal, or no config_service is supplied (partial reads never need one),
this falls back to truncate-only with unranked=True (conservative: never
claims a ranking guarantee not actually performed). Partials (non-terminal
reads) are ALWAYS unranked=True by design -- rerank is terminal-only.

Protocol wrap (MCP _mcp_response / REST JSON body) is each door's own
concern, applied by the caller after this function returns.
"""

from typing import Any, Dict, List, Optional, Tuple


def _dedup_key(result: Dict[str, Any]) -> Tuple[Any, Any]:
    """Same dedup identity as the temporal display path: file_path +
    commit_hash (mirrors make_temporal_dedup_key's intent at the
    QueryResult.to_dict() level, where commit_hash lives under
    metadata.commit_hash)."""
    commit_hash = (result.get("metadata") or {}).get("commit_hash")
    return (result.get("file_path"), commit_hash)


def _dedup(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []
    for r in results:
        key = _dedup_key(r)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _validate_requested_limit(requested_limit: Any) -> Optional[int]:
    if requested_limit is None:
        return None
    if not isinstance(requested_limit, int) or isinstance(requested_limit, bool):
        raise ValueError(f"ctx.requested_limit must be an int, got {requested_limit!r}")
    if requested_limit < 0:
        raise ValueError(f"ctx.requested_limit must be >= 0, got {requested_limit}")
    return requested_limit


def postprocess_temporal_snapshot(
    snapshot: Dict[str, Any],
    access_filtering_service: Any,
    username: str,
    is_admin: bool,
    terminal: bool,
    config_service: Optional[Any] = None,
    deadline_monotonic: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], int, Optional[int], bool]:
    """Apply the canonical per-read post-processing order to a temporal
    snapshot (as produced by store_temporal_snapshot/read_temporal_snapshot).

    Args:
        snapshot: parsed snapshot envelope (results/shards_completed/
            shards_total/ctx).
        access_filtering_service: real AccessFilteringService instance
            (filter_query_results(results, user_id) contract, accepts
            dicts).
        username: the requesting user.
        is_admin: whether the requester is an admin (bypasses filtering).
        terminal: whether this is a terminal (completed) read -- rerank is
            terminal-only, by design; partials are always unranked=True.
        config_service: real ConfigService instance, required to actually
            invoke the reranker on a terminal read with ctx.rerank_query
            present. None (default) -- used by partial reads, which never
            rerank -- falls back to truncate-only with unranked=True.
        deadline_monotonic: Story #1400 CRITICAL 5. Propagated into the
            reranker call to cap provider HTTP timeout / 429-retry sleep
            budget by remaining time.

    Returns:
        (results, shards_completed, shards_total, unranked).

    Raises:
        ValueError: ctx.requested_limit is present but not a non-negative
            int.
    """
    ctx = snapshot.get("ctx") or {}
    requested_limit = _validate_requested_limit(ctx.get("requested_limit"))
    raw_results = snapshot.get("results") or []

    # Access-filter FIRST -- never process/return unauthorized data.
    if is_admin:
        filtered = raw_results
    else:
        filtered = access_filtering_service.filter_query_results(raw_results, username)

    deduped = _dedup(filtered)

    rerank_query = ctx.get("rerank_query")
    if terminal and rerank_query and config_service is not None:
        from code_indexer.server.mcp.reranking import (
            _apply_reranking_sync,
            derive_unranked,
            extract_rerank_document,
        )

        effective_limit = (
            requested_limit if requested_limit is not None else len(deduped)
        )
        reranked, rerank_meta = _apply_reranking_sync(
            results=deduped,
            rerank_query=rerank_query,
            rerank_instruction=ctx.get("rerank_instruction"),
            content_extractor=extract_rerank_document,
            requested_limit=effective_limit,
            config_service=config_service,
            deadline_monotonic=deadline_monotonic,
        )
        return (
            reranked,
            snapshot.get("shards_completed", 0),
            snapshot.get("shards_total"),
            derive_unranked(rerank_meta),
        )

    if requested_limit is not None:
        truncated = deduped[:requested_limit]
    else:
        truncated = deduped

    # No rerank requested, not a terminal read, or no config_service
    # supplied -- conservatively unranked=True (never claims a ranking
    # guarantee not actually performed).
    return (
        truncated,
        snapshot.get("shards_completed", 0),
        snapshot.get("shards_total"),
        True,
    )
