"""
SQLite backend for hidden discovery repo storage (Story #719).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import List, Set

from ..database_manager import DatabaseConnectionManager


class HiddenDiscoveryReposSqliteBackend:
    """SQLite backend for hidden discovery repo storage (Story #719)."""

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def add_hidden_repo(self, repo_identifier: str) -> None:
        """Add a repo to the hidden list. Idempotent — duplicate adds are no-ops."""
        if not repo_identifier or not isinstance(repo_identifier, str):
            raise ValueError("repo_identifier must be a non-empty string")

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO hidden_discovery_repos (repo_identifier) VALUES (?)",
                (repo_identifier,),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def remove_hidden_repo(self, repo_identifier: str) -> bool:
        """Remove a repo from the hidden list. Returns True if removed, False if not found."""
        if not repo_identifier or not isinstance(repo_identifier, str):
            raise ValueError("repo_identifier must be a non-empty string")

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM hidden_discovery_repos WHERE repo_identifier = ?",
                (repo_identifier,),
            )
            return cursor.rowcount > 0

        return self._conn_manager.execute_atomic(operation)

    def list_hidden_repos(self) -> List[str]:
        """Return all hidden repo identifiers ordered by most recently hidden."""
        # Uses thread-local connection managed by DatabaseConnectionManager (same pattern
        # as all other read methods in this file — connection lifecycle is pool-managed).
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT repo_identifier FROM hidden_discovery_repos ORDER BY hidden_at DESC"
        )
        return [row[0] for row in cursor.fetchall()]

    def get_hidden_set(self) -> Set[str]:
        """Return hidden repo identifiers as a set for O(1) membership checks."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute("SELECT repo_identifier FROM hidden_discovery_repos")
        return {row[0] for row in cursor.fetchall()}

    def is_repo_hidden(self, repo_identifier: str) -> bool:
        """Check if a specific repo is hidden."""
        if not repo_identifier or not isinstance(repo_identifier, str):
            raise ValueError("repo_identifier must be a non-empty string")
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT 1 FROM hidden_discovery_repos WHERE repo_identifier = ? LIMIT 1",
            (repo_identifier,),
        )
        return cursor.fetchone() is not None
