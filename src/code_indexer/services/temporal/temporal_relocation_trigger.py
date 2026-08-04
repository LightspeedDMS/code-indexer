"""maybe_relocate_shard_to_sister_location() -- Story #1457 AC1's actual
relocation trigger.

RETIRED BY BUG #1528 -- NOT WIRED INTO ANY PRODUCTION WRITE PATH, AND MUST
NOT BE RE-WIRED. Temporal collections are now written directly in the
consolidated ``chunks.db`` layout and migrated IN PLACE (fleet migration
server-side, ``cidx index --migrate-chunks-to-sqlite`` for a standalone
CLI), so there is no reason to publish a second copy elsewhere. Re-wiring
this would also be actively DESTRUCTIVE: every row this module publishes
comes from ``read_legacy_shard_rows`` (a ``vector_*.json`` scan), which
finds NOTHING in a ``chunks.db`` shard -- it would publish an EMPTY sister
version and swap the namespace pointer onto it, and
``TemporalShardResolver`` is pointer-first, so later queries for that
namespace would silently return zero rows. The READ side (the resolver and
any alias pointers already published before this fix) remains fully
supported so previously relocated data stays queryable; this module is kept
only as the reference for how that existing data was produced.

Wires AC6's already-built build+publish machinery
(temporal_refresh_dispatch.execute_temporal_refresh_branch) into the real
temporal indexing pipeline, gated on the CIDX_SERVER_REFRESH_CONTEXT env
var (set unconditionally by build_temporal_child_env for every
server-spawned temporal child, all storage modes -- Story #1457 round 1):

- Env var ABSENT (standalone CLI, no server process): true no-op --
  Finding 1's resolution, temporal data stays entirely in-repo.
- Env var PRESENT (server-spawned refresh child, any storage mode): after
  a quarter shard's normal in-repo write+finalize (unchanged, happens
  BEFORE this function is called), ALSO builds+publishes the SAME data to
  the sister location via AC6's three-branch dispatch.

golden_repos_dir/repo_alias derivation: temporal indexing ONLY ever runs
against a golden repo's own clone directly (AC12 explicitly rejects
"temporal" in activated-repo reindex requests), so codebase_dir.name IS
the golden repo's own alias and codebase_dir.parent IS golden_repos_dir --
the SAME derivation already confirmed twice this story for the server
QUERY side (SemanticQueryManager's is_global branch, AC1/AC2 live wiring).

Known, accepted redundancy (NOT a correctness bug): on Branch B-bootstrap
(pointer absent, in-repo legacy rows exist), legacy_row_reader scans the
SAME local_shard_dir new_delta_rows is filtered from, so a just-written
new commit's row is passed to build_fresh_consolidated_temporal_version
via BOTH row_sources. ChunkStore.write_batch is INSERT OR REPLACE (keyed
by point_id) so this is idempotent -- correct, merely a mildly wasteful
duplicate write, not a data-corruption or double-counting risk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.storage.postgres.temporal_child_wiring import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
    CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.services.temporal.temporal_refresh_dispatch import (
    execute_temporal_refresh_branch,
)
from code_indexer.services.temporal.temporal_row_reader import (
    read_legacy_shard_rows,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
    parse_physical_temporal_name,
)

logger = logging.getLogger(__name__)

# Safety gate (2026-07-23 code review, both Claude and Codex reviewers,
# independently recommended): AC1's relocation trigger must be explicit
# opt-in, disabled by default -- mirroring Story #1456's
# CIDX_CHUNKS_DB_NEW_COLLECTIONS pattern exactly. Without this gate the
# trigger fires on every normal server refresh cycle in ANY environment
# running this branch, publishing sister versions while several
# correctness gaps (disconnected reader, AC3's missing PG re-key, no
# read-back verification before publish) are still being fixed.
#
# 2026-07-24 re-review (Codex finding #4): CIDX_TEMPORAL_SISTER_RELOCATION_
# ENABLED_ENV's canonical definition now lives in temporal_child_wiring.py
# (alongside CIDX_SERVER_REFRESH_CONTEXT_ENV) so build_temporal_child_env
# (PARENT side, running in the live server process) can set it from the
# config service's resolved value -- this module (running in the CHILD
# subprocess, no DB access) still only ever READS the env var, unchanged.


def _parse_temporal_sister_relocation_enabled_env() -> bool:
    """Defaults to False (true no-op, byte-identical to pre-AC1 behavior)
    unless CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED is set to a truthy
    value. Parsed fresh (not cached) so tests can monkeypatch the env var
    per-test."""
    return os.environ.get(
        CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV, ""
    ).strip().lower() in ("1", "true", "yes")


def maybe_relocate_shard_to_sister_location(
    codebase_dir: Path,
    shard_name: str,
    local_shard_dir: Path,
    new_commit_hashes: Iterable[str],
    vector_dim: int,
    force_rebuild: bool = False,
) -> None:
    """AC1's relocation trigger for ONE quarter shard.

    Call this immediately AFTER the shard's normal in-repo write+finalize
    completes (unchanged) -- this function ADDITIONALLY builds+publishes
    the same data to the sister location when in server context AND the
    CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED safety gate is explicitly on.

    Args:
        codebase_dir: The golden repo's own clone directory (temporal
            indexing never runs against an activated repo directly).
        shard_name: Physical collection name, e.g.
            "code-indexer-temporal-voyage_code_3-2024Q1".
        local_shard_dir: The in-repo shard directory just finalized.
        new_commit_hashes: Commit hashes processed in THIS refresh run
            for this shard (used to filter local_shard_dir's rows down to
            just this run's delta).
        vector_dim: Expected vector dimension.
        force_rebuild: Story #1457 HIGH #11 (2026-07-23 code review). Pass
            the caller's local-repair signal (temporal_indexer.py's
            `was_stale`, Bug #1407's stale-lifecycle barrier) through so a
            locally-repaired shard's sister HNSW index is rebuilt on
            republish even when this run's own new_commit_hashes is empty.
    """
    if not os.environ.get(CIDX_SERVER_REFRESH_CONTEXT_ENV):
        return

    if not _parse_temporal_sister_relocation_enabled_env():
        # Disabled by default (2026-07-23 code review safety gate) --
        # true no-op, byte-identical to pre-AC1 behavior, until this is
        # explicitly enabled.
        return

    parsed = parse_physical_temporal_name(shard_name)
    if parsed is None:
        logger.warning(
            "maybe_relocate_shard_to_sister_location: '%s' does not parse "
            "as a temporal collection name -- skipping relocation trigger "
            "for this shard.",
            shard_name,
        )
        return
    embedder_slug, quarter = parsed

    codebase_dir = Path(codebase_dir)
    # 2026-07-23 code review HIGH #7 (global alias namespace mismatch),
    # defense-in-depth symmetry: normalize exactly one trailing '-global'
    # suffix here too, so the publish side can never drift from the
    # query side's OWN normalization (semantic_query_manager.py's
    # _execute_temporal_query) even in an unrealistic future case.
    repo_alias = codebase_dir.name.removesuffix("-global")
    golden_repos_dir = codebase_dir.parent
    legacy_index_path = codebase_dir / ".code-indexer" / "index"

    alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=golden_repos_dir,
        legacy_index_path=legacy_index_path,
    )
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(golden_repos_dir))

    new_hashes = set(new_commit_hashes)
    # 2026-07-23 code review CRITICAL #4: fail_on_corrupt=True on BOTH
    # uses of the reader below -- a corrupt/unreadable row must fail this
    # publish loudly, never be silently dropped into an incomplete
    # published version.
    new_delta_rows: List[Dict[str, Any]] = [
        row
        for row in read_legacy_shard_rows(local_shard_dir, fail_on_corrupt=True)
        if row.get("payload", {}).get("commit_hash") in new_hashes
    ]

    def _strict_legacy_row_reader(shard_dir: Path) -> Iterable[Dict[str, Any]]:
        return list(read_legacy_shard_rows(shard_dir, fail_on_corrupt=True))

    execute_temporal_refresh_branch(
        resolver=resolver,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=golden_repos_dir,
        embedder_slug=embedder_slug,
        quarter=quarter,
        new_delta_rows=new_delta_rows,
        legacy_row_reader=_strict_legacy_row_reader,
        vector_dim=vector_dim,
        force_rebuild=force_rebuild,
    )
