"""
PostgreSQL backend for global repository registry.

Story #412: PostgreSQL Backend for GlobalRepos and GoldenRepoMetadata

Drop-in replacement for GlobalReposSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the GlobalReposBackend Protocol.

Table: global_repos
    alias_name      TEXT PRIMARY KEY
    repo_name       TEXT NOT NULL
    repo_url        TEXT
    index_path      TEXT NOT NULL
    created_at      TIMESTAMPTZ NOT NULL
    last_refresh    TIMESTAMPTZ NOT NULL
    enable_temporal BOOLEAN NOT NULL DEFAULT FALSE
    temporal_options JSONB
    enable_scip     BOOLEAN NOT NULL DEFAULT FALSE
    next_refresh    TIMESTAMPTZ
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .pg_utils import sanitize_row
from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class GlobalReposPostgresBackend:
    """
    PostgreSQL backend for global repository registry.

    Satisfies the GlobalReposBackend Protocol (protocols.py).
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
    # Protocol methods
    # ------------------------------------------------------------------

    def register_repo(
        self,
        alias_name: str,
        repo_name: str,
        repo_url: Optional[str],
        index_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None:
        """
        Register a new repository or update existing one (upsert).

        Args:
            alias_name: Unique alias for the repository (primary key).
            repo_name: Name of the repository.
            repo_url: Optional URL of the repository.
            index_path: Path to the repository index.
            enable_temporal: Whether temporal indexing is enabled.
            temporal_options: Optional temporal indexing options.
            enable_scip: Whether SCIP code intelligence indexing is enabled.
        """
        now = datetime.now(timezone.utc).isoformat()
        temporal_json = (
            json.dumps(temporal_options) if temporal_options is not None else None
        )

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO global_repos
                        (alias_name, repo_name, repo_url, index_path, created_at,
                         last_refresh, enable_temporal, temporal_options, enable_scip)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (alias_name) DO UPDATE SET
                        repo_name        = EXCLUDED.repo_name,
                        repo_url         = EXCLUDED.repo_url,
                        index_path       = EXCLUDED.index_path,
                        last_refresh     = EXCLUDED.last_refresh,
                        enable_temporal  = EXCLUDED.enable_temporal,
                        temporal_options = EXCLUDED.temporal_options,
                        enable_scip      = EXCLUDED.enable_scip
                    """,
                    (
                        alias_name,
                        repo_name,
                        repo_url,
                        index_path,
                        now,
                        now,
                        enable_temporal,
                        temporal_json,
                        enable_scip,
                    ),
                )
            conn.commit()
        logger.info("Registered repo: %s", alias_name)

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]:
        """
        Get repository details by alias.

        Args:
            alias_name: Alias of the repository to retrieve.

        Returns:
            Dictionary with repository details, or None if not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alias_name, repo_name, repo_url, index_path, created_at,
                           last_refresh, enable_temporal, temporal_options, enable_scip,
                           next_refresh
                    FROM global_repos
                    WHERE alias_name = %s
                    """,
                    (alias_name,),
                )
                row = cur.fetchone()

        if row is None:
            return None

        return self._row_to_dict(row)

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        """
        List all registered repositories.

        Returns:
            Dictionary mapping alias names to repository detail dicts.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alias_name, repo_name, repo_url, index_path, created_at,
                           last_refresh, enable_temporal, temporal_options, enable_scip,
                           next_refresh
                    FROM global_repos
                    """
                )
                rows = cur.fetchall()

        return {row[0]: self._row_to_dict(row) for row in rows}

    def delete_repo(self, alias_name: str) -> bool:
        """
        Delete a repository by alias.

        Args:
            alias_name: Alias of the repository to delete.

        Returns:
            True if a record was deleted, False if not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM global_repos WHERE alias_name = %s",
                    (alias_name,),
                )
                deleted: bool = cur.rowcount > 0
            conn.commit()

        if deleted:
            logger.info("Deleted repo: %s", alias_name)
        return deleted

    def update_last_refresh(self, alias_name: str) -> bool:
        """
        Update the last_refresh timestamp to now.

        Args:
            alias_name: Alias of the repository to update.

        Returns:
            True if record was updated, False if not found.
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE global_repos SET last_refresh = %s WHERE alias_name = %s",
                    (now, alias_name),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.debug("Updated last_refresh for repo: %s", alias_name)
        return updated

    def update_enable_temporal(self, alias_name: str, enable_temporal: bool) -> bool:
        """
        Update the enable_temporal flag.

        Args:
            alias_name: Alias of the repository to update.
            enable_temporal: New value for the flag.

        Returns:
            True if record was updated, False if not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE global_repos SET enable_temporal = %s WHERE alias_name = %s",
                    (enable_temporal, alias_name),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.debug(
                "Updated enable_temporal=%s for repo: %s", enable_temporal, alias_name
            )
        return updated

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool:
        """
        Update the enable_scip flag.

        Args:
            alias_name: Alias of the repository to update.
            enable_scip: New value for the flag.

        Returns:
            True if record was updated, False if not found.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE global_repos SET enable_scip = %s WHERE alias_name = %s",
                    (enable_scip, alias_name),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.debug("Updated enable_scip=%s for repo: %s", enable_scip, alias_name)
        return updated

    def update_next_refresh(self, alias_name: str, next_refresh: Optional[str]) -> bool:
        """
        Update the next_refresh timestamp (or clear it).

        Bug #1308 Blocker B: next_refresh is a TIMESTAMPTZ column, but the
        GlobalReposBackend contract (mirroring the SQLite backend's TEXT
        column) passes next_refresh as an epoch-float STRING. Convert it to
        a timezone-aware UTC datetime before binding -- writing the naked
        string would error against a real PostgreSQL connection.

        Args:
            alias_name: Alias of the repository to update.
            next_refresh: Unix timestamp as string, or None to clear.

        Returns:
            True if record was updated, False if not found.
        """
        next_refresh_dt: Optional[datetime] = (
            datetime.fromtimestamp(float(next_refresh), tz=timezone.utc)
            if next_refresh is not None
            else None
        )

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE global_repos SET next_refresh = %s WHERE alias_name = %s",
                    (next_refresh_dt, alias_name),
                )
                updated: bool = cur.rowcount > 0
            conn.commit()

        if updated:
            logger.debug("Updated next_refresh for repo: %s", alias_name)
        return updated

    def list_due_repos(self, limit: int, now: float) -> list[Dict[str, Any]]:
        """
        Return repos whose next_refresh is due (<= now), oldest-first, capped.

        Bug #1308 remediation item #2: mirrors the SQLite backend's Bug #1063
        oldest-first, capped due-query so the cluster RefreshScheduler can
        auto-refresh in postgres mode. Without this method the PG-backed
        registry can resolve/read/write individual repos but can never find
        which repos are due for a scheduled refresh.

        Bug #1308 Blocker B: next_refresh is a real TIMESTAMPTZ column, so the
        comparison must be NATIVE timestamptz (via to_timestamp(%s) on the
        bound epoch float), never `CAST(next_refresh AS DOUBLE PRECISION)` --
        that cast is an invalid query-plan against a timestamptz column and
        fails on every call, even with all-NULL rows.

        Args:
            limit: Maximum number of repos to return (<=0 returns empty list).
            now: Current Unix timestamp; repos with next_refresh <= now are due.

        Returns:
            List of repo dicts ordered by next_refresh ASC (oldest first).
        """
        if limit <= 0:
            return []

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alias_name, repo_name, repo_url, index_path, created_at,
                           last_refresh, enable_temporal, temporal_options, enable_scip,
                           next_refresh
                    FROM global_repos
                    WHERE next_refresh IS NOT NULL
                      AND next_refresh <= to_timestamp(%s)
                    ORDER BY next_refresh ASC
                    LIMIT %s
                    """,
                    (now, limit),
                )
                rows = cur.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _next_refresh_to_epoch_str(value: Optional[datetime]) -> Optional[str]:
        """
        Convert a TIMESTAMPTZ next_refresh value (a Python datetime, as
        returned by psycopg) to the epoch-float STRING contract the
        scheduler/GlobalRegistry expect (matching the SQLite backend's
        TEXT-column semantics exactly). Bug #1308 Blocker B.

        Must run BEFORE sanitize_row(), which would otherwise ISO-format any
        datetime value -- correct for created_at/last_refresh, but wrong for
        next_refresh's epoch-float-string contract.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return str(value.timestamp())
        return value

    @staticmethod
    def _row_to_dict(row: tuple) -> Dict[str, Any]:
        """Convert a DB row tuple to the canonical repository dict."""
        temporal_options_raw = row[7]
        if temporal_options_raw is None:
            temporal_options = None
        elif isinstance(temporal_options_raw, str):
            temporal_options = json.loads(temporal_options_raw)
        else:
            temporal_options = temporal_options_raw

        return sanitize_row(
            {
                "alias_name": row[0],
                "repo_name": row[1],
                "repo_url": row[2],
                "index_path": row[3],
                "created_at": row[4],
                "last_refresh": row[5],
                "enable_temporal": bool(row[6]),
                "temporal_options": temporal_options,
                "enable_scip": bool(row[8]),
                "next_refresh": GlobalReposPostgresBackend._next_refresh_to_epoch_str(
                    row[9]
                ),
            }
        )
