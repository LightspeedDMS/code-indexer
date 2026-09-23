"""GoldenRepoMetadataBackend Protocol (Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class GoldenRepoMetadataBackend(Protocol):
    """Protocol for golden repository metadata storage."""

    # Bug #1533: every implementation must state which kind of store it is --
    # True for the SHARED, cross-node store (PostgreSQL), False for a
    # NODE-LOCAL one (SQLite). Callers whose correctness depends on the
    # cluster-wide view check this and must refuse to read a node-local store;
    # they compare with `is True`, so an implementation that omits the
    # declaration is treated as node-local rather than silently trusted.
    is_shared_backend: bool

    def ensure_table_exists(self) -> None: ...

    def add_repo(
        self,
        alias: str,
        repo_url: str,
        default_branch: str,
        clone_path: str,
        created_at: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict] = None,
    ) -> None: ...

    def get_repo(self, alias: str) -> Optional[Dict[str, Any]]: ...

    def list_repos(self) -> List[Dict[str, Any]]: ...

    def remove_repo(self, alias: str) -> bool: ...

    def repo_exists(self, alias: str) -> bool: ...

    def update_enable_temporal(self, alias: str, enable: bool) -> bool: ...

    # Bug #1414: temporal_options is the Web UI's sole write target
    # (GoldenRepoManager.save_temporal_options); every concrete backend
    # MUST implement this or the Web UI save silently AttributeErrors in
    # cluster mode (Any-typed injection site defeats mypy).
    def update_temporal_options(self, alias: str, options: Optional[Dict]) -> bool: ...

    def update_repo_url(self, alias: str, repo_url: str) -> bool: ...

    def update_category(
        self, alias: str, category_id: Optional[int], auto_assigned: bool = True
    ) -> bool: ...

    def update_wiki_enabled(self, alias: str, enabled: bool) -> None: ...

    def update_default_branch(self, alias: str, branch: str) -> None: ...

    def invalidate_description_refresh_tracking(self, alias: str) -> None: ...

    def invalidate_dependency_map_tracking(self, alias: str) -> None: ...

    def list_repos_with_categories(self) -> List[Dict[str, Any]]: ...

    # Bug #1382: registry-reconcile circuit-breaker cross-restart
    # confirmation state (see golden_repo_reconciler.py).
    def record_reconcile_breaker_observation(self, fingerprint: str) -> int: ...

    def reset_reconcile_breaker_state(self) -> None: ...

    def get_reconcile_breaker_state(self) -> Optional[Dict[str, Any]]: ...

    # Issue #1383: persistent, discoverable trace of a confirmed
    # registry-reconcile auto-removal event, surviving the breaker-state
    # reset above (see golden_repo_reconciler.py / health_service.py).
    def record_reconcile_auto_heal_event(self, removed_aliases: List[str]) -> None: ...

    def get_reconcile_auto_heal_event(self) -> Optional[Dict[str, Any]]: ...

    # Issue #1477: fleet-migration per-repo failure quarantine state (see
    # server/services/fleet_migration/quarantine.py).
    def record_fleet_migration_failure(
        self,
        golden_alias: str,
        state_signature: str,
        failure_cause: Optional[str] = None,
    ) -> int: ...

    def reset_fleet_migration_failure(self, golden_alias: str) -> None: ...

    # Issue #1477 Finding N: fallback used when the full reset (DELETE)
    # above fails but a plain UPDATE still works -- zeroes
    # consecutive_failure_count while keeping the row, so a just-repaired
    # repo gets a genuinely fresh failure budget instead of resuming from
    # a stale, elevated count (see quarantine.py's
    # _clear_quarantine_after_detected_repair()).
    def soft_reset_fleet_migration_failure_count(self, golden_alias: str) -> None: ...

    def touch_fleet_migration_failure_check(self, golden_alias: str) -> None: ...

    def get_fleet_migration_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]: ...

    def list_fleet_migration_failure_states(self) -> List[Dict[str, Any]]: ...

    # Bug #1506: ordinary-refresh integrity-gate per-repo failure
    # quarantine state (see global_repos/refresh_integrity_gate.py).
    def record_refresh_integrity_failure(
        self, golden_alias: str, detail: str
    ) -> int: ...

    def reset_refresh_integrity_failure(self, golden_alias: str) -> None: ...

    def get_refresh_integrity_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]: ...

    # Bug #1769: local-repo `cidx init` repair per-repo failure
    # quarantine state (see global_repos/refresh_scheduler.py's
    # _repair_uninitialized_local_repo()).
    def record_local_repo_repair_failure(
        self, golden_alias: str, detail: str
    ) -> int: ...

    def reset_local_repo_repair_failure(self, golden_alias: str) -> None: ...

    def get_local_repo_repair_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]: ...

    # Story #1560: per-golden-alias duplicate-point-id auto-resolution
    # outcome state (see server/services/fleet_migration/dedup_state.py).
    # A NEW table, distinct from fleet_migration_quarantine_state above --
    # that one means "this repo keeps failing"; this one means "this
    # repo migrated successfully but permanently lost N records".
    def record_dedup_outcome(
        self,
        golden_alias: str,
        *,
        duplicate_groups: int,
        records_before: int,
        records_deleted: int,
        winner_kept_groups: int,
        whole_group_deleted_groups: int,
        collection_total: int,
    ) -> Dict[str, Any]: ...

    def get_dedup_state(self, golden_alias: str) -> Optional[Dict[str, Any]]: ...

    def list_dedup_states(self) -> List[Dict[str, Any]]: ...

    def clear_dedup_state(self, golden_alias: str, reason: str) -> None: ...

    # Story #1589: bulk-clear EVERY currently-active dedup-outcome row in
    # one shot -- the Diagnostics tab's "Clear All Dedup Warnings" action.
    # Returns the number of rows actually cleared.
    def clear_all_dedup_states(self, reason: str) -> int: ...

    # Bug #1539's cidx-meta backup conflict-resolution per-repo failure
    # quarantine state (record_cidx_meta_conflict_failure /
    # reset_cidx_meta_conflict_failure / get_cidx_meta_conflict_failure_state)
    # is RETIRED as of Bug #1555's root-cause fix: CidxMetaBackupSync.sync()
    # is now a plain mirror-push that can never raise
    # ConflictResolutionFailedError, so nothing ever calls these again. See
    # cidx_meta_backup/sync.py's module docstring for the design rationale.

    # Bug #1567: durable pending-deletion queue for versioned-snapshot
    # cleanup (see global_repos/cleanup_manager.py). Backs the in-process
    # queue that a restart/worker-recycle previously discarded silently,
    # using a WALL-CLOCK timestamp (never time.monotonic(), which has no
    # cross-process meaning) so the minimum-retention-age floor survives a
    # restart.
    def schedule_cleanup_deletion(
        self, index_path: str, scheduled_at: float
    ) -> float: ...

    def list_cleanup_pending_deletions(self) -> List[Dict[str, Any]]: ...

    def remove_cleanup_pending_deletion(self, index_path: str) -> None: ...

    def close(self) -> None: ...
