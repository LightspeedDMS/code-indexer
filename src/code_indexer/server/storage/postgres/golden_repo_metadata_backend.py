"""
PostgreSQL backend for golden repository metadata.

Story #412: PostgreSQL Backend for GlobalRepos and GoldenRepoMetadata

Drop-in replacement for GoldenRepoMetadataSqliteBackend using psycopg v3
sync connections via ConnectionPool.  Satisfies the GoldenRepoMetadataBackend
Protocol.

Tables managed:
    golden_repos_metadata       — primary repo records
    description_refresh_tracking — used by invalidate_description_refresh_tracking
    dependency_map_tracking      — used by invalidate_dependency_map_tracking

Cross-table mutations use explicit transactions so they remain atomic.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .pg_utils import sanitize_row
from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Issue #1383: golden_repo_reconcile_auto_heal_event is a singleton-row
# table (like golden_repo_reconcile_breaker_state) -- only the most recent
# confirmed auto-removal event needs to be discoverable.
_RECONCILE_AUTO_HEAL_EVENT_ROW_ID = 1


class GoldenRepoMetadataPostgresBackend:
    """
    PostgreSQL backend for golden repository metadata.

    Satisfies the GoldenRepoMetadataBackend Protocol (protocols.py).
    All mutations use explicit transactions via the connection pool.
    """

    # Bug #1533: the SHARED, cross-node store. Counterpart of
    # GoldenRepoMetadataSqliteBackend.is_shared_backend (False) -- callers
    # that must see the cluster-wide view check this rather than assuming an
    # injected backend is necessarily the shared one.
    is_shared_backend = True

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def ensure_table_exists(self) -> None:
        """
        Ensure the golden_repos_metadata table exists (idempotent).

        In PostgreSQL the schema is managed by the migration runner; this
        method is a no-op compatibility shim so callers that call it on
        startup do not need to be changed.
        """
        # Migrations handle DDL; this is intentionally a no-op for Postgres.
        pass

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

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
            temporal_options: Optional temporal indexing options.

        Raises:
            psycopg.errors.UniqueViolation: If alias already exists.
        """
        temporal_json = (
            json.dumps(temporal_options) if temporal_options is not None else None
        )

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO golden_repos_metadata
                        (alias, repo_url, default_branch, clone_path, created_at,
                         enable_temporal, temporal_options)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        alias,
                        repo_url,
                        default_branch,
                        clone_path,
                        created_at,
                        enable_temporal,
                        temporal_json,
                    ),
                )
            conn.commit()
        logger.info("Added golden repo: %s", alias)

    def get_repo(self, alias: str) -> Optional[Dict[str, Any]]:
        """
        Get golden repository details by alias.

        Args:
            alias: Alias of the repository to retrieve.

        Returns:
            Dictionary with repository details, or None if not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alias, repo_url, default_branch, clone_path, created_at,
                           enable_temporal, temporal_options,
                           category_id, category_auto_assigned,
                           COALESCE(wiki_enabled, FALSE)
                    FROM golden_repos_metadata
                    WHERE alias = %s
                    """,
                    (alias,),
                )
                row = cur.fetchone()

        if row is None:
            return None

        return self._row_to_dict_full(row)

    def list_repos(self) -> List[Dict[str, Any]]:
        """
        List all golden repositories.

        Returns:
            List of repository dictionaries (without category fields).
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alias, repo_url, default_branch, clone_path, created_at,
                           enable_temporal, temporal_options,
                           COALESCE(wiki_enabled, FALSE)
                    FROM golden_repos_metadata
                    """
                )
                rows = cur.fetchall()

        return [self._row_to_dict_basic(row) for row in rows]

    def remove_repo(self, alias: str) -> bool:
        """
        Remove a golden repository by alias.

        Args:
            alias: Alias of the repository to remove.

        Returns:
            True if a record was deleted, False if not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM golden_repos_metadata WHERE alias = %s",
                    (alias,),
                )
                deleted: bool = cur.rowcount > 0
            conn.commit()

        if deleted:
            logger.info("Removed golden repo: %s", alias)
        return deleted

    def repo_exists(self, alias: str) -> bool:
        """
        Check if a golden repository exists.

        Args:
            alias: Alias to check.

        Returns:
            True if alias exists, False otherwise.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM golden_repos_metadata WHERE alias = %s",
                    (alias,),
                )
                return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Field update methods
    # ------------------------------------------------------------------

    def update_enable_temporal(self, alias: str, enable: bool) -> bool:
        """
        Update the enable_temporal flag for a golden repository.

        Args:
            alias: Alias of the repository to update.
            enable: New value for enable_temporal flag.

        Returns:
            True if a record was updated, False if alias not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE golden_repos_metadata SET enable_temporal = %s WHERE alias = %s",
                    (enable, alias),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.info("Updated enable_temporal=%s for golden repo: %s", enable, alias)
        return updated

    def update_temporal_options(self, alias: str, options: Optional[Dict]) -> bool:
        """
        Update the temporal_options JSON for a golden repository.

        Bug #1414: this method did not exist on the PostgreSQL backend at
        all (only update_enable_temporal and update_repo_url existed), so
        in cluster/PostgreSQL mode GoldenRepoManager.save_temporal_options()
        (the Web UI's only write path for temporal_options) called
        `.update_temporal_options(...)` on this class and raised an
        unhandled AttributeError -> HTTP 500, persisting nothing anywhere.
        Mirrors GoldenRepoMetadataSqliteBackend.update_temporal_options
        exactly (Story #478's contract): options=None clears the column.

        Args:
            alias: Alias of the repository to update.
            options: Dict of temporal options (max_commits, diff_context,
                since_date, all_branches), or None to clear.

        Returns:
            True if a record was updated, False if alias not found.
        """
        temporal_json = json.dumps(options) if options is not None else None

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE golden_repos_metadata SET temporal_options = %s::jsonb "
                    "WHERE alias = %s",
                    (temporal_json, alias),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.info("Updated temporal_options for golden repo: %s", alias)
        return updated

    def update_repo_url(self, alias: str, repo_url: str) -> bool:
        """
        Update the repo_url for a golden repository.

        Args:
            alias: Alias of the repository to update.
            repo_url: New repo_url value.

        Returns:
            True if a record was updated, False if alias not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE golden_repos_metadata SET repo_url = %s WHERE alias = %s",
                    (repo_url, alias),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.info("Updated repo_url=%s for golden repo: %s", repo_url, alias)
        return updated

    def update_category(
        self, alias: str, category_id: Optional[int], auto_assigned: bool = True
    ) -> bool:
        """
        Update category assignment for a golden repository.

        Args:
            alias: Alias of the repository to update.
            category_id: Category ID to assign, or None for Unassigned.
            auto_assigned: Whether this is an automatic assignment.

        Returns:
            True if a record was updated, False if alias not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE golden_repos_metadata
                    SET category_id = %s, category_auto_assigned = %s
                    WHERE alias = %s
                    """,
                    (category_id, auto_assigned, alias),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.debug(
                "Updated category_id=%s (auto=%s) for repo: %s",
                category_id,
                auto_assigned,
                alias,
            )
        return updated

    def update_wiki_enabled(self, alias: str, enabled: bool) -> None:
        """
        Update wiki_enabled flag for a golden repo.

        Args:
            alias: Alias of the repository to update.
            enabled: New value for wiki_enabled flag.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE golden_repos_metadata SET wiki_enabled = %s WHERE alias = %s",
                    (enabled, alias),
                )
            conn.commit()
        logger.info("Updated wiki_enabled=%s for golden repo: %s", enabled, alias)

    def update_default_branch(self, alias: str, branch: str) -> None:
        """
        Update the default_branch for a golden repository.

        Args:
            alias: Repository alias (primary key).
            branch: New default branch name.

        Notes:
            If alias does not exist, this is a no-op (no error raised).
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE golden_repos_metadata SET default_branch = %s WHERE alias = %s",
                    (branch, alias),
                )
            conn.commit()
        logger.info("Updated default_branch=%r for golden repo: %s", branch, alias)

    # ------------------------------------------------------------------
    # Cross-table invalidation methods
    # ------------------------------------------------------------------

    def invalidate_description_refresh_tracking(self, alias: str) -> None:
        """
        Invalidate description refresh tracking for a repo after branch change.

        Sets last_known_commit to NULL so the next refresh cycle re-analyzes.
        No-op if the alias has no tracking record.

        Args:
            alias: Repository alias whose tracking record to invalidate.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE description_refresh_tracking
                    SET last_known_commit = NULL
                    WHERE repo_alias = %s
                    """,
                    (alias,),
                )
            conn.commit()

    def invalidate_dependency_map_tracking(self, alias: str) -> None:
        """
        Remove alias entry from dependency_map_tracking.commit_hashes JSON.

        The commit_hashes column stores a JSON object mapping aliases to
        commit hashes.  This removes the entry for the specified alias so
        the next analysis re-processes it.
        No-op if no tracking record exists or alias not in commit_hashes.

        Args:
            alias: Repository alias to remove from commit_hashes.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # Fetch the current JSON, remove the key, write back
                cur.execute(
                    "SELECT commit_hashes FROM dependency_map_tracking WHERE id = 1"
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    return

                raw = row[0]
                # psycopg v3 with JSONB may return dict directly
                if isinstance(raw, str):
                    hashes: dict = json.loads(raw)
                else:
                    hashes = dict(raw)

                if alias not in hashes:
                    return

                del hashes[alias]

                cur.execute(
                    "UPDATE dependency_map_tracking SET commit_hashes = %s::jsonb WHERE id = 1",
                    (json.dumps(hashes),),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # List with categories
    # ------------------------------------------------------------------

    def list_repos_with_categories(self) -> List[Dict[str, Any]]:
        """
        List all golden repositories with category information.

        Returns:
            List of repository dicts including category_id and
            category_auto_assigned fields.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alias, repo_url, default_branch, clone_path, created_at,
                           enable_temporal, temporal_options,
                           category_id, category_auto_assigned,
                           COALESCE(wiki_enabled, FALSE)
                    FROM golden_repos_metadata
                    """
                )
                rows = cur.fetchall()

        return [self._row_to_dict_full(row) for row in rows]

    # ------------------------------------------------------------------
    # Registry-reconcile circuit-breaker confirmation state (Bug #1382)
    # ------------------------------------------------------------------

    def record_reconcile_breaker_observation(self, fingerprint: str) -> int:
        """
        Record one registry-reconcile circuit-breaker high-ratio observation
        (Bug #1382). See GoldenRepoMetadataSqliteBackend for the full
        contract -- this is the drop-in PostgreSQL (cluster-mode) mirror.

        Returns:
            The consecutive-observation count after recording this one.
        """
        now = datetime.now(timezone.utc)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT orphan_fingerprint, consecutive_count "
                    "FROM golden_repo_reconcile_breaker_state WHERE id = 1"
                )
                row = cur.fetchone()

                if row is None:
                    cur.execute(
                        "INSERT INTO golden_repo_reconcile_breaker_state "
                        "(id, orphan_fingerprint, consecutive_count, "
                        "first_observed_at, last_observed_at, updated_at) "
                        "VALUES (1, %s, 1, %s, %s, %s)",
                        (fingerprint, now, now, now),
                    )
                    count = 1
                else:
                    prev_fingerprint, prev_count = row
                    if prev_fingerprint == fingerprint:
                        count = prev_count + 1
                        cur.execute(
                            "UPDATE golden_repo_reconcile_breaker_state "
                            "SET consecutive_count = %s, last_observed_at = %s, "
                            "updated_at = %s WHERE id = 1",
                            (count, now, now),
                        )
                    else:
                        count = 1
                        cur.execute(
                            "UPDATE golden_repo_reconcile_breaker_state "
                            "SET orphan_fingerprint = %s, consecutive_count = 1, "
                            "first_observed_at = %s, last_observed_at = %s, "
                            "updated_at = %s WHERE id = 1",
                            (fingerprint, now, now, now),
                        )
            conn.commit()

        return count

    def reset_reconcile_breaker_state(self) -> None:
        """Clear the registry-reconcile circuit-breaker's persisted
        confirmation state (Bug #1382). See GoldenRepoMetadataSqliteBackend
        for the full contract."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM golden_repo_reconcile_breaker_state WHERE id = 1"
                )
            conn.commit()

    def get_reconcile_breaker_state(self) -> Optional[Dict[str, Any]]:
        """Return the current registry-reconcile circuit-breaker state, or
        None if the breaker has never tripped (or was reset since).
        Bug #1382 health-check escalation surface reads this."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT orphan_fingerprint, consecutive_count, "
                    "first_observed_at, last_observed_at "
                    "FROM golden_repo_reconcile_breaker_state WHERE id = 1"
                )
                row = cur.fetchone()

        if row is None:
            return None
        return {
            "orphan_fingerprint": row[0],
            "consecutive_count": row[1],
            "first_observed_at": row[2],
            "last_observed_at": row[3],
        }

    def record_reconcile_auto_heal_event(self, removed_aliases: List[str]) -> None:
        """
        Persist a discoverable trace of a confirmed registry-reconcile
        auto-removal event (Issue #1383). See
        GoldenRepoMetadataSqliteBackend for the full contract -- this is
        the drop-in PostgreSQL (cluster-mode) mirror.

        Raises:
            ValueError: If removed_aliases is not a list, or contains a
                non-string / empty element.
        """
        if not isinstance(removed_aliases, list):
            raise ValueError(
                f"removed_aliases must be a list, got: {type(removed_aliases)!r}"
            )
        for alias in removed_aliases:
            if not isinstance(alias, str) or not alias:
                raise ValueError(
                    f"removed_aliases must contain only non-empty strings, "
                    f"got: {alias!r}"
                )

        now = datetime.now(timezone.utc)
        removed_aliases_csv = ",".join(removed_aliases)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO golden_repo_reconcile_auto_heal_event "
                    "(id, removed_aliases, occurred_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "removed_aliases = EXCLUDED.removed_aliases, "
                    "occurred_at = EXCLUDED.occurred_at",
                    (_RECONCILE_AUTO_HEAL_EVENT_ROW_ID, removed_aliases_csv, now),
                )
            conn.commit()

    def get_reconcile_auto_heal_event(self) -> Optional[Dict[str, Any]]:
        """Return the most recently persisted registry-reconcile auto-heal
        event, or None if no confirmed auto-removal has ever fired (Issue
        #1383). See GoldenRepoMetadataSqliteBackend for the full contract
        -- this is the drop-in PostgreSQL (cluster-mode) mirror."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT removed_aliases, occurred_at FROM "
                    "golden_repo_reconcile_auto_heal_event WHERE id = %s",
                    (_RECONCILE_AUTO_HEAL_EVENT_ROW_ID,),
                )
                row = cur.fetchone()

        if row is None:
            return None
        removed_aliases_csv, occurred_at = row
        removed_aliases = [a for a in (removed_aliases_csv or "").split(",") if a]
        return {"removed_aliases": removed_aliases, "occurred_at": occurred_at}

    # ------------------------------------------------------------------
    # Fleet-migration failure quarantine (Issue #1477)
    # ------------------------------------------------------------------

    def record_fleet_migration_failure(
        self,
        golden_alias: str,
        state_signature: str,
        failure_cause: Optional[str] = None,
    ) -> int:
        """
        Record one fleet-migration consolidation failure for a golden repo
        (Issue #1477). See GoldenRepoMetadataSqliteBackend for the full
        contract -- this is the drop-in PostgreSQL (cluster-mode) mirror.

        ``failure_cause`` (Finding I, Codex round-5 review) is ALSO
        always overwritten to the value supplied for THIS failure -- see
        GoldenRepoMetadataSqliteBackend for the full contract.

        Returns:
            The consecutive-failure count after recording this one.

        Raises:
            ValueError: golden_alias or state_signature is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not state_signature:
            raise ValueError("state_signature must be a non-empty string")

        now = datetime.now(timezone.utc)

        # Issue #1477 Finding L (Codex round-6 review, live-reproduced
        # against real concurrent PostgreSQL connections): a separate
        # SELECT followed by a Python-computed count+1 INSERT/UPDATE is a
        # classic lost-update race -- two concurrent connections can both
        # read the same starting count, both compute the same next value,
        # and one increment silently vanishes. This is now a SINGLE atomic
        # statement: PostgreSQL's own row-level ON CONFLICT handling
        # performs the increment server-side, so there is no window
        # between a read and a write for a second connection to land in.
        #
        # Every column bound via an explicit placeholder (no inline
        # literal mixed with "%s" markers) -- 8 columns, 8 "%s", 8 tuple
        # elements for the VALUES clause, unambiguous to verify by eye.
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO fleet_migration_quarantine_state "
                    "(golden_alias, consecutive_failure_count, "
                    "state_signature, first_failed_at, last_failed_at, "
                    "updated_at, signature_checked_at, failure_cause) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (golden_alias) DO UPDATE SET "
                    "consecutive_failure_count = "
                    "fleet_migration_quarantine_state.consecutive_failure_count + 1, "
                    "state_signature = EXCLUDED.state_signature, "
                    "last_failed_at = EXCLUDED.last_failed_at, "
                    "updated_at = EXCLUDED.updated_at, "
                    "signature_checked_at = EXCLUDED.signature_checked_at, "
                    "failure_cause = EXCLUDED.failure_cause "
                    "RETURNING consecutive_failure_count",
                    (
                        golden_alias,
                        1,
                        state_signature,
                        now,
                        now,
                        now,
                        now,
                        failure_cause,
                    ),
                )
                row = cur.fetchone()
            conn.commit()

        count: int = row[0]
        return count

    def reset_fleet_migration_failure(self, golden_alias: str) -> None:
        """Clear any persisted fleet-migration failure/quarantine state for
        a golden repo (Issue #1477). See GoldenRepoMetadataSqliteBackend
        for the full contract.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM fleet_migration_quarantine_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
            conn.commit()

    def soft_reset_fleet_migration_failure_count(self, golden_alias: str) -> None:
        """Issue #1477 Finding N: fallback used by
        `_clear_quarantine_after_detected_repair()` when the full reset
        (DELETE, above) fails but a plain UPDATE still works. Zeroes
        `consecutive_failure_count` while KEEPING the row -- gives a
        just-repaired repo a genuinely fresh failure budget instead of
        resuming from a stale, elevated count. See
        GoldenRepoMetadataSqliteBackend for the full contract.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fleet_migration_quarantine_state "
                    "SET consecutive_failure_count = %s WHERE golden_alias = %s",
                    (0, golden_alias),
                )
            conn.commit()

    def touch_fleet_migration_failure_check(self, golden_alias: str) -> None:
        """Update ONLY the `signature_checked_at` throttle-bookkeeping
        timestamp for `golden_alias` (Issue #1477 Finding C, Codex round-3
        review). See GoldenRepoMetadataSqliteBackend for the full
        contract -- this is the drop-in PostgreSQL (cluster-mode) mirror.

        A no-op (never raises) when no row exists for `golden_alias`.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        now = datetime.now(timezone.utc)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fleet_migration_quarantine_state "
                    "SET signature_checked_at = %s WHERE golden_alias = %s",
                    (now, golden_alias),
                )
            conn.commit()

    def get_fleet_migration_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """Return the currently persisted fleet-migration failure state for
        a golden repo, or None if it has never failed (or was reset
        since). See GoldenRepoMetadataSqliteBackend for the full
        contract.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT golden_alias, consecutive_failure_count, "
                    "state_signature, first_failed_at, last_failed_at, "
                    "signature_checked_at, failure_cause "
                    "FROM fleet_migration_quarantine_state WHERE golden_alias = %s",
                    (golden_alias,),
                )
                row = cur.fetchone()

        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": row[1],
            "state_signature": row[2],
            "first_failed_at": row[3],
            "last_failed_at": row[4],
            "signature_checked_at": row[5],
            "failure_cause": row[6],
        }

    def list_fleet_migration_failure_states(self) -> List[Dict[str, Any]]:
        """Return every persisted fleet-migration failure-tracking row
        (Issue #1477). See GoldenRepoMetadataSqliteBackend for the full
        contract."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT golden_alias, consecutive_failure_count, "
                    "state_signature, first_failed_at, last_failed_at, "
                    "signature_checked_at, failure_cause "
                    "FROM fleet_migration_quarantine_state"
                )
                rows = cur.fetchall()

        return [
            {
                "golden_alias": row[0],
                "consecutive_failure_count": row[1],
                "state_signature": row[2],
                "first_failed_at": row[3],
                "last_failed_at": row[4],
                "signature_checked_at": row[5],
                "failure_cause": row[6],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # cidx-meta backup conflict-resolution failure quarantine (Bug #1539)
    # ------------------------------------------------------------------

    def record_cidx_meta_conflict_failure(
        self, golden_alias: str, target_sha: str, detail: str
    ) -> int:
        """Record one cidx-meta conflict-resolution failure (Bug #1539).

        Mirrors GoldenRepoMetadataSqliteBackend's contract exactly. A
        single atomic ``INSERT ... ON CONFLICT ... RETURNING`` performs
        the target-SHA-aware conditional increment server-side: the
        count increments only when the stored ``last_target_sha`` equals
        the new one, otherwise it resets to 1 -- avoiding a separate
        read-then-write round trip and any lost-update race. Keying on
        the upstream commit SHA (never freeform error text) is Bug
        #1539's Codex round-3 redesign.

        Returns:
            The consecutive-failure count after recording this one.

        Raises:
            ValueError: any argument is empty/blank (whitespace-only
                included).
            RuntimeError: the upsert's RETURNING clause produced no row
                -- should be unreachable, guarded loudly rather than
                silently indexing into a None result.
        """
        for name, value in (
            ("golden_alias", golden_alias),
            ("target_sha", target_sha),
            ("detail", detail),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        now = datetime.now(timezone.utc)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cidx_meta_conflict_quarantine_state "
                    "(golden_alias, consecutive_failure_count, last_target_sha, "
                    "last_detail, first_failed_at, last_failed_at, updated_at) "
                    "VALUES (%s, 1, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (golden_alias) DO UPDATE SET "
                    "consecutive_failure_count = CASE WHEN "
                    "cidx_meta_conflict_quarantine_state.last_target_sha = "
                    "EXCLUDED.last_target_sha THEN "
                    "cidx_meta_conflict_quarantine_state.consecutive_failure_count + 1 "
                    "ELSE 1 END, "
                    "last_target_sha = EXCLUDED.last_target_sha, "
                    "last_detail = EXCLUDED.last_detail, "
                    "last_failed_at = EXCLUDED.last_failed_at, "
                    "updated_at = EXCLUDED.updated_at "
                    "RETURNING consecutive_failure_count",
                    (golden_alias, target_sha, detail, now, now, now),
                )
                row = cur.fetchone()
            conn.commit()

        if row is None:
            raise RuntimeError(
                "cidx_meta_conflict_quarantine_state upsert RETURNING "
                "clause produced no row -- this should be unreachable"
            )
        return int(row[0])

    def reset_cidx_meta_conflict_failure(self, golden_alias: str) -> None:
        """Clear any persisted cidx-meta conflict-resolution failure/
        quarantine state for a golden repo (Bug #1539). See
        GoldenRepoMetadataSqliteBackend for the full contract.

        Raises:
            ValueError: golden_alias is empty/blank (including
                whitespace-only).
        """
        if not golden_alias or not golden_alias.strip():
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cidx_meta_conflict_quarantine_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
            conn.commit()

    def get_cidx_meta_conflict_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """Return the currently persisted cidx-meta conflict-resolution
        failure state for a golden repo, or None if it has never failed
        (or was reset since). See GoldenRepoMetadataSqliteBackend for the
        full contract.

        Raises:
            ValueError: golden_alias is empty/blank (including
                whitespace-only).
        """
        if not golden_alias or not golden_alias.strip():
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT golden_alias, consecutive_failure_count, "
                    "last_target_sha, last_detail, first_failed_at, last_failed_at "
                    "FROM cidx_meta_conflict_quarantine_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
                row = cur.fetchone()

        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": row[1],
            "last_target_sha": row[2],
            "last_detail": row[3],
            "first_failed_at": row[4],
            "last_failed_at": row[5],
        }

    # ------------------------------------------------------------------
    # Ordinary-refresh integrity-gate failure quarantine (Bug #1506)
    # ------------------------------------------------------------------

    def record_refresh_integrity_failure(self, golden_alias: str, detail: str) -> int:
        """Record one ordinary-refresh integrity-gate failure for a golden
        repo (Bug #1506). See GoldenRepoMetadataSqliteBackend for the full
        contract -- this is the drop-in PostgreSQL (cluster-mode) mirror.
        A single atomic ``INSERT ... ON CONFLICT ... RETURNING`` statement
        performs the increment server-side (mirrors
        record_fleet_migration_failure's lost-update-race fix above).

        Returns:
            The consecutive-failure count after recording this one.

        Raises:
            ValueError: golden_alias or detail is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")

        now = datetime.now(timezone.utc)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO refresh_integrity_quarantine_state "
                    "(golden_alias, consecutive_failure_count, last_detail, "
                    "first_failed_at, last_failed_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (golden_alias) DO UPDATE SET "
                    "consecutive_failure_count = "
                    "refresh_integrity_quarantine_state.consecutive_failure_count + 1, "
                    "last_detail = EXCLUDED.last_detail, "
                    "last_failed_at = EXCLUDED.last_failed_at, "
                    "updated_at = EXCLUDED.updated_at "
                    "RETURNING consecutive_failure_count",
                    (golden_alias, 1, detail, now, now, now),
                )
                row = cur.fetchone()
            conn.commit()

        count: int = row[0]
        return count

    def reset_refresh_integrity_failure(self, golden_alias: str) -> None:
        """Clear any persisted refresh-integrity failure/quarantine state
        for a golden repo (Bug #1506). See GoldenRepoMetadataSqliteBackend
        for the full contract.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM refresh_integrity_quarantine_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
            conn.commit()

    def get_refresh_integrity_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """Return the currently persisted refresh-integrity failure state
        for a golden repo, or None if it has never failed (or was reset
        since). See GoldenRepoMetadataSqliteBackend for the full contract.

        Raises:
            ValueError: golden_alias is empty/blank.
        """
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT golden_alias, consecutive_failure_count, "
                    "last_detail, first_failed_at, last_failed_at "
                    "FROM refresh_integrity_quarantine_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
                row = cur.fetchone()

        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": row[1],
            "last_detail": row[2],
            "first_failed_at": row[3],
            "last_failed_at": row[4],
        }

    # ------------------------------------------------------------------
    # Duplicate-point-id auto-resolution outcome state (Story #1560)
    #
    # Reachable exactly like every other method on this class: via
    # dedup_state.py's _get_dedup_backend(golden_repo_manager), which
    # duck-types whatever backend GoldenRepoManager/StorageFactory
    # injected (this class in cluster/postgres mode,
    # GoldenRepoMetadataSqliteBackend in solo/sqlite mode) and calls
    # backend.record_dedup_outcome(...) generically -- there is no
    # PG-specific call site anywhere for ANY method here, and none is
    # needed; that is this codebase's established integration pattern.
    # ------------------------------------------------------------------

    _DEDUP_STATE_SELECT_COLUMNS = (
        "golden_alias, duplicate_groups, records_before, records_deleted, "
        "winner_kept_groups, whole_group_deleted_groups, collection_total, "
        "first_dropped_at, dropped_at, cleared_at, cleared_reason"
    )

    # AC9: cumulative fields ADDED server-side via `+=` on conflict (a
    # single atomic statement -- avoids the read-then-write lost-update
    # race record_fleet_migration_failure's own PG mirror already
    # guards against for an analogous counter); records_before/
    # collection_total OVERWRITTEN (snapshot semantics);
    # first_dropped_at absent from SET so a conflict never touches it;
    # cleared_at/cleared_reason reset -- a fresh outcome is active again.
    _RECORD_DEDUP_OUTCOME_SQL = (
        "INSERT INTO fleet_migration_dedup_state "
        "(golden_alias, duplicate_groups, records_before, records_deleted, "
        "winner_kept_groups, whole_group_deleted_groups, collection_total, "
        "first_dropped_at, dropped_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (golden_alias) DO UPDATE SET "
        "duplicate_groups = fleet_migration_dedup_state.duplicate_groups "
        "+ EXCLUDED.duplicate_groups, "
        "records_before = EXCLUDED.records_before, "
        "records_deleted = fleet_migration_dedup_state.records_deleted "
        "+ EXCLUDED.records_deleted, "
        "winner_kept_groups = fleet_migration_dedup_state.winner_kept_groups "
        "+ EXCLUDED.winner_kept_groups, "
        "whole_group_deleted_groups = "
        "fleet_migration_dedup_state.whole_group_deleted_groups "
        "+ EXCLUDED.whole_group_deleted_groups, "
        "collection_total = EXCLUDED.collection_total, "
        "dropped_at = EXCLUDED.dropped_at, "
        "cleared_at = NULL, cleared_reason = NULL "
        f"RETURNING {_DEDUP_STATE_SELECT_COLUMNS}"
    )

    @staticmethod
    def _dedup_state_row_to_dict(row: Any) -> Dict[str, Any]:
        """`row: Any` -- a psycopg cursor row tuple; column types vary
        and have no single precise static type in this codebase's
        convention (mirrors sqlite_backends.py's identical helper)."""
        return {
            "golden_alias": row[0],
            "duplicate_groups": row[1],
            "records_before": row[2],
            "records_deleted": row[3],
            "winner_kept_groups": row[4],
            "whole_group_deleted_groups": row[5],
            "collection_total": row[6],
            "first_dropped_at": row[7],
            "dropped_at": row[8],
            "cleared_at": row[9],
            "cleared_reason": row[10],
        }

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
    ) -> Dict[str, Any]:
        """Record one dedup-resolution outcome (AC6/AC7/AC9) -- see
        `_RECORD_DEDUP_OUTCOME_SQL` for the cumulative-vs-snapshot
        semantics. Numeric parameters are trusted at their type-hinted
        `int` contract with zero additional runtime range validation --
        matching every OTHER method in this class (e.g.
        `record_fleet_migration_failure`'s implicit `+1` counter,
        `soft_reset_fleet_migration_failure_count`'s hardcoded `0`) and
        the already-accepted SQLite mirror in sqlite_backends.py.

        Raises:
            ValueError: golden_alias is not a non-empty (non-whitespace)
                string.
        """
        if not isinstance(golden_alias, str) or not golden_alias.strip():
            raise ValueError(
                f"golden_alias must be a non-empty string, got {golden_alias!r}"
            )
        now = datetime.now(timezone.utc)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    self._RECORD_DEDUP_OUTCOME_SQL,
                    (
                        golden_alias,
                        duplicate_groups,
                        records_before,
                        records_deleted,
                        winner_kept_groups,
                        whole_group_deleted_groups,
                        collection_total,
                        now,
                        now,
                    ),
                )
                row = cur.fetchone()
            conn.commit()

        assert row is not None, (
            "INSERT ... RETURNING on fleet_migration_dedup_state must "
            "always yield exactly one row"
        )
        return self._dedup_state_row_to_dict(row)

    def get_dedup_state(self, golden_alias: str) -> Optional[Dict[str, Any]]:
        """Currently persisted dedup-outcome state, or None if absent.

        Raises:
            ValueError: golden_alias is not a non-empty (non-whitespace)
                string.
        """
        if not isinstance(golden_alias, str) or not golden_alias.strip():
            raise ValueError(
                f"golden_alias must be a non-empty string, got {golden_alias!r}"
            )

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._DEDUP_STATE_SELECT_COLUMNS} "
                    f"FROM fleet_migration_dedup_state WHERE golden_alias = %s",
                    (golden_alias,),
                )
                row = cur.fetchone()

        return None if row is None else self._dedup_state_row_to_dict(row)

    def list_dedup_states(self) -> List[Dict[str, Any]]:
        """Every persisted dedup-outcome row -- used by the /health
        surface (AC13-AC18)."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._DEDUP_STATE_SELECT_COLUMNS} "
                    f"FROM fleet_migration_dedup_state"
                )
                rows = cur.fetchall()

        return [self._dedup_state_row_to_dict(row) for row in rows]

    def clear_dedup_state(self, golden_alias: str, reason: str) -> None:
        """Mark a dedup-outcome state as cleared (AC8). No-op if absent.

        Raises:
            ValueError: golden_alias or reason is not a non-empty
                (non-whitespace) string.
        """
        if not isinstance(golden_alias, str) or not golden_alias.strip():
            raise ValueError(
                f"golden_alias must be a non-empty string, got {golden_alias!r}"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"reason must be a non-empty string, got {reason!r}")
        now = datetime.now(timezone.utc)

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fleet_migration_dedup_state SET cleared_at = %s, "
                    "cleared_reason = %s WHERE golden_alias = %s",
                    (now, reason, golden_alias),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Cleanup pending-deletion queue (Bug #1567)
    # ------------------------------------------------------------------

    def schedule_cleanup_deletion(self, index_path: str, scheduled_at: float) -> float:
        """
        Durably record ``index_path`` as pending deletion. See
        GoldenRepoMetadataSqliteBackend for the full contract -- this is
        the drop-in PostgreSQL (cluster-mode) mirror.

        Raises:
            ValueError: index_path is not a non-empty string, or
                scheduled_at is not a finite number.
        """
        if not isinstance(index_path, str) or not index_path.strip():
            raise ValueError(
                f"index_path must be a non-empty string, got {index_path!r}"
            )
        if not isinstance(scheduled_at, (int, float)) or isinstance(scheduled_at, bool):
            raise ValueError(
                f"scheduled_at must be a real number, got {scheduled_at!r}"
            )
        scheduled_at = float(scheduled_at)
        if not math.isfinite(scheduled_at):
            raise ValueError(
                f"scheduled_at must be a finite number, got {scheduled_at!r}"
            )

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT scheduled_at FROM cleanup_pending_deletion_state "
                    "WHERE index_path = %s",
                    (index_path,),
                )
                row = cur.fetchone()
                if row is not None:
                    return float(row[0])
                cur.execute(
                    "INSERT INTO cleanup_pending_deletion_state "
                    "(index_path, scheduled_at) VALUES (%s, %s)",
                    (index_path, scheduled_at),
                )
            conn.commit()
        return float(scheduled_at)

    def list_cleanup_pending_deletions(self) -> List[Dict[str, Any]]:
        """Return every durably-pending deletion row. See
        GoldenRepoMetadataSqliteBackend for the full contract -- this is
        the drop-in PostgreSQL (cluster-mode) mirror."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT index_path, scheduled_at FROM cleanup_pending_deletion_state"
                )
                rows = cur.fetchall()
        return [{"index_path": row[0], "scheduled_at": float(row[1])} for row in rows]

    def remove_cleanup_pending_deletion(self, index_path: str) -> None:
        """Remove one durably-pending deletion row, idempotent. See
        GoldenRepoMetadataSqliteBackend for the full contract.

        Raises:
            ValueError: index_path is not a non-empty string.
        """
        if not isinstance(index_path, str) or not index_path.strip():
            raise ValueError(
                f"index_path must be a non-empty string, got {index_path!r}"
            )
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cleanup_pending_deletion_state WHERE index_path = %s",
                    (index_path,),
                )
            conn.commit()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_temporal_options(raw: Any) -> Optional[Dict]:
        """Parse temporal_options from DB (may be str, dict, or None)."""
        if raw is None:
            return None
        if isinstance(raw, str):
            return dict(json.loads(raw))
        return dict(raw)

    @classmethod
    def _row_to_dict_basic(cls, row: tuple) -> Dict[str, Any]:
        """
        Convert a basic SELECT row (8 columns, no category fields) to dict.

        Column order:
            0  alias
            1  repo_url
            2  default_branch
            3  clone_path
            4  created_at
            5  enable_temporal
            6  temporal_options
            7  wiki_enabled
        """
        return sanitize_row(
            {
                "alias": row[0],
                "repo_url": row[1],
                "default_branch": row[2],
                "clone_path": row[3],
                "created_at": row[4],
                "enable_temporal": bool(row[5]),
                "temporal_options": cls._parse_temporal_options(row[6]),
                "wiki_enabled": bool(row[7]),
            }
        )

    @classmethod
    def _row_to_dict_full(cls, row: tuple) -> Dict[str, Any]:
        """
        Convert a full SELECT row (10 columns, with category fields) to dict.

        Column order:
            0  alias
            1  repo_url
            2  default_branch
            3  clone_path
            4  created_at
            5  enable_temporal
            6  temporal_options
            7  category_id
            8  category_auto_assigned
            9  wiki_enabled
        """
        return sanitize_row(
            {
                "alias": row[0],
                "repo_url": row[1],
                "default_branch": row[2],
                "clone_path": row[3],
                "created_at": row[4],
                "enable_temporal": bool(row[5]),
                "temporal_options": cls._parse_temporal_options(row[6]),
                "category_id": row[7],
                "category_auto_assigned": bool(row[8]),
                "wiki_enabled": bool(row[9]),
            }
        )
