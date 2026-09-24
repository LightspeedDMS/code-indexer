"""
SQLite backend for research sessions and messages storage (Story #522).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager


class ResearchSessionsSqliteBackend:
    """
    SQLite backend for research sessions and messages storage (Story #522).

    Satisfies the ResearchSessionsBackend Protocol.
    Uses the main cidx_server.db (research tables already live there).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create research tables if they do not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claude_session_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES research_sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_research_messages_session_id
                ON research_messages(session_id)
                """
            )

        self._conn_manager.execute_atomic(_op)

    def create_session(
        self,
        session_id: str,
        name: str,
        folder_path: str,
        claude_session_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Insert a new research session record."""
        from datetime import datetime, timezone

        now = created_at or datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO research_sessions (id, name, folder_path, claude_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, name, folder_path, claude_session_id, now, now),
            )

        self._conn_manager.execute_atomic(_op)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by ID, or None if not found."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, name, folder_path, claude_session_id, created_at, updated_at "
            "FROM research_sessions WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions ordered by updated_at DESC."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, name, folder_path, claude_session_id, created_at, updated_at "
            "FROM research_sessions ORDER BY updated_at DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (CASCADE removes messages). Returns True if found."""
        result: Dict[str, Any] = {"found": False}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "DELETE FROM research_sessions WHERE id = ?", (session_id,)
            )
            result["found"] = cursor.rowcount > 0

        self._conn_manager.execute_atomic(_op)
        return bool(result["found"])

    def update_session_title(self, session_id: str, name: str) -> bool:
        """Update session name. Returns True if session was found and updated."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        result: Dict[str, Any] = {"found": False}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "UPDATE research_sessions SET name = ?, updated_at = ? WHERE id = ?",
                (name, now, session_id),
            )
            result["found"] = cursor.rowcount > 0

        self._conn_manager.execute_atomic(_op)
        return bool(result["found"])

    def update_session_claude_id(self, session_id: str, claude_session_id: str) -> None:
        """Store the Claude CLI session ID for a research session."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE research_sessions SET claude_session_id = ?, updated_at = ? WHERE id = ?",
                (claude_session_id, now, session_id),
            )

        self._conn_manager.execute_atomic(_op)

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a message and return the full message dict."""
        from datetime import datetime, timezone

        now = timestamp or datetime.now(timezone.utc).isoformat()
        result: Dict[str, Any] = {"message_id": None}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "INSERT INTO research_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            result["message_id"] = cursor.lastrowid

        self._conn_manager.execute_atomic(_op)

        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, session_id, role, content, created_at FROM research_messages WHERE id = ?",
            (result["message_id"],),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                f"add_message: inserted row id={result['message_id']} not found after INSERT"
            )
        return dict(row)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all messages for a session in insertion order."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, session_id, role, content, created_at "
            "FROM research_messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
