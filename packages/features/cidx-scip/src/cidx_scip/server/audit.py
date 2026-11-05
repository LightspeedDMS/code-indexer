"""
SCIP Audit Repository for Dependency Installation Tracking.

Provides audit trail for SCIP dependency installations with project context.
Part of AC3: Audit Table for Dependency Installations with Project Context (Story #646).

Since modular phase 4d the records live in the server database behind the
feature's ``storage`` capability (``scip_dependency_installations``, created
by the feature's migration 1): the SQLite backend on a solo server, the
PostgreSQL backend in a cluster. A repository opened on a file path (a tool
or a test) keeps that file's own table.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from cidx_scip.server.audit_sqlite import SCIPAuditSqliteBackend


class SqliteFileStorage:
    """``storage``-shaped access to one SQLite file: a connection per block,
    committed when the block ends normally, rolled back when it raises."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class SCIPAuditRepository:
    """
    Repository for managing SCIP dependency installation audit records.

    Provides atomic record creation and comprehensive querying capabilities
    for tracking dependency installations across projects and languages.
    """

    def __init__(
        self, db_path: Optional[str] = None, storage_backend: Optional[Any] = None
    ) -> None:
        """
        Initialize SCIP Audit Repository.

        Args:
            db_path: SQLite file to keep the records in when no backend is
                given (default ``~/.cidx-server/scip_audit.db``).
            storage_backend: the backend holding the records (the feature
                passes the one over its ``storage`` capability).
        """
        if storage_backend is None:
            path = (
                Path(db_path)
                if db_path
                else Path.home() / ".cidx-server" / "scip_audit.db"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path: Optional[Path] = path
            storage_backend = SCIPAuditSqliteBackend(
                SqliteFileStorage(path), ensure_schema=True
            )
        else:
            self.db_path = None
        self._backend = storage_backend

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
    ) -> int:
        """
        Create an audit record for a dependency installation.

        Atomic write operation - record is immediately queryable after insertion.

        Args:
            job_id: Background job ID that triggered installation
            repo_alias: Repository alias being processed
            package: Package name that was installed
            command: Full installation command executed
            project_path: Project path within repository (optional)
            project_language: Programming language (optional)
            project_build_system: Build system used (optional)
            reasoning: Claude's reasoning for installation (optional)
            username: User who triggered the job (optional)

        Returns:
            Record ID of created audit record
        """
        return int(
            self._backend.create_audit_record(
                job_id=job_id,
                repo_alias=repo_alias,
                package=package,
                command=command,
                project_path=project_path,
                project_language=project_language,
                project_build_system=project_build_system,
                reasoning=reasoning,
                username=username,
            )
        )

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
        """
        Query audit records with filtering and pagination.

        Args:
            job_id: Filter by job ID (optional)
            repo_alias: Filter by repository alias (optional)
            project_language: Filter by project language (optional)
            project_build_system: Filter by build system (optional)
            since: Filter records after this ISO timestamp (optional)
            until: Filter records before this ISO timestamp (optional)
            limit: Maximum records to return (default: 100)
            offset: Number of records to skip (default: 0)

        Returns:
            Tuple of (records list, total count)
            Each record is a dictionary with all audit fields
        """
        records, total = self._backend.query_audit_records(
            job_id=job_id,
            repo_alias=repo_alias,
            project_language=project_language,
            project_build_system=project_build_system,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return list(records), int(total)
