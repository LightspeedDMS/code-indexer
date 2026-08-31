"""PostgreSQL backend for temporal metadata storage (Bug #1313).

Root cause: TemporalMetadataStore (Story #669) was a SQLite-WAL database
that, in cluster mode, lives on the shared NFS golden-repos mount. NFS cannot
satisfy SQLite WAL's `-shm` requirement, and every commit paid an NFS fsync,
serializing all 8 indexing threads on the same lock. This backend replaces
ONLY the storage engine (schema/operations are identical to the SQLite
backend) with PostgreSQL -- eliminating the NFS bottleneck.

Satisfies the TemporalMetadataBackend Protocol
(code_indexer/storage/temporal_metadata_backend.py). Schema (the
``temporal_metadata`` table and its two indexes) is owned entirely by the
SQL migration (storage/postgres/migrations/sql/033_temporal_metadata.sql)
-- this backend does NOT create or alter any table. `service_init.py`
always runs `MigrationRunner` before `StorageFactory.create_backends()`
builds the backend_registry, and this backend's only production
construction path (a factory installed in `startup/lifespan.py`) runs
strictly after that same backend_registry already exists -- so schema is
guaranteed present by the time any instance is constructed (Issue #1697,
mirroring Bug #1655/#1662: the previous defensive
``CREATE TABLE IF NOT EXISTS`` self-heal here -- byte-identical to the
migration -- was dead code in every real deployment, removed rather than
kept as a second copy of the schema).

Unlike SQLite (one .db file per collection), one PostgreSQL table holds every
collection's rows -- all operations are scoped by ``collection_key`` (derived
from the collection path by TemporalMetadataStore, see temporal_metadata_store.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from code_indexer.storage.temporal_metadata_store import (
    COLLECTION_KEY_LENGTH,
    canonical_content_digest_rows,
    generate_hash_prefix,
)

logger = logging.getLogger(__name__)


class TemporalMetadataPostgresBackend:
    """PostgreSQL backend for temporal collection metadata storage.

    Satisfies the TemporalMetadataBackend Protocol. All mutations commit
    immediately after executing the DML statement.
    """

    def __init__(self, pool: Any, collection_key: str) -> None:
        """Initialize with a shared connection pool.

        Schema is assumed to already exist (see module docstring) -- this
        constructor does not touch the database.

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

    def content_digest(self) -> str:
        """Deterministic sha256 digest of every row this scope holds.

        Issue #1548 round-4 exploit fix: mirrors
        ``TemporalMetadataSqliteBackend.content_digest`` exactly (same
        selected columns, same ordering, same JSON encoding) so a legacy
        SQLite scope and a fixed-root PostgreSQL scope holding the SAME
        rows always produce the SAME digest, regardless of which concrete
        backend either side happens to use. See the SQLite implementation's
        docstring for why ``created_at``/``format_version`` are excluded.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT hash_prefix, point_id, commit_hash, file_path, chunk_index
                FROM temporal_metadata
                WHERE collection_key = %s
                ORDER BY hash_prefix, point_id, commit_hash, file_path, chunk_index
                """,
                (self._collection_key,),
            ).fetchall()
        # Issue #1548 round-5 secondary finding 4: re-sort in Python with a
        # NULL-order-neutral key, independent of PostgreSQL's own NULL-last
        # ORDER BY default -- see canonical_content_digest_rows()'s
        # docstring for why the bare ORDER BY above alone cannot guarantee
        # agreement with the SQLite backend's digest for the SAME rows.
        rows = canonical_content_digest_rows(rows)
        encoded = json.dumps(
            [list(row) for row in rows],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def copy_collection_scope(
        self,
        target_collection_path: Path,
        *,
        pre_commit_check: Optional[Callable[[], None]] = None,
    ) -> None:
        """Additively re-key rows; retain the source key for compatibility.

        Issue #1548 round-10 Finding 1: ``pre_commit_check`` (if given) is
        invoked immediately before this transaction's own ``conn.
        commit()`` -- the narrowest achievable window between "rows
        written" and "durably committed". Raising here, before the
        commit, triggers ``psycopg``'s own automatic rollback-on-exception
        for a connection borrowed via ``pool.connection()`` as a context
        manager -- no explicit ``conn.rollback()`` is needed; the
        exception propagates unchanged and the write is never committed.
        """
        target_key = hashlib.sha256(str(target_collection_path).encode()).hexdigest()[
            : len(self._collection_key)
        ]
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO temporal_metadata
                    (collection_key, hash_prefix, point_id, commit_hash,
                     file_path, chunk_index, created_at, format_version)
                SELECT %s, hash_prefix, point_id, commit_hash, file_path,
                       chunk_index, created_at, format_version
                FROM temporal_metadata
                WHERE collection_key = %s
                ON CONFLICT (collection_key, hash_prefix) DO UPDATE SET
                    point_id = EXCLUDED.point_id,
                    commit_hash = EXCLUDED.commit_hash,
                    file_path = EXCLUDED.file_path,
                    chunk_index = EXCLUDED.chunk_index,
                    created_at = EXCLUDED.created_at,
                    format_version = EXCLUDED.format_version
                """,
                (target_key, self._collection_key),
            )
            if pre_commit_check is not None:
                pre_commit_check()
            conn.commit()

    def delete_collection_scope(self) -> None:
        """Delete this collection key; callers provide the authorization gate."""
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM temporal_metadata WHERE collection_key = %s",
                (self._collection_key,),
            )
            conn.commit()


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
