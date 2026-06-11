"""
Cluster Fernet key helper (Bug #1072, Chunk 2, Step 5).

Provides a race-safe load-or-create pattern for Fernet encryption keys
stored in the cluster_secrets table, so multiple cluster nodes converge
on the same key value.

Extracted from totp_service._load_or_create_cluster_key so the pattern
is reusable by any cluster-mode service (e.g. SSHKeyManager).
"""

from __future__ import annotations

import logging
from typing import Any

from cryptography.fernet import Fernet

try:
    from psycopg.rows import dict_row
except ImportError:
    dict_row = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def load_or_create_fernet_key(pool: Any, key_name: str) -> Fernet:
    """Load (or race-safely create) a Fernet key stored in cluster_secrets.

    Uses INSERT ... ON CONFLICT (key_name) DO NOTHING so that concurrent
    nodes racing to create the key all converge on the single winner written
    by the first INSERT that commits.  After the INSERT + commit the winner
    is determined by a final SELECT.

    Args:
        pool: psycopg3 connection pool (cluster mode).
        key_name: the cluster_secrets key_name
                  (e.g. 'ssh_key_encryption_key').

    Returns:
        A Fernet instance built from the stored (winning) key value.
    """
    with pool.connection() as conn:
        # Try to read an existing key first
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                (key_name,),
            ).fetchone()

        if row:
            key_value: str = row["key_value"]
            logger.info(
                "cluster_key_provider: loaded key '%s' from cluster_secrets",
                key_name,
            )
            return Fernet(key_value.encode())

        # Key not present — generate and insert (race-safe)
        new_key = Fernet.generate_key().decode()
        conn.execute(
            """
            INSERT INTO cluster_secrets (key_name, key_value)
            VALUES (%s, %s)
            ON CONFLICT (key_name) DO NOTHING
            """,
            (key_name, new_key),
        )
        conn.commit()

        # Re-read to get the winner (our INSERT may have lost the race)
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = %s",
                (key_name,),
            ).fetchone()

        key_value = row["key_value"]  # type: ignore[index]
        logger.info(
            "cluster_key_provider: generated and stored key '%s' in cluster_secrets",
            key_name,
        )
        return Fernet(key_value.encode())
