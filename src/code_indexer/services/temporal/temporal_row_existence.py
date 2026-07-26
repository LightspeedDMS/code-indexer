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


def temporal_shard_has_committed_rows(shard_dir: Path) -> bool:
    """Return True iff at least one committed row file exists under shard_dir.

    Side-effect-free (never writes anything) and short-circuits on the
    FIRST match found -- does not enumerate every row file in a shard that
    may hold thousands of hash-sharded `vector_*.json` files.

    Args:
        shard_dir: Path to a temporal quarter-shard or monolith collection
            directory (e.g. `.code-indexer/index/code-indexer-temporal-{slug}-{quarter}/`).

    Returns:
        False if shard_dir does not exist or is not a directory, or if it
        contains zero `vector_*.json` files anywhere in its (4-level
        hash-sharded) subtree. True on the first row file found.
    """
    shard_dir = Path(shard_dir)
    if not shard_dir.is_dir():
        return False
    for _ in shard_dir.rglob("vector_*.json"):
        return True
    return False
