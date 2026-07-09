"""PostgreSQL backend for temporal metadata storage (Bug #1313).

Root cause: TemporalMetadataStore (Story #669) was a SQLite-WAL database
that, in cluster mode, lives on the shared NFS golden-repos mount. NFS cannot
satisfy SQLite WAL's `-shm` requirement, and every commit paid an NFS fsync,
serializing all 8 indexing threads on the same lock. This backend replaces
ONLY the storage engine (schema/operations are identical to the SQLite
backend) with PostgreSQL -- eliminating the NFS bottleneck.

Satisfies the TemporalMetadataBackend Protocol
(code_indexer/storage/temporal_metadata_backend.py). Table created on first
use via the migration (033_temporal_metadata.sql); ``_ensure_schema`` also
runs a CREATE TABLE IF NOT EXISTS defensively (mirrors
payload_cache_backend.py, which has no separate migration).

Unlike SQLite (one .db file per collection), one PostgreSQL table holds every
collection's rows -- all operations are scoped by ``collection_key`` (derived
from the collection path by TemporalMetadataStore, see temporal_metadata_store.py).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from code_indexer.storage.temporal_metadata_store import (
    COLLECTION_KEY_LENGTH,
    generate_hash_prefix,
)

logger = logging.getLogger(__name__)


class TemporalMetadataPostgresBackend:
    """PostgreSQL backend for temporal collection metadata storage.

    Satisfies the TemporalMetadataBackend Protocol. All mutations commit
    immediately after executing the DML statement.
    """

    def __init__(self, pool: Any, collection_key: str) -> None:
        """Initialize with a shared connection pool and ensure the table exists.

        Args:
            pool: A psycopg v3 ConnectionPool instance (see connection_pool.py).
                Typed ``Any`` (not ``ConnectionPool``) so importing this module
                never requires psycopg_pool to be installed -- mirrors the
                precedent in ci_tokens_backend.py (Bug #1313 review Finding 1).
            collection_key: Opaque identifier scoping all rows written/read by
                this backend instance to a single logical temporal collection
                (derived by TemporalMetadataStore from the collection path).
        """
        self._pool = pool
        self._collection_key = collection_key
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the temporal_metadata table and indexes if not already
        present.

        Additive/idempotent -- the migration (033_temporal_metadata.sql) is
        the primary path; this is a defensive fallback mirroring
        payload_cache_backend.py's _ensure_schema. The DDL below is
        byte-consistent with 033_temporal_metadata.sql (same table, same
        columns/types, same primary key, same two indexes).

        Bug #1313 round-2 rework (Codex Finding B): a failure here MUST NOT
        be swallowed. Previously this method caught every exception, logged
        a warning, and returned a constructed backend anyway -- so a missing
        migration, wrong DB permissions, or a broken table would defer the
        failure to the first temporal write/read instead of failing at
        storage initialization. Log at ERROR then re-raise so construction
        itself fails loudly.
        """
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS temporal_metadata (
                        collection_key TEXT NOT NULL,
                        hash_prefix TEXT NOT NULL,
                        point_id TEXT NOT NULL,
                        commit_hash TEXT,
                        file_path TEXT,
                        chunk_index INTEGER,
                        created_at TEXT,
                        format_version INTEGER NOT NULL DEFAULT 2,
                        PRIMARY KEY (collection_key, hash_prefix)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_temporal_meta_pointid
                        ON temporal_metadata (collection_key, point_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_temporal_meta_commit
                        ON temporal_metadata (collection_key, commit_hash)
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.error(
                "TemporalMetadataPostgresBackend: schema setup failed: %s",
                exc,
                exc_info=True,
            )
            raise

    # Bug #1313 review Finding 4: hash_prefix is deterministically derived
    # from point_id via generate_hash_prefix (sha256(point_id)[:16]); since
    # hash_prefix is never caller-supplied or persisted independently of
    # point_id -- it is always freshly recomputed from point_id right below,
    # every single call -- the same point_id always produces the same
    # hash_prefix. Therefore ON CONFLICT (collection_key, hash_prefix) never
    # leaves a stale row that would violate UNIQUE(collection_key, point_id)
    # (idx_temporal_meta_pointid): ON CONFLICT and true replace-by-point_id
    # semantics coincide for every write this backend performs.
    def save_metadata_batch(self, rows: List[Tuple[str, Dict]]) -> List[str]:
        """Save metadata for multiple points in ONE transaction/commit.

        Bug #1313: this replaces N per-vector SQLite connect/commit cycles
        (each paying an NFS fsync) with one PostgreSQL transaction, with
        ``SET LOCAL synchronous_commit = off`` relaxing WAL fsync for these
        ephemeral, deterministically-reconstructable rows (Bug #1181 pattern).

        Args:
            rows: List of (point_id, payload) tuples.

        Returns:
            List of 16-char hash prefixes in the same order as input rows.
        """
        if not rows:
            return []

        created_at = datetime.now().isoformat()
        hash_prefixes: List[str] = []
        params = []
        for point_id, payload in rows:
            hash_prefix = generate_hash_prefix(point_id)
            hash_prefixes.append(hash_prefix)
            commit_hash = payload.get("commit_hash", "")
            file_path = payload.get("path", "")
            chunk_index = payload.get("chunk_index", 0)
            params.append(
                (
                    self._collection_key,
                    hash_prefix,
                    point_id,
                    commit_hash,
                    file_path,
                    chunk_index,
                    created_at,
                )
            )

        with self._pool.connection() as conn:
            # Bug #1181 pattern: relax durability for these ephemeral,
            # deterministically-reconstructable rows. SET LOCAL is
            # per-transaction; does not affect users/jobs/migrations.
            conn.execute("SET LOCAL synchronous_commit = off")
            # psycopg v3: executemany lives on the cursor, NOT the connection
            # (memory feedback_faithful_db_mocks).
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO temporal_metadata
                        (collection_key, hash_prefix, point_id, commit_hash,
                         file_path, chunk_index, created_at, format_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 2)
                    ON CONFLICT (collection_key, hash_prefix) DO UPDATE SET
                        point_id = EXCLUDED.point_id,
                        commit_hash = EXCLUDED.commit_hash,
                        file_path = EXCLUDED.file_path,
                        chunk_index = EXCLUDED.chunk_index,
                        created_at = EXCLUDED.created_at,
                        format_version = EXCLUDED.format_version
                    """,
                    params,
                )
            conn.commit()

        return hash_prefixes

    def save_metadata(self, point_id: str, payload: Dict) -> str:
        """Save metadata for a single point and return its hash prefix."""
        hash_prefixes = self.save_metadata_batch([(point_id, payload)])
        return hash_prefixes[0]

    def checkpoint_wal(self) -> None:
        """No-op: PostgreSQL has no per-file WAL to checkpoint from the client."""
        return None

    def get_point_id(self, hash_prefix: str) -> Optional[str]:
        """Retrieve point_id from hash prefix, scoped to this collection_key."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT point_id FROM temporal_metadata
                WHERE collection_key = %s AND hash_prefix = %s
                """,
                (self._collection_key, hash_prefix),
            ).fetchone()
        return row[0] if row else None

    def get_metadata(self, hash_prefix: str) -> Optional[Dict]:
        """Retrieve full metadata from hash prefix, scoped to this collection_key."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT point_id, commit_hash, file_path, chunk_index, created_at
                FROM temporal_metadata
                WHERE collection_key = %s AND hash_prefix = %s
                """,
                (self._collection_key, hash_prefix),
            ).fetchone()
        if row is None:
            return None
        return {
            "point_id": row[0],
            "commit_hash": row[1],
            "file_path": row[2],
            "chunk_index": row[3],
            "created_at": row[4],
        }

    def delete_metadata(self, hash_prefix: str) -> None:
        """Delete a metadata entry, scoped to this collection_key."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                DELETE FROM temporal_metadata
                WHERE collection_key = %s AND hash_prefix = %s
                """,
                (self._collection_key, hash_prefix),
            )
            conn.commit()

    def cleanup_stale_metadata(self, valid_hash_prefixes: Set[str]) -> int:
        """Remove entries without a corresponding vector file, scoped to this
        collection_key.

        Args:
            valid_hash_prefixes: Set of hash prefixes that have vector files.

        Returns:
            Number of stale entries removed.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT hash_prefix FROM temporal_metadata
                WHERE collection_key = %s
                """,
                (self._collection_key,),
            ).fetchall()
            all_prefixes = {row[0] for row in rows}
            stale_prefixes = all_prefixes - valid_hash_prefixes

            if stale_prefixes:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM temporal_metadata
                        WHERE collection_key = %s AND hash_prefix = ANY(%s)
                        """,
                        (self._collection_key, list(stale_prefixes)),
                    )
                conn.commit()
                logger.info(
                    "TemporalMetadataPostgresBackend: cleaned up %d stale "
                    "metadata entries (collection_key=%s)",
                    len(stale_prefixes),
                    self._collection_key,
                )

        return len(stale_prefixes)

    def count_entries(self) -> int:
        """Count total metadata entries scoped to this collection_key."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM temporal_metadata
                WHERE collection_key = %s
                """,
                (self._collection_key,),
            ).fetchone()
        return row[0] if row else 0


def make_postgres_temporal_metadata_factory(
    pool: Any,
) -> Callable[[Path], "TemporalMetadataPostgresBackend"]:
    """Build the PG temporal-metadata-backend factory bound to *pool*.

    Bug #1313 round-3: this is the SINGLE shared definition of the PG
    factory shape. Two call sites construct a factory this way and MUST
    compute an identical collection_key for the same collection_path so
    server-side reads and child-process writes (the CIDX_TEMPORAL_PG_BOOTSTRAP_DIR
    contract, see temporal_child_wiring.py) agree on where rows live:

      1. server/startup/lifespan.py -- installs the factory in-process,
         bound to the server's own PostgreSQL connection pool, for any
         TemporalMetadataStore constructed directly inside the server
         process (e.g. dashboard_service.py reads).
      2. server/storage/postgres/temporal_child_wiring.py -- installs the
         factory inside a CHILD `cidx index --index-commits` subprocess,
         bound to a fresh pool built from the bootstrap config.json the
         parent pointed it at, for the actual temporal indexing writes.

    Args:
        pool: A psycopg v3 ConnectionPool instance (see connection_pool.py).
            Typed ``Any`` for the same reason as the constructor above --
            importing this module must never require psycopg_pool.

    Returns:
        A callable taking a collection_path and returning a
        TemporalMetadataPostgresBackend scoped to
        sha256(str(collection_path))[:COLLECTION_KEY_LENGTH].
    """

    def _factory(collection_path: Path) -> "TemporalMetadataPostgresBackend":
        collection_key = hashlib.sha256(str(collection_path).encode()).hexdigest()[
            :COLLECTION_KEY_LENGTH
        ]
        return TemporalMetadataPostgresBackend(pool, collection_key=collection_key)

    return _factory
