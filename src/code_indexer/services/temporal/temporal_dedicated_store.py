"""Dedicated temporal read-side FilesystemVectorStore (Story #1457 AC2).

"Introduce a dedicated temporal vector store instance rooted at the sister
location; do NOT thread a second root through the shared semantic store."

The sister_root is the SAME `golden_repos_dir` `VersionedSnapshotManager`
already uses for semantic versioned snapshots (`versioned_base`, see
`server/startup/clone_backend_wiring.py`'s `build_snapshot_manager`) --
`.versioned/` already lives there as a SIBLING of each golden repo's clone,
so no new physical root concept is introduced. `aliases_dir =
golden_repos_dir / "aliases"` matches the ~15 real production
`AliasManager` construction sites verbatim (e.g.
`server/query/semantic_query_manager.py`, `server/multi/multi_search_service.py`).

NOTE (honest scope disclosure): this module builds ONLY the store
CONSTRUCTION function -- wiring the resolver so `_get_collection_path`
correctly resolves published temporal names. It does NOT wire this store
into any of the five live front-door query call sites
(`semantic_query_manager.py`, `daemon/service.py`,
`multi_search_service.py`, `cli.py`, `temporal_worker.py`) -- that live
wiring is deliberately deferred pending the resolution-scope pin (AC8 Step
6), which is not yet built. Wiring the query hot path without that
protection would reintroduce the exact mid-read deletion hazard the
story's many correction rounds exist to prevent.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def build_dedicated_temporal_read_store(
    golden_repos_dir: Path,
    repo_alias: str,
    legacy_index_path: Path,
) -> FilesystemVectorStore:
    """Construct the dedicated temporal read-side store for one golden repo.

    Args:
        golden_repos_dir: The server's golden-repos root directory (the
            SAME root `VersionedSnapshotManager` uses as `versioned_base`).
        repo_alias: The golden repo's alias (e.g. "evolution").
        legacy_index_path: The golden repo's ORIGINAL in-repo
            `.code-indexer/index/` directory (the AC8 in-repo fallback
            source for un-migrated quarters).

    Returns:
        A `FilesystemVectorStore` constructed WITH a `TemporalShardResolver`
        injected, rooted at the sister location -- resolving published
        temporal collection names through the resolver (per-instance-gated,
        AC8), never through a second thread of the shared semantic store.

    Raises:
        ValueError: any of the three required arguments is missing/empty.
    """
    if not golden_repos_dir:
        raise ValueError("golden_repos_dir is required and must be non-empty")
    if not repo_alias:
        raise ValueError("repo_alias is required and must be a non-empty string")
    if not legacy_index_path:
        raise ValueError("legacy_index_path is required and must be non-empty")

    golden_repos_dir = Path(golden_repos_dir)
    sister_root = golden_repos_dir
    aliases_dir = golden_repos_dir / "aliases"

    alias_manager = AliasManager(str(aliases_dir))
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=sister_root,
        legacy_index_path=Path(legacy_index_path),
    )

    return FilesystemVectorStore(
        base_path=sister_root,
        temporal_shard_resolver=resolver,
    )
