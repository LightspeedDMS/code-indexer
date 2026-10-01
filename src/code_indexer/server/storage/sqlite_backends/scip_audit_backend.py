"""
SQLite backend for SCIP dependency installation audit records (Story #516).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager


class SCIPAuditSqliteBackend:
    """
    SQLite backend for SCIP dependency installation audit records (Story #516).

    Implements the SCIPAuditBackend Protocol.
    Adds node_id column to the original SCIPAuditRepository schema for
    cluster node identification.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (scip_audit.db).
        """
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the scip_dependency_installations table and indexes if they don't exist."""

        def _do_init(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scip_dependency_installations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                """
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON scip_dependency_installations (timestamp)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_repo_alias
                ON scip_dependency_installations (repo_alias)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_id
                ON scip_dependency_installations (job_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_project_language
                ON scip_dependency_installations (project_language)
                """
            )

        self._conn_manager.execute_atomic(_do_init)

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
        result: Dict[str, Any] = {}

        def _do_insert(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                INSERT INTO scip_dependency_installations
                (job_id, repo_alias, project_path, project_language,
                 project_build_system, package, command, reasoning, username, node_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            )
            result["record_id"] = cursor.lastrowid

        self._conn_manager.execute_atomic(_do_insert)
        record_id = result.get("record_id")
        if record_id is None:
            raise RuntimeError("Failed to get record ID after INSERT")
        return record_id  # type: ignore[no-any-return]

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
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]

        where_sql, params = self._build_where_clause(
            job_id=job_id,
            repo_alias=repo_alias,
            project_language=project_language,
            project_build_system=project_build_system,
            since=since,
            until=until,
        )

        count_sql = f"""
            SELECT COUNT(*) as total
            FROM scip_dependency_installations
            {where_sql}
        """
        cursor.execute(count_sql, params)
        total = cursor.fetchone()["total"]

        query_sql = f"""
            SELECT
                id, timestamp, job_id, repo_alias, project_path,
                project_language, project_build_system, package,
                command, reasoning, username, node_id
            FROM scip_dependency_installations
            {where_sql}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query_sql, params + [limit, offset])
        records = [dict(row) for row in cursor.fetchall()]
        return records, total

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
        where_clauses = []
        params: List[Any] = []

        if job_id:
            where_clauses.append("job_id = ?")
            params.append(job_id)
        if repo_alias:
            where_clauses.append("repo_alias = ?")
            params.append(repo_alias)
        if project_language:
            where_clauses.append("project_language = ?")
            params.append(project_language)
        if project_build_system:
            where_clauses.append("project_build_system = ?")
            params.append(project_build_system)
        if since:
            where_clauses.append("timestamp >= ?")
            params.append(since)
        if until:
            where_clauses.append("timestamp <= ?")
            params.append(until)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        return where_sql, params

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass
