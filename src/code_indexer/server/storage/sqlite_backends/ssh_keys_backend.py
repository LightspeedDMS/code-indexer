"""
SQLite backend for SSH key management. Uses junction table ssh_key_hosts.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class SSHKeysSqliteBackend:
    """SQLite backend for SSH key management. Uses junction table ssh_key_hosts."""

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def create_key(
        self,
        name: str,
        fingerprint: str,
        key_type: str,
        private_path: str,
        public_path: str,
        public_key: Optional[str] = None,
        email: Optional[str] = None,
        description: Optional[str] = None,
        is_imported: bool = False,
        private_key: Optional[str] = None,
    ) -> None:
        """Create a new SSH key record."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO ssh_keys (name, fingerprint, key_type, private_path, public_path,
                   public_key, email, description, created_at, is_imported,
                   private_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     fingerprint = excluded.fingerprint,
                     key_type = excluded.key_type,
                     private_path = excluded.private_path,
                     public_path = excluded.public_path,
                     public_key = excluded.public_key,
                     email = excluded.email,
                     description = excluded.description,
                     is_imported = excluded.is_imported,
                     private_key = excluded.private_key""",
                (
                    name,
                    fingerprint,
                    key_type,
                    private_path,
                    public_path,
                    public_key,
                    email,
                    description,
                    now,
                    is_imported,
                    private_key,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Created SSH key: {name}")

    def _get_hosts_for_key(self, conn: Any, key_name: str) -> list:
        """Get hosts for a key from junction table."""
        cursor = conn.execute(
            "SELECT hostname FROM ssh_key_hosts WHERE key_name = ?", (key_name,)
        )
        return [row[0] for row in cursor.fetchall()]

    def get_key(self, name: str) -> Optional[Dict[str, Any]]:
        """Get SSH key details with hosts."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT name, fingerprint, key_type, private_path, public_path, public_key,
               email, description, created_at, imported_at, is_imported,
               private_key FROM ssh_keys WHERE name = ?""",
            (name,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        hosts = self._get_hosts_for_key(conn, name)
        return {
            "name": row[0],
            "fingerprint": row[1],
            "key_type": row[2],
            "private_path": row[3],
            "public_path": row[4],
            "public_key": row[5],
            "email": row[6],
            "description": row[7],
            "created_at": row[8],
            "imported_at": row[9],
            "is_imported": bool(row[10]),
            "hosts": hosts,
            "private_key": row[11],
        }

    def assign_host(self, key_name: str, hostname: str) -> None:
        """Assign a host to a key."""

        def operation(conn):
            conn.execute(
                "INSERT OR IGNORE INTO ssh_key_hosts (key_name, hostname) VALUES (?, ?)",
                (key_name, hostname),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def remove_host(self, key_name: str, hostname: str) -> None:
        """Remove a host from a key."""

        def operation(conn):
            conn.execute(
                "DELETE FROM ssh_key_hosts WHERE key_name = ? AND hostname = ?",
                (key_name, hostname),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def delete_key(self, name: str) -> bool:
        """Delete an SSH key (cascades to hosts)."""

        def operation(conn):
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute("DELETE FROM ssh_keys WHERE name = ?", (name,))
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted SSH key: {name}")
        return deleted

    def list_keys(self) -> list:
        """List all SSH keys with their hosts."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT name, fingerprint, key_type, private_path, public_path, public_key,
               email, description, created_at, imported_at, is_imported,
               private_key FROM ssh_keys"""
        )
        results = []
        for row in cursor.fetchall():
            key_name = row[0]
            hosts = self._get_hosts_for_key(conn, key_name)
            results.append(
                {
                    "name": key_name,
                    "fingerprint": row[1],
                    "key_type": row[2],
                    "private_path": row[3],
                    "public_path": row[4],
                    "public_key": row[5],
                    "email": row[6],
                    "description": row[7],
                    "created_at": row[8],
                    "imported_at": row[9],
                    "is_imported": bool(row[10]),
                    "hosts": hosts,
                    "private_key": row[11],
                }
            )
        return results

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
