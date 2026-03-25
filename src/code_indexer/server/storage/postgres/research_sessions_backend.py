"""
PostgreSQL backend for research sessions storage (Story #522).

Drop-in replacement for ResearchSessionsSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the ResearchSessionsBackend Protocol
(protocols.py).

Tables created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class ResearchSessionsPostgresBackend:
    """
    PostgreSQL backend for research sessions storage.

    Satisfies the ResearchSessionsBackend Protocol (protocols.py).
    All mutations commit immediately after DML execution.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure tables exist.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create research tables and indexes if they do not already exist."""
        with self._pool.connection() as conn:
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
                    id SERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
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
            conn.commit()

    def create_session(
        self,
        session_id: str,
        name: str,
        folder_path: str,
        claude_session_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Insert a new research session record."""
        now = created_at or datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO research_sessions (id, name, folder_path, claude_session_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (session_id, name, folder_path, claude_session_id, now, now),
            )
            conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by ID, or None if not found."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, name, folder_path, claude_session_id, created_at, updated_at "
                "FROM research_sessions WHERE id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "folder_path": row[2],
            "claude_session_id": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions ordered by updated_at DESC."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, name, folder_path, claude_session_id, created_at, updated_at "
                "FROM research_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "folder_path": row[2],
                "claude_session_id": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (CASCADE removes messages). Returns True if found."""
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM research_sessions WHERE id = %s", (session_id,)
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return deleted > 0

    def update_session_title(self, session_id: str, name: str) -> bool:
        """Update session name. Returns True if session was found and updated."""
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            result = conn.execute(
                "UPDATE research_sessions SET name = %s, updated_at = %s WHERE id = %s",
                (name, now, session_id),
            )
            updated = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return updated > 0

    def update_session_claude_id(self, session_id: str, claude_session_id: str) -> None:
        """Store the Claude CLI session ID for a research session."""
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE research_sessions SET claude_session_id = %s, updated_at = %s WHERE id = %s",
                (claude_session_id, now, session_id),
            )
            conn.commit()

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a message and return the full message dict."""
        now = timestamp or datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO research_messages (session_id, role, content, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id, session_id, role, content, created_at
                """,
                (session_id, role, content, now),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("add_message: INSERT RETURNING returned no row")
        return {
            "id": row[0],
            "session_id": row[1],
            "role": row[2],
            "content": row[3],
            "created_at": row[4],
        }

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all messages for a session in insertion order."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, session_id, role, content, created_at "
                "FROM research_messages WHERE session_id = %s ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "role": row[2],
                "content": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
