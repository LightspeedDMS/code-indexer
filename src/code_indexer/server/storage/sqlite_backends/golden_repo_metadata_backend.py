"""
SQLite backend for golden repository metadata (Story #711).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
The original class body (1,423 lines) exceeds the project's 1,000-line-per-
file limit even after being moved into its own module, so it is composed
via plain multiple inheritance from _GoldenRepoMetadataExtraMixin (see
_golden_repo_metadata_extra_mixin.py) -- a structural, behaviour-preserving
split; this file owns __init__/the connection manager and the "core" CRUD
methods, the mixin owns the remaining quarantine/dedup/cleanup methods.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager
from ._golden_repo_metadata_extra_mixin import _GoldenRepoMetadataExtraMixin

logger = logging.getLogger(__name__)


class GoldenRepoMetadataSqliteBackend(_GoldenRepoMetadataExtraMixin):
    """
    SQLite backend for golden repository metadata (Story #711).

    Replaces golden-repos/metadata.json with atomic SQLite operations,
    eliminating race conditions from concurrent access.
    """

    # Bug #1533: NODE-LOCAL storage. On a cluster node this store cannot see
    # repos registered by any other node, so callers whose correctness depends
    # on the cluster-wide view must refuse to read it. Declared explicitly so
    # they can ask what this backend IS, rather than inferring "shared" from
    # the fact that some backend was injected.
    is_shared_backend = False

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def ensure_table_exists(self) -> None:
        """Ensure the golden_repos_metadata table exists (idempotent)."""

        def operation(conn):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                    alias TEXT PRIMARY KEY NOT NULL,
                    repo_url TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    clone_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enable_temporal INTEGER NOT NULL DEFAULT 0,
                    temporal_options TEXT,
                    category_id INTEGER,
                    category_auto_assigned INTEGER DEFAULT 0,
                    wiki_enabled INTEGER DEFAULT 0
                )
            """
            )
            _migrate_golden_repos_metadata_columns(conn)
            _create_golden_repo_metadata_support_tables(conn)

        self._conn_manager.execute_atomic(operation)

    # Bug #1539's record_cidx_meta_conflict_failure /
    # reset_cidx_meta_conflict_failure / get_cidx_meta_conflict_failure_state
    # are RETIRED as of Bug #1555's root-cause fix: CidxMetaBackupSync.sync()
    # is now a plain mirror-push that can never raise
    # ConflictResolutionFailedError, so nothing ever calls these again. The
    # cidx_meta_conflict_quarantine_state table (ensure_table_exists above)
    # is left in place per this project's backward-compatible-migrations
    # rule -- it simply gains no new rows.

    def record_refresh_integrity_failure(self, golden_alias: str, detail: str) -> int:
        """
        Record one ordinary-refresh integrity-gate failure for a golden
        repo (Bug #1506). ``detail`` (the integrity_check/flush failure
        text) is ALWAYS overwritten to the value supplied for THIS
        failure.

        Returns:
            The consecutive-failure count after recording this one.

        Raises:
            ValueError: golden_alias or detail is empty/blank (Codex review
                Finding 4: matches GoldenRepoMetadataPostgresBackend's
                validation exactly -- an empty alias/detail is a caller
                bug that must fail loud, not be silently stored). The
                actual increment is already atomic via ``execute_atomic``'s
                ``BEGIN EXCLUSIVE`` transaction below (SQLite has no
                ``INSERT ... ON CONFLICT ... RETURNING`` in this
                environment's SQLite 3.34.1, but ``BEGIN EXCLUSIVE``
                already serializes the read-then-update against any
                concurrent writer for the whole transaction).
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")

        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            row = conn.execute(
                "SELECT consecutive_failure_count "
                "FROM refresh_integrity_quarantine_state WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO refresh_integrity_quarantine_state "
                    "(golden_alias, consecutive_failure_count, last_detail, "
                    "first_failed_at, last_failed_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (golden_alias, 1, detail, now, now, now),
                )
                return 1

            new_count = row[0] + 1
            conn.execute(
                "UPDATE refresh_integrity_quarantine_state "
                "SET consecutive_failure_count = ?, last_detail = ?, "
                "last_failed_at = ?, updated_at = ? "
                "WHERE golden_alias = ?",
                (new_count, detail, now, now, golden_alias),
            )
            return new_count

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def reset_refresh_integrity_failure(self, golden_alias: str) -> None:
        """
        Clear any persisted refresh-integrity failure/quarantine state for
        a golden repo (Bug #1506) -- called on a successful integrity-gate
        pass. A no-op (never raises FOR AN UNKNOWN ALIAS) when no row
        exists for ``golden_alias``.

        Raises:
            ValueError: golden_alias is empty/blank (Codex review Finding
                4: matches GoldenRepoMetadataPostgresBackend).
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        def operation(conn):
            conn.execute(
                "DELETE FROM refresh_integrity_quarantine_state WHERE golden_alias = ?",
                (golden_alias,),
            )

        self._conn_manager.execute_atomic(operation)

    def get_refresh_integrity_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return the currently persisted refresh-integrity failure state for
        a golden repo, or None if it has never failed (or was reset since).

        Raises:
            ValueError: golden_alias is empty/blank (Codex review Finding
                4: matches GoldenRepoMetadataPostgresBackend).
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        conn = self._conn_manager.get_connection()
        row = conn.execute(
            "SELECT golden_alias, consecutive_failure_count, last_detail, "
            "first_failed_at, last_failed_at "
            "FROM refresh_integrity_quarantine_state WHERE golden_alias = ?",
            (golden_alias,),
        ).fetchone()
        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": row[1],
            "last_detail": row[2],
            "first_failed_at": row[3],
            "last_failed_at": row[4],
        }

    def record_local_repo_repair_failure(self, golden_alias: str, detail: str) -> int:
        """
        Record one local-repo `cidx init` repair failure for a golden repo
        (Bug #1769). ``detail`` (the repair subprocess's stderr/exception
        text) is ALWAYS overwritten to the value supplied for THIS
        failure.

        Returns:
            The consecutive-failure count after recording this one.

        Raises:
            ValueError: golden_alias or detail is empty/blank (matches
                record_refresh_integrity_failure's validation exactly).
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")

        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            row = conn.execute(
                "SELECT consecutive_failure_count "
                "FROM local_repo_repair_quarantine_state WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO local_repo_repair_quarantine_state "
                    "(golden_alias, consecutive_failure_count, last_detail, "
                    "first_failed_at, last_failed_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (golden_alias, 1, detail, now, now, now),
                )
                return 1

            new_count = row[0] + 1
            conn.execute(
                "UPDATE local_repo_repair_quarantine_state "
                "SET consecutive_failure_count = ?, last_detail = ?, "
                "last_failed_at = ?, updated_at = ? "
                "WHERE golden_alias = ?",
                (new_count, detail, now, now, golden_alias),
            )
            return new_count

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def reset_local_repo_repair_failure(self, golden_alias: str) -> None:
        """
        Clear any persisted local-repo repair failure/quarantine state for
        a golden repo (Bug #1769) -- called on a successful `cidx init`
        repair. A no-op (never raises FOR AN UNKNOWN ALIAS) when no row
        exists for ``golden_alias``.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        def operation(conn):
            conn.execute(
                "DELETE FROM local_repo_repair_quarantine_state WHERE golden_alias = ?",
                (golden_alias,),
            )

        self._conn_manager.execute_atomic(operation)

    def get_local_repo_repair_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return the currently persisted local-repo repair failure state for
        a golden repo, or None if it has never failed (or was reset
        since).

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        conn = self._conn_manager.get_connection()
        row = conn.execute(
            "SELECT golden_alias, consecutive_failure_count, last_detail, "
            "first_failed_at, last_failed_at "
            "FROM local_repo_repair_quarantine_state WHERE golden_alias = ?",
            (golden_alias,),
        ).fetchone()
        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": row[1],
            "last_detail": row[2],
            "first_failed_at": row[3],
            "last_failed_at": row[4],
        }

    def add_repo(
        self,
        alias: str,
        repo_url: str,
        default_branch: str,
        clone_path: str,
        created_at: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict] = None,
    ) -> None:
        """
        Add a new golden repository.

        Args:
            alias: Unique alias for the repository (primary key).
            repo_url: Git repository URL.
            default_branch: Default branch name.
            clone_path: Path to cloned repository.
            created_at: ISO 8601 timestamp when repository was created.
            enable_temporal: Whether temporal indexing is enabled.
            temporal_options: Optional temporal indexing options (stored as JSON).

        Raises:
            sqlite3.IntegrityError: When a repo with the same alias already exists.
                Callers must pre-check existence (e.g. repo_exists() or the in-memory
                golden_repos dict) before calling this method.  Race safety is provided
                by the outer bootstrap FileLock in service_init.py plus the callers'
                own pre-checks, not by silent OR IGNORE tolerance.
        """
        temporal_json = json.dumps(temporal_options) if temporal_options else None

        def operation(conn):
            conn.execute(
                """INSERT INTO golden_repos_metadata
                   (alias, repo_url, default_branch, clone_path, created_at,
                    enable_temporal, temporal_options)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    alias,
                    repo_url,
                    default_branch,
                    clone_path,
                    created_at,
                    1 if enable_temporal else 0,
                    temporal_json,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Added golden repo: {alias}")

    def get_repo(self, alias: str) -> Optional[Dict[str, Any]]:
        """
        Get golden repository details by alias.

        Args:
            alias: Alias of the repository to retrieve.

        Returns:
            Dictionary with repository details, or None if not found.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias, repo_url, default_branch, clone_path, created_at,
                      enable_temporal, temporal_options, category_id, category_auto_assigned,
                      COALESCE(wiki_enabled, 0)
               FROM golden_repos_metadata WHERE alias = ?""",
            (alias,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "alias": row[0],
            "repo_url": row[1],
            "default_branch": row[2],
            "clone_path": row[3],
            "created_at": row[4],
            "enable_temporal": bool(row[5]),
            "temporal_options": json.loads(row[6]) if row[6] else None,
            "category_id": row[7],
            "category_auto_assigned": bool(row[8]),
            "wiki_enabled": bool(row[9]),
        }

    def list_repos(self) -> List[Dict[str, Any]]:
        """
        List all golden repositories.

        Returns:
            List of repository dictionaries.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias, repo_url, default_branch, clone_path, created_at,
                      enable_temporal, temporal_options, COALESCE(wiki_enabled, 0)
               FROM golden_repos_metadata"""
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "alias": row[0],
                    "repo_url": row[1],
                    "default_branch": row[2],
                    "clone_path": row[3],
                    "created_at": row[4],
                    "enable_temporal": bool(row[5]),
                    "temporal_options": json.loads(row[6]) if row[6] else None,
                    "wiki_enabled": bool(row[7]),
                }
            )

        return result

    def remove_repo(self, alias: str) -> bool:
        """
        Remove a golden repository by alias.

        Args:
            alias: Alias of the repository to remove.

        Returns:
            True if a record was deleted, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM golden_repos_metadata WHERE alias = ?",
                (alias,),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Removed golden repo: {alias}")
        return deleted

    def repo_exists(self, alias: str) -> bool:
        """
        Check if a golden repository exists.

        Args:
            alias: Alias to check.

        Returns:
            True if alias exists, False otherwise.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT 1 FROM golden_repos_metadata WHERE alias = ?",
            (alias,),
        )
        return cursor.fetchone() is not None

    def update_enable_temporal(self, alias: str, enable: bool) -> bool:
        """
        Update the enable_temporal flag for a golden repository.

        Bug #131: This method is called after successful temporal index creation
        to update the enable_temporal flag in the database.

        Args:
            alias: Alias of the repository to update.
            enable: New value for enable_temporal flag.

        Returns:
            True if a record was updated, False if alias not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET enable_temporal = ? WHERE alias = ?",
                (1 if enable else 0, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated enable_temporal={enable} for golden repo: {alias}")
        return updated

    def update_temporal_options(self, alias: str, options: Optional[Dict]) -> bool:
        """
        Update the temporal_options JSON for a golden repository.

        Story #478: Persist temporal indexing configuration (max_commits,
        diff_context, since_date, all_branches) per repository so that
        admin-triggered rebuilds and scheduled refreshes apply stored options.

        Args:
            alias: Alias of the repository to update.
            options: Dict of temporal options, or None to clear.

        Returns:
            True if a record was updated, False if alias not found.
        """
        temporal_json = json.dumps(options) if options is not None else None

        def operation(conn):
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET temporal_options = ? WHERE alias = ?",
                (temporal_json, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated temporal_options for golden repo: {alias}")
        return updated

    def update_repo_url(self, alias: str, repo_url: str) -> bool:
        """
        Update the repo_url for a golden repository.

        Bug #131: This method is used during legacy cidx-meta migration (Scenario 2)
        to update repo_url from None to "local://cidx-meta".

        Args:
            alias: Alias of the repository to update.
            repo_url: New repo_url value.

        Returns:
            True if a record was updated, False if alias not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET repo_url = ? WHERE alias = ?",
                (repo_url, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated repo_url={repo_url} for golden repo: {alias}")
        return updated

    def update_category(
        self, alias: str, category_id: Optional[int], auto_assigned: bool = True
    ) -> bool:
        """
        Update category assignment for a golden repository (Story #181).

        Args:
            alias: Alias of the repository to update.
            category_id: Category ID to assign, or None for Unassigned.
            auto_assigned: Whether this is an automatic assignment (True) or manual (False).

        Returns:
            True if a record was updated, False if alias not found.
        """

        def operation(conn):
            cursor = conn.execute(
                """UPDATE golden_repos_metadata
                   SET category_id = ?, category_auto_assigned = ?
                   WHERE alias = ?""",
                (category_id, 1 if auto_assigned else 0, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(
                f"Updated category_id={category_id} (auto={auto_assigned}) for repo: {alias}"
            )
        return updated

    def update_wiki_enabled(self, alias: str, enabled: bool) -> None:
        """Update wiki_enabled flag for a golden repo (Story #280)."""

        def operation(conn):
            conn.execute(
                "UPDATE golden_repos_metadata SET wiki_enabled = ? WHERE alias = ?",
                (1 if enabled else 0, alias),
            )

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Updated wiki_enabled={enabled} for golden repo: {alias}")

    def update_default_branch(self, alias: str, branch: str) -> None:
        """
        Update the default_branch for a golden repository (Story #303).

        Args:
            alias: Repository alias (primary key).
            branch: New default branch name.

        Notes:
            If alias does not exist, this is a no-op (no error raised).
        """

        def operation(conn):
            conn.execute(
                "UPDATE golden_repos_metadata SET default_branch = ? WHERE alias = ?",
                (branch, alias),
            )

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Updated default_branch={branch!r} for golden repo: {alias}")

    def invalidate_description_refresh_tracking(self, alias: str) -> None:
        """
        Invalidate description refresh tracking for a repo after branch change (Story #303).

        Sets last_known_commit to NULL so the next refresh cycle re-analyzes.
        No-op if the alias has no tracking record.
        """

        def operation(conn):
            conn.execute(
                "UPDATE description_refresh_tracking SET last_known_commit = NULL WHERE repo_alias = ?",
                (alias,),
            )

        self._conn_manager.execute_atomic(operation)


def _migrate_golden_repos_metadata_columns(conn: sqlite3.Connection) -> None:
    """Add golden_repos_metadata columns that may be missing (idempotent)."""
    cursor = conn.execute("PRAGMA table_info(golden_repos_metadata)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    migrations = [
        ("category_id", "INTEGER"),
        ("category_auto_assigned", "INTEGER DEFAULT 0"),
        ("wiki_enabled", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_def in migrations:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE golden_repos_metadata ADD COLUMN {col_name} {col_def}"
            )
            logger.info("Migrated golden_repos_metadata: added column %s", col_name)


# Bug #1382: singleton-row table persisting the registry-reconcile
# circuit-breaker's cross-restart confirmation state (see
# record_reconcile_breaker_observation() below).
def _create_breaker_state_table(conn: sqlite3.Connection) -> None:
    """Bug #1382: registry-reconcile circuit-breaker cross-restart state."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS golden_repo_reconcile_breaker_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            orphan_fingerprint TEXT,
            consecutive_count INTEGER NOT NULL DEFAULT 0,
            first_observed_at TEXT,
            last_observed_at TEXT,
            updated_at TEXT
        )
    """
    )


# Issue #1383: singleton-row table persisting a discoverable
# trace of the most recent confirmed registry-reconcile
# auto-removal event -- survives the breaker-state reset above
# (see record_reconcile_auto_heal_event() below).
def _create_auto_heal_event_table(conn: sqlite3.Connection) -> None:
    """Issue #1383: most recent confirmed registry-reconcile auto-removal event."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS golden_repo_reconcile_auto_heal_event (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            removed_aliases TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        )
    """
    )


# Issue #1477: per-golden-alias fleet-migration failure
# quarantine tracking (see record_fleet_migration_failure()
# below). Unlike the singleton-row breaker-state table above,
# this is keyed per golden_alias since many repos are tracked
# independently.
def _create_fleet_migration_quarantine_table(conn: sqlite3.Connection) -> None:
    """Issue #1477: per-golden-alias fleet-migration failure quarantine tracking."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_migration_quarantine_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            state_signature TEXT,
            first_failed_at TEXT,
            last_failed_at TEXT,
            updated_at TEXT,
            signature_checked_at TEXT,
            failure_cause TEXT
        )
    """
    )


# Story #1560: per-golden-alias duplicate-point-id auto-
# resolution outcome tracking (see record_dedup_outcome()
# below). A NEW table, distinct from
# fleet_migration_quarantine_state above -- that one means
# "this repo keeps failing"; this one means "this repo
# migrated successfully but permanently lost N records".
def _create_dedup_state_table(conn: sqlite3.Connection) -> None:
    """Story #1560: per-golden-alias duplicate-point-id auto-resolution outcome tracking."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_migration_dedup_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            duplicate_groups INTEGER NOT NULL DEFAULT 0,
            records_before INTEGER NOT NULL DEFAULT 0,
            records_deleted INTEGER NOT NULL DEFAULT 0,
            winner_kept_groups INTEGER NOT NULL DEFAULT 0,
            whole_group_deleted_groups INTEGER NOT NULL DEFAULT 0,
            collection_total INTEGER NOT NULL DEFAULT 0,
            first_dropped_at TEXT,
            dropped_at TEXT,
            cleared_at TEXT,
            cleared_reason TEXT
        )
    """
    )


# Bug #1506: per-golden-alias ordinary-refresh integrity-gate
# failure quarantine tracking (see
# record_refresh_integrity_failure() below). Deliberately
# simpler than fleet_migration_quarantine_state above --
# ordinary refresh naturally alternates try/reset each
# scheduled cycle, so a bare consecutive counter (no
# content-signature auto-clear) is sufficient.
def _create_refresh_integrity_table(conn: sqlite3.Connection) -> None:
    """Bug #1506: per-golden-alias ordinary-refresh integrity-gate failure quarantine tracking."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_integrity_quarantine_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_detail TEXT,
            first_failed_at TEXT,
            last_failed_at TEXT,
            updated_at TEXT
        )
    """
    )


# Bug #1769: per-golden-alias local-repo `cidx init` repair
# failure quarantine tracking (see
# record_local_repo_repair_failure() below). Structurally
# identical to refresh_integrity_quarantine_state above --
# RefreshScheduler._repair_uninitialized_local_repo() retries
# the SAME repair every scheduled cycle, alternating
# try/reset, so a bare consecutive counter is sufficient.
# Before this table existed there was NO persisted failure
# state at all for this repair path -- a permanently-broken
# local repo re-ran `cidx init --force` and logged an ERROR
# on every single scheduled cycle forever (observed: 1,151
# occurrences over 3+ days on staging for a stuck
# langfuse_Claude_Code_*-global repo).
def _create_local_repo_repair_table(conn: sqlite3.Connection) -> None:
    """Bug #1769: per-golden-alias local-repo `cidx init` repair failure quarantine tracking."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_repo_repair_quarantine_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_detail TEXT,
            first_failed_at TEXT,
            last_failed_at TEXT,
            updated_at TEXT
        )
    """
    )


