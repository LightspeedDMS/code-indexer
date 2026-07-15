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
