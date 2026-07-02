"""
PostgreSQL backend for SSH key management storage.

Story #414: PostgreSQL Backend for Remaining 6 Backends

Drop-in replacement for SSHKeysSqliteBackend satisfying the SSHKeysBackend protocol.
Uses psycopg v3 sync mode with a connection pool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


class SSHKeysPostgresBackend:
    """
    PostgreSQL backend for SSH key management.

    Satisfies the SSHKeysBackend protocol.
    Accepts a psycopg v3 connection pool in __init__.
    Uses a junction table ssh_key_hosts for host assignments.
    """

    def __init__(self, pool: Any) -> None:
        """
        Initialize the backend.

        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

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
    ) -> None:
        """Create a new SSH key record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO ssh_keys
                   (name, fingerprint, key_type, private_path, public_path,
                    public_key, email, description, created_at, is_imported)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                ),
            )
        logger.info(f"Created SSH key: {name}")

    def get_key(self, name: str) -> Optional[Dict[str, Any]]:
        """Get SSH key details including associated hosts."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT name, fingerprint, key_type, private_path, public_path,
                          public_key, email, description, created_at, imported_at, is_imported
                   FROM ssh_keys WHERE name = %s""",
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
        }

    def _get_hosts_for_key(self, conn: Any, key_name: str) -> List[str]:
        """Get hosts assigned to a key from junction table."""
        cursor = conn.execute(
            "SELECT hostname FROM ssh_key_hosts WHERE key_name = %s", (key_name,)
        )
        return [row[0] for row in cursor.fetchall()]

    def assign_host(self, key_name: str, hostname: str) -> None:
        """Assign a hostname to an SSH key (idempotent)."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO ssh_key_hosts (key_name, hostname)
                   VALUES (%s, %s)
                   ON CONFLICT (key_name, hostname) DO NOTHING""",
                (key_name, hostname),
            )

    def remove_host(self, key_name: str, hostname: str) -> None:
        """Remove a hostname assignment from an SSH key."""
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM ssh_key_hosts WHERE key_name = %s AND hostname = %s",
                (key_name, hostname),
            )

    def delete_key(self, name: str) -> bool:
        """Delete an SSH key and cascade to hosts. Returns True if deleted."""
        with self._pool.connection() as conn:
            cursor = conn.execute("DELETE FROM ssh_keys WHERE name = %s", (name,))
            deleted: bool = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted SSH key: {name}")
        return deleted

    def list_keys(self) -> list:
        """List all SSH keys with their assigned hosts."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT name, fingerprint, key_type, private_path, public_path,
                          public_key, email, description, created_at, imported_at, is_imported
                   FROM ssh_keys"""
            )
            rows = cursor.fetchall()
            result = []
            for row in rows:
                key_name = row[0]
                hosts = self._get_hosts_for_key(conn, key_name)
                result.append(
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
                    }
                )
        return result

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
