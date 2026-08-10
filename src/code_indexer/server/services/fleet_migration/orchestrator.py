"""run_fleet_migration_for_repo() -- Story #1458 AC1/AC1a/AC2/AC8/AC9/AC10.

The per-repo fleet-migration job: consolidates one golden repo's MUTABLE
BASE CLONE in place. Executes, in order, under a SINGLE held write lock:

  (1) consolidate every SEMANTIC collection (Story #1458 AC3, via
      ``consolidate_collection_in_place``);
  (2) consolidate every in-repo TEMPORAL shard IN PLACE via that SAME
      engine (Bug #1528 -- previously this published each namespace to the
      Story #1457 "sister location" instead) -- a literal sub-step of THIS
      job, inside the SAME write-lock hold, not an independently-scheduled
      operation;
  (3) only after BOTH complete AND the completion gate (every real in-repo
      temporal shard verified fully consolidated, AND no collection skipped
      for insufficient disk) passes, fire the single post-consolidation
      snapshot (AC10), exactly once for the repo.

AC2: the write lock is acquired BEFORE touching the base clone and released
in ``finally``, held continuously through the whole sequence including the
AC10 trigger.

AC8: the lock is acquired with an explicit long, migration-specific TTL
(never the base 3600s default) so it cannot expire out from under a
genuinely long-running migration (approach (a) of AC8's two allowed
approaches).

AC9: after acquiring the lock, migration ALSO verifies no
``global_repo_refresh`` job is currently active for this repo (the write
lock alone does not exclude an ALREADY-running refresh -- a real TOCTOU gap,
see ``RefreshScheduler.check_refresh_not_in_progress``'s own docstring). If
one is found, migration releases the lock and returns immediately without
touching the base clone -- retry is via re-invocation on a later scheduling
pass, never an in-call blocking/polling wait (this codebase's established
fail-fast convention for job conflicts, e.g. AC7's activation-vs-migration
case).

AC7 (activation-during-migration fail-fast) requires NO code here at all:
it is inherited "for free" because migration acquires the SAME
``WriteLockManager`` lock file activation's existing non-blocking
``acquire()`` call already checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.fleet_migration.completion_gate import (
    mark_post_consolidation_snapshot_published,
    repo_temporal_dirs_fully_consolidated,
)
from code_indexer.server.services.fleet_migration.snapshot_trigger import (
    trigger_post_consolidation_snapshot,
)
from code_indexer.server.services.query_path_cache import (
    is_immutable_versioned_snapshot,
)
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)

if TYPE_CHECKING:
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

logger = logging.getLogger(__name__)

#: Owner identity recorded in the WriteLockManager lock file (AC2/AC7).
MIGRATION_OWNER_NAME = "fleet_migration"

#: AC8 approach (a): an explicit, migration-specific TTL that comfortably
#: exceeds the realistic worst-case migration duration for the largest
#: fleet repo. Justification: the largest known production golden repo
#: (the epic's own worked example, "evolution") has on the order of
#: millions of chunk files; even at a conservative sustained throughput of
#: several hundred chunks/sec for the streaming read+write+verify sequence,
#: a full single-repo consolidation pass is expected to complete in well
#: under a few hours. 24 hours is a wide, deliberately generous margin
#: above that estimate while still being a bounded, auto-recoverable TTL
#: (never an unbounded/forever lock) if the owning process genuinely dies.
MIGRATION_LOCK_TTL_SECONDS = 24 * 60 * 60

_GLOBAL_SUFFIX = "-global"


class FleetMigrationSymlinkRaceError(Exception):
    """Raised when a semantic collection directory that passed
    discovery-time symlink validation is found to be a symlink -- or to
    resolve into an immutable ``.versioned/`` snapshot -- at the moment
    consolidation is about to run against it (Story #1458 round-6 Codex
    CRITICAL finding #4).

    ``discovery.py``'s own ``is_symlink()`` rejection only proves the
    directory was NOT a symlink AT DISCOVERY TIME; a concurrent process
    can still swap a real directory for a symlink between discovery and
    this later, destructive write. This re-validation -- performed
    immediately before the destructive operation and under the
    migration's own write lock -- narrows, but does not fully eliminate,
    that TOCTOU race window (a true fix would anchor every subsequent
    open/write to an already-opened directory descriptor via
    openat/O_NOFOLLOW semantics; that structural rewrite is out of scope
    for this fix).
    """


@dataclass(frozen=True)
class TemporalNamespaceSpec:
    """One in-repo temporal (embedder, quarter) namespace to bootstrap."""

    pointer_namespace: str
    legacy_shard_dir: Path
    embedder_slug: str


@dataclass
class FleetMigrationRepoResult:
    """Outcome of one :func:`run_fleet_migration_for_repo` call.

    status:
        - "completed": the full per-repo pass succeeded, including the
          AC10 post-consolidation snapshot.
        - "incomplete": semantic consolidation and/or temporal bootstrap
          ran, but the completion gate did not pass (a collection was
          skipped for insufficient disk, and/or a residual in-repo
          temporal directory remains) -- AC10 did NOT fire.
        - "refresh_in_flight": AC9's JobTracker check found an active
          refresh job for this repo; the base clone was NOT touched at
          all. Retry via re-invocation on a later scheduling pass.
        - "lock_held": AC2's write lock was already held (by migration
          itself or another writer); the base clone was NOT touched.
        - "refused_immutable_path": defense-in-depth -- base_clone_path
          resolves into the IMMUTABLE .versioned/ snapshot tree; the base
          clone was NOT touched and the write lock was NEVER acquired.
    """

    status: str
    collections_consolidated: int = 0
    collections_skipped_disk: int = 0
    temporal_namespaces_processed: int = 0
    snapshot_path: Optional[str] = None
    detail: str = ""


def _bare_alias(alias: str) -> str:
    return alias[: -len(_GLOBAL_SUFFIX)] if alias.endswith(_GLOBAL_SUFFIX) else alias


def _consolidate_collections(
    collection_dirs: List[Path],
    *,
    deletion_authorized: bool = True,
) -> Tuple[int, int]:
    """AC1 step (1). Returns (consolidated_count, skipped_disk_count).

    Bug #1528: kind-agnostic. TEMPORAL shard directories now flow through
    this SAME helper -- and therefore the same symlink/immutable-path
    re-validation and the same in-place engine -- as semantic collections,
    instead of being published to a separate "sister" location by a
    parallel mechanism.

    ``deletion_authorized`` is Story #1460's AC1/AC2 rollout-safety gate,
    threaded straight through to ``consolidate_collection_in_place`` --
    see that function's own docstring for the full semantics.
    """
    consolidated_count = 0
    skipped_disk_count = 0
    for collection_dir in collection_dirs:
        # Codex round-6 CRITICAL finding #4 (TOCTOU): discovery.py's own
        # is_symlink() rejection only proves this path was safe AT
        # DISCOVERY TIME -- re-validate immediately before the
        # destructive write, right here under the migration's own write
        # lock, so a symlink swapped in between discovery and this call
        # is refused rather than silently followed into (and then
        # destructively written into / deleted from) an immutable
        # .versioned/ snapshot.
        if collection_dir.is_symlink() or is_immutable_versioned_snapshot(
            str(collection_dir.resolve())
        ):
            raise FleetMigrationSymlinkRaceError(
                f"Refusing to consolidate {collection_dir}: it is a "
                f"symlink and/or resolves to an IMMUTABLE .versioned/ "
                f"snapshot -- this directory passed discovery-time "
                f"validation but changed before this destructive write "
                f"could run."
            )
        result = consolidate_collection_in_place(
            collection_dir, deletion_authorized=deletion_authorized
        )
        if result.status in ("consolidated", "already_consolidated"):
            consolidated_count += 1
        elif result.status == "skipped_insufficient_disk":
            skipped_disk_count += 1
    return consolidated_count, skipped_disk_count


def _consolidate_temporal_namespaces(
    temporal_namespaces: List[TemporalNamespaceSpec],
    sister_alias_manager: AliasManager,
    sister_root: Path,
    *,
    deletion_authorized: bool = True,
) -> Tuple[int, int]:
    """AC1 step (2), Bug #1528 revision: consolidate every in-repo temporal
    shard IN PLACE, synchronously, inside THIS same write-lock hold, as a
    literal sub-step of this job.

    Previously this published each namespace to the Story #1457 "sister
    location" (``bootstrap_temporal_namespace_to_sister``): a second
    consolidated copy elsewhere, an alias pointer to it, and only then
    reclamation of the in-repo tree. That was a parallel migration system
    for temporal alone, which existed only because temporal was excluded
    from the CHUNKS_DB write path. With that exclusion gone, temporal is
    migrated by the ONE engine semantic collections already use -- same
    directory, discriminator flip, legacy files deleted only after a
    verified durable write.

    Returns ``(consolidated_count, skipped_disk_count)`` from that shared
    helper; ``deletion_authorized`` is Story #1460's AC1/AC2 rollout-safety
    gate, threaded straight through to ``consolidate_collection_in_place``.

    A namespace that ALREADY has a sister alias pointer (published by the
    retired mechanism before this fix) is still consolidated in place, but
    is reported as an operator-actionable WARNING: ``TemporalShardResolver``
    is pointer-first, so reads for that namespace keep resolving to the
    previously-published sister copy rather than to the shard just
    consolidated here.
    """
    for spec in temporal_namespaces:
        if sister_alias_manager.alias_exists(spec.pointer_namespace):
            logger.warning(
                "fleet migration: temporal namespace '%s' still has a "
                "legacy sister alias pointer under %s -- consolidating the "
                "in-repo shard %s in place, but pointer-first query "
                "resolution will keep reading the previously-published "
                "sister copy for this namespace until that pointer is "
                "retired",
                spec.pointer_namespace,
                sister_root,
                spec.legacy_shard_dir,
            )

    consolidatable: List[Path] = []
    for spec in temporal_namespaces:
        if not (spec.legacy_shard_dir / "collection_meta.json").is_file():
            # No metadata file means the in-place engine has nothing to flip
            # a chunks_db discriminator in and would raise, so this
            # directory is skipped rather than aborting the whole pass. A
            # genuine ROWLESS "empty artifact" (Story #1458 AC1a) is
            # unremarkable; one that still holds legacy rows is an
            # un-migratable anomaly that will keep the completion gate
            # closed, so the operator must see it.
            has_legacy_rows = (
                next(spec.legacy_shard_dir.rglob("vector_*.json"), None) is not None
            )
            if has_legacy_rows:
                logger.warning(
                    "fleet migration: temporal shard %s holds legacy "
                    "vector_*.json rows but has NO collection_meta.json -- "
                    "it cannot be consolidated in place, so this repo will "
                    "stay incomplete until the directory is repaired or "
                    "removed",
                    spec.legacy_shard_dir,
                )
            else:
                logger.info(
                    "fleet migration: temporal shard %s is a rowless "
                    "artifact with no collection_meta.json -- nothing to "
                    "consolidate, skipping",
                    spec.legacy_shard_dir,
                )
            continue
        consolidatable.append(spec.legacy_shard_dir)

    return _consolidate_collections(
        consolidatable,
        deletion_authorized=deletion_authorized,
    )


def _run_migration_sequence(
    refresh_scheduler: "RefreshScheduler",
    sister_alias_manager: AliasManager,
    bare_alias: str,
    base_clone_path: Path,
    index_path: Path,
    semantic_collection_dirs: List[Path],
    temporal_namespaces: List[TemporalNamespaceSpec],
    sister_root: Path,
    *,
    deletion_authorized: bool = True,
) -> FleetMigrationRepoResult:
    """AC1 steps (1)-(3), run under the ALREADY-acquired write lock and
    AFTER AC9's in-flight-refresh check has already passed.

    ``deletion_authorized`` is Story #1460's AC1/AC2 rollout-safety gate --
    when False, the DESTRUCTIVE legacy-file deletion step is withheld for
    both collection kinds while the non-destructive build/verify/flip work
    still runs (see ``consolidate_collection_in_place``'s docstring). The
    completion gate below naturally refuses to fire AC10's snapshot in that
    case, since withheld deletion always leaves legacy sharded files behind
    and therefore a not-yet-fully-migrated collection.
    """
    consolidated_count, skipped_disk_count = _consolidate_collections(
        semantic_collection_dirs, deletion_authorized=deletion_authorized
    )
    temporal_consolidated, temporal_skipped_disk = _consolidate_temporal_namespaces(
        temporal_namespaces,
        sister_alias_manager,
        sister_root,
        deletion_authorized=deletion_authorized,
    )
    consolidated_count += temporal_consolidated
    skipped_disk_count += temporal_skipped_disk

    # AC1 step (3) / AC10: Bug #1528 -- temporal shards are migrated IN
    # PLACE, so their directories legitimately remain; the gate is that
    # every real temporal shard verifies as fully consolidated to
    # chunks.db, AND no collection was left unconsolidated for lack of disk
    # headroom. Either failure means this repo is not yet fully
    # consolidated, so the snapshot must not fire.
    temporal_complete = repo_temporal_dirs_fully_consolidated(index_path)
    if skipped_disk_count > 0 or not temporal_complete:
        detail = (
            "one or more collections were skipped for insufficient disk headroom"
            if skipped_disk_count > 0
            else "one or more in-repo temporal shards are still in the "
            "legacy sharded layout"
        )
        return FleetMigrationRepoResult(
            status="incomplete",
            collections_consolidated=consolidated_count,
            collections_skipped_disk=skipped_disk_count,
            temporal_namespaces_processed=len(temporal_namespaces),
            detail=detail,
        )

    # Codex round-6 HIGH finding #7: the disk-skip/temporal-absence
    # checks above prove nothing about whether each collection was
    # GENUINELY, verifiably migrated -- consolidate_collection_in_place's
    # returned status alone is not proof (e.g. finding #5's duplicate-ID
    # bug could return "consolidated" despite lost/residual data). Re-run
    # the full, fresh verification oracle on every semantic collection
    # immediately before the snapshot fires -- never trust a status
    # string alone at the one point where the result becomes durably
    # published and query-visible.
    for collection_dir in semantic_collection_dirs:
        if not verify_collection_fully_migrated(collection_dir):
            return FleetMigrationRepoResult(
                status="incomplete",
                collections_consolidated=consolidated_count,
                collections_skipped_disk=skipped_disk_count,
                temporal_namespaces_processed=len(temporal_namespaces),
                detail=(
                    f"fresh re-verification failed immediately before "
                    f"the snapshot trigger for collection "
                    f"{collection_dir} -- refusing to publish"
                ),
            )

    # Issue #1546 AC5: ownership-loss checkpoint immediately before the
    # migration's work becomes durably published and query-visible --
    # the last chance to detect the write lock is no longer legitimately
    # held before firing the AC10 snapshot trigger.
    refresh_scheduler.raise_if_write_lock_ownership_lost(
        bare_alias, owner_name=MIGRATION_OWNER_NAME
    )
    snapshot_path = trigger_post_consolidation_snapshot(
        refresh_scheduler, bare_alias, str(base_clone_path)
    )
    # New CRITICAL finding: durably record "snapshot published" as a
    # DISTINCT state from "consolidation done" -- only reached after the
    # trigger call above has already returned successfully, so a crash
    # mid-trigger leaves this marker absent and the next migration pass
    # simply retries (idempotent).
    mark_post_consolidation_snapshot_published(index_path)

    return FleetMigrationRepoResult(
        status="completed",
        collections_consolidated=consolidated_count,
        collections_skipped_disk=skipped_disk_count,
        temporal_namespaces_processed=len(temporal_namespaces),
        snapshot_path=snapshot_path,
    )


def run_fleet_migration_for_repo(
    *,
    refresh_scheduler: "RefreshScheduler",
    sister_alias_manager: AliasManager,
    repo_alias: str,
    base_clone_path: Path,
    index_path: Path,
    semantic_collection_dirs: List[Path],
    temporal_namespaces: List[TemporalNamespaceSpec],
    sister_root: Path,
    deletion_authorized: Optional[bool] = None,
) -> FleetMigrationRepoResult:
    """Run the full per-repo fleet-migration pass for one golden repo.

    Args:
        refresh_scheduler: The REAL RefreshScheduler used to acquire/hold
            the write lock (AC2) and to fire the AC10 snapshot trigger.
        sister_alias_manager: AliasManager scoped to the sister location's
            aliases directory (distinct from ``refresh_scheduler.
            alias_manager``, scoped to the golden-repos aliases directory).
        repo_alias: The golden repo's alias, with or without "-global".
        base_clone_path: The MUTABLE BASE CLONE path to consolidate and,
            on success, to snapshot (AC10).
        index_path: The repo's ``.code-indexer/index/`` directory, checked
            by the AC1/AC10 completion gate.
        semantic_collection_dirs: Every semantic collection directory in
            the base clone to consolidate (AC3, step 1 of AC1's ordering).
        temporal_namespaces: Every in-repo temporal (embedder, quarter)
            namespace to bootstrap to the sister location (Story #1457
            AC11, step 2 of AC1's ordering).
        sister_root: Root directory under which sister-location published
            temporal versions live.
        deletion_authorized: Story #1460 AC1/AC2 rollout-safety gate.
            Explicit True/False always wins (test/admin-tool override).
            None (the default) resolves from the operator-controlled,
            ``get_config_service()``-backed ``fleet_migration_config.
            enabled`` flag (default OFF, Web-UI-configurable -- never an
            env var) at call time, immediately before it is needed --
            genuine defense-in-depth so ANY caller of this function
            (not just ``FleetMigrationScheduler``, the only production
            caller today) gets the real, fail-closed config value rather
            than silently deleting unconditionally.

    Returns:
        A :class:`FleetMigrationRepoResult` describing the outcome.
    """
    bare_alias = _bare_alias(repo_alias)

    # Defense-in-depth (Codex round-2 follow-up): discovery.py already
    # skips a candidate whose base_clone_path resolves to an immutable
    # .versioned/ snapshot, but the destructive engine itself must ALSO
    # refuse -- never trust the caller alone. Checked BEFORE the write
    # lock is even acquired, so an immutable path never touches locking
    # state either.
    if is_immutable_versioned_snapshot(str(Path(base_clone_path).resolve())):
        return FleetMigrationRepoResult(
            status="refused_immutable_path",
            detail=(
                f"base_clone_path {base_clone_path} resolves to an "
                f"IMMUTABLE .versioned/ snapshot -- migration only ever "
                f"targets the mutable base clone; refusing to touch it"
            ),
        )

    # AC2 + AC8: acquire BEFORE touching the base clone, with a migration-
    # specific long TTL (bypassing RefreshScheduler.acquire_write_lock's
    # wrapper, which hardcodes the base 3600s default) so the lock cannot
    # expire out from under a genuinely long-running migration.
    lock_acquired = refresh_scheduler.write_lock_manager.acquire(
        bare_alias,
        owner_name=MIGRATION_OWNER_NAME,
        ttl_seconds=MIGRATION_LOCK_TTL_SECONDS,
    )
    if not lock_acquired:
        return FleetMigrationRepoResult(
            status="lock_held",
            detail=f"write lock for {bare_alias!r} is already held by another writer",
        )

    try:
        # AC9: close the refresh-vs-migration TOCTOU gap. The write lock
        # alone does not exclude a refresh that was ALREADY running before
        # migration's acquire call.
        try:
            refresh_scheduler.check_refresh_not_in_progress(bare_alias)
        except DuplicateJobError as exc:
            return FleetMigrationRepoResult(
                status="refresh_in_flight",
                detail=(
                    f"a global_repo_refresh job ({exc.existing_job_id}) is "
                    f"active for {bare_alias!r}; migration did not touch "
                    f"the base clone -- retry on a later scheduling pass"
                ),
            )

        # Story #1460 AC1/AC2: resolve the rollout-safety gate ONLY here,
        # immediately before it is needed -- an explicit override always
        # wins; otherwise fall back to the SAME operator-controlled,
        # get_config_service()-backed flag Story #1458 already wired
        # through the Web UI Config Screen. Deferred this late (never at
        # function entry) so the lock_held/refresh_in_flight/immutable
        # -path early-return paths above never touch the config service.
        resolved_deletion_authorized = (
            deletion_authorized
            if deletion_authorized is not None
            else bool(get_config_service().get_config().fleet_migration_config.enabled)
        )

        return _run_migration_sequence(
            refresh_scheduler,
            sister_alias_manager,
            bare_alias,
            base_clone_path,
            index_path,
            semantic_collection_dirs,
            temporal_namespaces,
            sister_root,
            deletion_authorized=resolved_deletion_authorized,
        )
    finally:
        # AC2: held continuously through the whole sequence above,
        # released here regardless of outcome (success, incomplete, or
        # exception) -- never left dangling.
        refresh_scheduler.release_write_lock(
            bare_alias, owner_name=MIGRATION_OWNER_NAME
        )
