"""AC6's three-branch publish dispatch (Story #1457).

Ties together the resolver (AC8), the build primitives
(`temporal_consolidated_build.py`), and the publish decision
(`temporal_shard_publisher.py`) into ONE cohesive "execute an AC6 refresh
for one (embedder, quarter) namespace" function.

The branch decision reuses `TemporalShardResolver.resolve()`'s EXISTING
pointer-first / in-repo-fallback-second result -- never a separate,
hand-rolled `alias_exists()` + row-existence check:
  - SISTER_POINTER result -> Branch A: copy the current version + apply
    ONLY the new-commit delta, then publish via swap_alias (subsequent
    publish -- the pointer already exists).
  - IN_REPO_LEGACY result  -> Branch B-bootstrap: build a fresh
    consolidated version from BOTH the legacy in-repo rows AND the new
    delta, then publish via create_alias (first-ever publish).
  - None                   -> Branch B-fresh: build a fresh consolidated
    version from ONLY the new delta, then publish via create_alias
    (first-ever publish, genuinely new quarter with zero prior data).

NOTE (honest scope disclosure): row-sourcing for the IN_REPO_LEGACY branch
is injected via `legacy_row_reader` -- a separate, pluggable concern this
module does NOT implement (reading the actual `vector_*.json` hash-sharded
file content is AC11's step-1 scan primitive, not yet built). This module
owns ONLY the branch DECISION and orchestration of already-built pieces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.services.temporal.temporal_consolidated_build import (
    build_fresh_consolidated_temporal_version,
    copy_and_extend_consolidated_temporal_version,
)
from code_indexer.services.temporal.temporal_shard_publisher import (
    publish_temporal_shard_version,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
    TemporalShardSource,
)


def execute_temporal_refresh_branch(
    resolver: TemporalShardResolver,
    snapshot_manager: VersionedSnapshotManager,
    alias_manager: AliasManager,
    repo_alias: str,
    sister_root: Path,
    embedder_slug: str,
    quarter: Optional[str],
    new_delta_rows: List[Dict[str, Any]],
    legacy_row_reader: Callable[[Path], Iterable[Dict[str, Any]]],
    vector_dim: int,
    force_rebuild: bool = False,
) -> Path:
    """Execute AC6's three-branch build+publish decision for one namespace.

    Args:
        resolver: TemporalShardResolver for this repo+embedder (constructed
            with the SAME repo_alias/sister_root passed here).
        snapshot_manager: VersionedSnapshotManager rooted at the SAME
            sister_root (constructed with versioned_base=str(sister_root)).
        alias_manager: AliasManager for publishing (SAME instance the
            resolver reads from).
        repo_alias: The golden repo's alias -- must match resolver/
            snapshot_manager's construction, passed explicitly rather than
            reached out of those objects' internals.
        sister_root: Root directory for `.versioned/{ns}/v_*` -- must match
            snapshot_manager's construction, passed explicitly for the same
            reason.
        embedder_slug: Sanitized embedder model slug.
        quarter: Quarter suffix (e.g. "2024Q1"), or None for the
            quarter-less monolith namespace.
        new_delta_rows: This refresh's new-commit delta rows.
        legacy_row_reader: Callable(legacy_path) -> Iterable[record dict],
            invoked ONLY for the IN_REPO_LEGACY branch to stream the
            existing legacy shard's historical rows.
        vector_dim: Expected vector dimension.
        force_rebuild: Story #1457 HIGH #11 (2026-07-23 code review). Local-
            repair signal (the caller's `was_stale`/Bug #1407 barrier) --
            forwarded to Branch A's copy-and-extend call so a locally-
            repaired shard's sister HNSW index is rebuilt even when this
            refresh's own new_delta_rows is empty. Branches B-bootstrap and
            B-fresh always build fresh from scratch regardless of this flag,
            so it has no effect on those branches.

    Returns:
        The path to the newly-published version directory.
    """
    resolved = resolver.resolve(embedder_slug, quarter)
    suffix = f"-{quarter}" if quarter else ""
    pointer_namespace = f"{repo_alias}-temporal-{embedder_slug}{suffix}"

    if resolved is not None and resolved.source == TemporalShardSource.SISTER_POINTER:
        new_version_path = copy_and_extend_consolidated_temporal_version(
            snapshot_manager,
            pointer_namespace,
            resolved.path,
            new_delta_rows,
            vector_dim,
            force_rebuild=force_rebuild,
        )
    elif resolved is not None and resolved.source == TemporalShardSource.IN_REPO_LEGACY:
        legacy_rows = list(legacy_row_reader(resolved.path))
        new_version_path = build_fresh_consolidated_temporal_version(
            sister_root,
            pointer_namespace,
            [legacy_rows, new_delta_rows],
            vector_dim,
            embedder_slug=embedder_slug,
        )
    else:
        new_version_path = build_fresh_consolidated_temporal_version(
            sister_root,
            pointer_namespace,
            [new_delta_rows],
            vector_dim,
            embedder_slug=embedder_slug,
        )

    publish_temporal_shard_version(alias_manager, pointer_namespace, new_version_path)

    return Path(new_version_path)
