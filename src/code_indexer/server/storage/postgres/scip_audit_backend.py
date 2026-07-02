"""
PostgreSQL backend for SCIP audit storage (Story #516).

Drop-in replacement for SCIPAuditSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the SCIPAuditBackend Protocol (protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required for new deployments.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class SCIPAuditPostgresBackend:
    """
    PostgreSQL backend for SCIP dependency installation audit records.

    Satisfies the SCIPAuditBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure the table exists.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the scip_dependency_installations table and indexes if they don't exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scip_dependency_installations (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ DEFAULT NOW(),
                        job_id VARCHAR(36) NOT NULL,
                        repo_alias VARCHAR(255) NOT NULL,
                        project_path VARCHAR(255),
                        project_language VARCHAR(50),
                        project_build_system VARCHAR(50),
                        package VARCHAR(255) NOT NULL,
                        command TEXT NOT NULL,
                        reasoning TEXT,
                        username VARCHAR(255),
                        node_id VARCHAR(255)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_timestamp "
                    "ON scip_dependency_installations (timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_repo_alias "
                    "ON scip_dependency_installations (repo_alias)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_job_id "
                    "ON scip_dependency_installations (job_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_project_language "
                    "ON scip_dependency_installations (project_language)"
                )
                conn.commit()
        except Exception as exc:
            logger.warning("SCIPAuditPostgresBackend: schema setup failed: %s", exc)

    def create_audit_record(
        self,
        job_id: str,
        repo_alias: str,
        package: str,
        command: str,
        project_path: Optional[str] = None,
        project_language: Optional[str] = None,
        project_build_system: Optional[str] = None,
        reasoning: Optional[str] = None,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> int:
        """Create an audit record for a dependency installation.

        Args:
            job_id: Background job ID that triggered installation.
            repo_alias: Repository alias being processed.
            package: Package name that was installed.
            command: Full installation command executed.
            project_path: Project path within repository (optional).
            project_language: Programming language (optional).
            project_build_system: Build system used (optional).
            reasoning: Claude's reasoning for installation (optional).
            username: User who triggered the job (optional).
            node_id: Cluster node identifier (optional, Story #516 AC1).

        Returns:
            Record ID of created audit record.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO scip_dependency_installations
                (job_id, repo_alias, project_path, project_language,
                 project_build_system, package, command, reasoning, username, node_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    job_id,
                    repo_alias,
                    project_path,
                    project_language,
                    project_build_system,
                    package,
                    command,
                    reasoning,
                    username,
                    node_id,
                ),
            ).fetchone()
            conn.commit()

        if row is None:
            raise RuntimeError("Failed to get record ID after INSERT")
        return int(row[0])

    def query_audit_records(
        self,
        job_id: Optional[str] = None,
        repo_alias: Optional[str] = None,
        project_language: Optional[str] = None,
        project_build_system: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query audit records with filtering and pagination.

        Args:
            job_id: Filter by job ID (optional).
            repo_alias: Filter by repository alias (optional).
            project_language: Filter by project language (optional).
            project_build_system: Filter by build system (optional).
            since: Filter records after this ISO timestamp (optional).
            until: Filter records before this ISO timestamp (optional).
            limit: Maximum records to return (default 100).
            offset: Number of records to skip (default 0).

        Returns:
            Tuple of (records list, total count).
        """
        where_clause, params = self._build_where_clause(
            job_id=job_id,
            repo_alias=repo_alias,
            project_language=project_language,
            project_build_system=project_build_system,
            since=since,
            until=until,
        )

        with self._pool.connection() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM scip_dependency_installations {where_clause}",
                params,
            ).fetchone()
            total_count: int = int(count_row[0]) if count_row else 0

            rows = conn.execute(
                f"""
                SELECT
                    id, timestamp, job_id, repo_alias, project_path,
                    project_language, project_build_system, package,
                    command, reasoning, username, node_id
                FROM scip_dependency_installations
                {where_clause}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            ).fetchall()

        return [self._row_to_dict(row) for row in rows], total_count

    def _build_where_clause(
        self,
        job_id: Optional[str],
        repo_alias: Optional[str],
        project_language: Optional[str],
        project_build_system: Optional[str],
        since: Optional[str],
        until: Optional[str],
    ) -> Tuple[str, List[Any]]:
        """Build WHERE clause and parameters for query filtering."""
        conditions: List[str] = []
        params: List[Any] = []

        if job_id:
            conditions.append("job_id = %s")
            params.append(job_id)
        if repo_alias:
            conditions.append("repo_alias = %s")
            params.append(repo_alias)
        if project_language:
            conditions.append("project_language = %s")
            params.append(project_language)
        if project_build_system:
            conditions.append("project_build_system = %s")
            params.append(project_build_system)
        if since:
            conditions.append("timestamp >= %s")
            params.append(since)
        if until:
            conditions.append("timestamp <= %s")
            params.append(until)

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where_clause, params

    def _row_to_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to an audit record dict."""
        # timestamp may come back as a datetime from PostgreSQL
        from datetime import datetime

        timestamp = row[1]
        if isinstance(timestamp, datetime):
            timestamp = timestamp.isoformat()

        return {
            "id": row[0],
            "timestamp": timestamp,
            "job_id": row[2],
            "repo_alias": row[3],
            "project_path": row[4],
            "project_language": row[5],
            "project_build_system": row[6],
            "package": row[7],
            "command": row[8],
            "reasoning": row[9],
            "username": row[10],
            "node_id": row[11],
        }

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
