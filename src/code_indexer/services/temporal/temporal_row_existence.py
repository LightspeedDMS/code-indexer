"""Shared row-existence-not-queryability primitive (Story #1457 AC6/AC8/AC11).

`hnsw_index.bin` presence answers "is this shard currently searchable" (a
QUERYABILITY signal) -- a fundamentally different question from "does this
shard hold any committed data at all" (a DATA-EXISTENCE signal). Conflating
the two was a real bug class this story's design corrects repeatedly: a
quarter/monolith shard directory can have real committed
`vector_*.json` rows with NO `hnsw_index.bin` (e.g. a crash between
`upsert_points()` and `end_indexing()`), and such a shard MUST still be
detected as "has data" by any bootstrap/publish/discovery decision.

This module provides the ONE shared, side-effect-free scan every such
decision (AC6's three-branch build decision, AC8's resolver in-repo
fallback, AC11's bootstrap discovery) reuses -- never
`IDIndexManager.rebuild_from_vectors`, which is NOT read-only (it writes
`id_index.bin` as a side effect, Story #1458 finding F6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal


def temporal_shard_has_committed_rows(
    shard_dir: Path,
    *,
    on_error: Literal["treat_absent", "raise"] = "treat_absent",
) -> bool:
    """Return True iff at least one committed row exists under shard_dir.

    Side-effect-free and short-circuits on the first match. Layout-aware:
    dispatches on ``resolve_chunk_layout`` (the project's sole authority),
    never a bare ``chunks.db`` existence probe.

    Args:
        shard_dir: A temporal quarter-shard or monolith collection directory.
        on_error: Policy for an UNREADABLE CHUNKS_DB store (corrupt, locked,
            permission denied), forwarded to ``chunk_store_has_real_data``.
            ``"treat_absent"`` (default) answers False -- for status/health
            reporting, which must stay resilient. ``"raise"`` propagates and
            is REQUIRED for any DESTRUCTIVE caller, where "no data" means
            "safe to wipe and re-embed" (Bug #1529 finding #5; see
            ``temporal_reindex_needs_clear``).

    Returns:
        False if shard_dir is not a directory or holds zero rows.

    Raises:
        Propagates ``chunk_store_has_real_data``'s error when
        ``on_error="raise"`` and the store is unreadable.
    """
    shard_dir = Path(shard_dir)
    if not shard_dir.is_dir():
        return False

    from code_indexer.storage.shared.chunk_layout import (
        ChunkLayout,
        resolve_chunk_layout,
    )

    if resolve_chunk_layout(shard_dir) is ChunkLayout.CHUNKS_DB:
        from code_indexer.storage.sqlite_chunk_store import chunk_store_has_real_data

        # on_error is the CALLER's policy, never hardcoded here.
        return bool(
            chunk_store_has_real_data(shard_dir / "chunks.db", on_error=on_error)
        )

    for _ in shard_dir.rglob("vector_*.json"):
        return True
    return False