# Bug #1539: per-golden-alias cidx-meta backup conflict-
# resolution failure quarantine tracking (see
# record_cidx_meta_conflict_failure() below). This table
# stores the last failure's upstream target commit SHA --
# NOT freeform error text (Codex round-3 review found text
# fingerprinting fundamentally fragile: both false-positive
# collisions and false-negative misses across attempts) --
# so the SAME underlying rebase target can be distinguished
# from a genuinely different one across separate,
# independent sync() attempts, and automatically resets the
# moment the world changes (new commits land upstream).
def _create_cidx_meta_conflict_table(conn: sqlite3.Connection) -> None:
    """Bug #1539: per-golden-alias cidx-meta backup conflict-resolution failure quarantine tracking."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cidx_meta_conflict_quarantine_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_target_sha TEXT,
            last_detail TEXT,
            first_failed_at TEXT,
            last_failed_at TEXT,
            updated_at TEXT
        )
    """
    )


# Bug #1567: durable pending-deletion queue for versioned-
# snapshot cleanup (see global_repos/cleanup_manager.py). The
# PRE-FIX queue lived only in per-process dicts keyed by
# time.monotonic() -- any restart/worker-recycle silently
# discarded a scheduled deletion. scheduled_at is a WALL-CLOCK
# epoch-seconds float (time.time()), never time.monotonic(),
# since the minimum-retention-age floor
# (CleanupManager.MIN_RETENTION_AGE_SECONDS) must survive a
# process restart to mean anything across processes.
def _create_cleanup_pending_deletion_table(conn: sqlite3.Connection) -> None:
    """Bug #1567: durable pending-deletion queue for versioned-snapshot cleanup."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cleanup_pending_deletion_state (
            index_path TEXT PRIMARY KEY NOT NULL,
            scheduled_at REAL NOT NULL
        )
    """
    )


def _create_golden_repo_metadata_support_tables(conn: sqlite3.Connection) -> None:
    """Create the quarantine/dedup/cleanup tables ensure_table_exists owns,
    in the exact order the original monolithic method executed them."""
    _create_breaker_state_table(conn)
    _create_auto_heal_event_table(conn)
    _create_fleet_migration_quarantine_table(conn)
    _create_dedup_state_table(conn)
    _create_refresh_integrity_table(conn)
    _create_local_repo_repair_table(conn)
    _create_cidx_meta_conflict_table(conn)
    _create_cleanup_pending_deletion_table(conn)
