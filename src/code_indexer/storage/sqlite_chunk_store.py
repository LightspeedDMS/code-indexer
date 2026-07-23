"""SQLite-backed chunk-store engine (Story #1455, Epic #1454).

A reusable, collection-agnostic storage primitive that persists chunk
records (vector + full payload + content variant) in a single SQLite file
per collection, replacing the current one-file-per-chunk-JSON design
(`vector_<hash>.json`, 4-level hash-sharded).

Passthrough by construction, not by whitelist
-----------------------------------------------
The current writer's record shape (`FilesystemVectorStore._prepare_vector_data_batch`,
`filesystem_vector_store.py:1876`) looks like::

    {
        "id": "<point_id>",
        "vector": [...],
        "metadata": {"language": ..., "type": ...},   # sibling of payload
        "payload": {...},                              # full search payload
        # plus exactly ONE content variant:
        "chunk_text": "...",                            # OR
        "git_blob_hash": "...", "indexed_with_uncommitted_changes": False,  # OR
        # (nothing -- reconstruct-from-git pointer lives inside payload)
    }

``write_batch`` stores ``id`` and ``vector`` in their own dedicated columns
and treats *every other key* as an opaque, JSON-serialized, zstd-compressed
blob. This is the exact mechanism that prevents the #1361 CIDX2 data-loss
bug: that bug was a hardcoded field WHITELIST silently dropping the
load-bearing ``payload`` dict. Here there is no whitelist of fields to keep
-- only a two-item EXCLUDE-list (``id``, ``vector``) of fields that get
their own columns. Any field present in a record -- known today, or from an
older/legacy shape never anticipated -- survives automatically.

Two write/open modes
---------------------
- MUTABLE (default): a single, long-lived writer connection,
  ``journal_mode=DELETE``. Used for the active base clone during indexing.
  Supports payload-only field merges (mirrors
  ``FilesystemVectorStore._batch_update_payload_only``) and point deletion
  (mirrors ``FilesystemVectorStore.delete_points``).
- IMMUTABLE: a fresh connection opened with the ``immutable=1`` SQLite URI
  parameter. Used ONLY for published, versioned snapshots. Callers MUST
  decide mutable-vs-immutable via the existing
  ``is_immutable_versioned_snapshot()`` predicate
  (``server/services/query_path_cache.py``) -- see
  :func:`open_chunk_store_for_path`. This module never invents a parallel
  predicate, and never opens a path in immutable mode unless the caller
  explicitly requests it: opening a mutating file with ``immutable=1`` is a
  correctness/corruption bug, not a perf nit.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Union

import numpy as np
import zstandard

_VECTOR_DTYPE = "<f4"  # little-endian float32, per AC3


class ChunkStoreError(Exception):
    """Base exception for chunk-store engine errors."""


class InvalidVectorError(ChunkStoreError):
    """Raised when a vector fails dtype/dimension validation."""


class NonFiniteVectorError(ChunkStoreError):
    """Raised when a vector contains NaN or +/-inf at write time.

    AC3: this is a NEW check that does not exist in today's writer (which
    validates dtype/dimension but not finiteness). Rejected loudly (raise),
    never silently coerced or dropped.
    """


class ImmutableChunkStoreError(ChunkStoreError):
    """Raised when a write is attempted against an immutable-mode store."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    point_id TEXT PRIMARY KEY,
    path TEXT,
    vector BLOB NOT NULL,
    data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

CREATE TABLE IF NOT EXISTS chunk_store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_RESERVED_KEYS = ("id", "vector")


class ChunkStore:
    """SQLite-backed chunk-store engine for a single collection.

    Reusable primitive: open/mutable/immutable, write-batch, read-by-point_id,
    stream-all, delete, payload-update. Out of scope: HNSW/id/path index
    files -- this engine only manages the ``chunks.db``-equivalent store.
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        *,
        immutable: bool = False,
        expected_dim: Optional[int] = None,
    ) -> None:
        """Open (creating if needed) a chunk store at ``db_path``.

        Args:
            db_path: Path to the ``chunks.db``-equivalent SQLite file.
            immutable: When True, opens a FRESH connection with the SQLite
                ``immutable=1`` URI parameter (read-only; all writes raise
                ``ImmutableChunkStoreError``). Callers should determine this
                via :func:`open_chunk_store_for_path`, not by guessing.
            expected_dim: Optional known vector dimension. When omitted, the
                dimension is inferred from the first vector ever written and
                persisted so it is enforced across sessions too.
        """
        self.db_path = Path(db_path)
        self._immutable = immutable
        self._compressor = zstandard.ZstdCompressor()
        self._decompressor = zstandard.ZstdDecompressor()
        self._conn = self._open_connection()
        self._expected_dim = expected_dim
        if not immutable:
            self._ensure_schema()
            if self._expected_dim is None:
                self._expected_dim = self._load_persisted_dim()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        if self._immutable:
            uri = f"file:{self.db_path}?immutable=1"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA journal_mode=DELETE")
        return conn

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ChunkStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Vector dimension bookkeeping (AC3: "preserve existing dtype/dimension
    # validation behavior" -- self-consistent per-collection dimension,
    # persisted so the invariant survives reopening the store).
    # ------------------------------------------------------------------

    def _load_persisted_dim(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT value FROM chunk_store_meta WHERE key = 'vector_dim'"
        ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def _persist_dim(self, dim: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO chunk_store_meta (key, value) VALUES ('vector_dim', ?)",
            (str(dim),),
        )

    # ------------------------------------------------------------------
    # Vector encode/decode (AC3)
    # ------------------------------------------------------------------

    def _encode_vector(self, point_id: str, vector: Sequence[float]) -> bytes:
        try:
            arr = np.asarray(vector)
        except (ValueError, TypeError) as exc:
            raise InvalidVectorError(
                f"Point {point_id} has a malformed vector that numpy cannot "
                f"convert to an array: {exc}"
            ) from exc

        # Reject anything that isn't already an integer/unsigned/float kind
        # up front (covers object-dtype AND pure-string arrays such as
        # ["not", "a", "number"], which numpy happily parses to a unicode
        # dtype rather than object dtype).
        if arr.dtype.kind not in ("i", "u", "f"):
            raise InvalidVectorError(
                f"Point {point_id} has invalid vector with dtype={arr.dtype}. "
                f"Vector contains non-numeric values."
            )

        try:
            f32 = np.asarray(arr, dtype=_VECTOR_DTYPE)
        except (ValueError, TypeError) as exc:
            raise InvalidVectorError(
                f"Point {point_id} has invalid vector that cannot be cast to "
                f"float32: {exc}"
            ) from exc

        if self._expected_dim is not None and f32.shape[0] != self._expected_dim:
            raise InvalidVectorError(
                f"Point {point_id} has vector dimension {f32.shape[0]}, "
                f"expected {self._expected_dim}"
            )

        if not np.isfinite(f32).all():
            raise NonFiniteVectorError(
                f"Point {point_id} has a non-finite vector (NaN or inf). "
                f"Rejected at write time -- never silently coerced."
            )

        if self._expected_dim is None:
            self._expected_dim = int(f32.shape[0])
            self._persist_dim(self._expected_dim)

        return f32.tobytes()

    @staticmethod
    def _decode_vector(blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=_VECTOR_DTYPE)

    # ------------------------------------------------------------------
    # Opaque data encode/decode (AC1: full field passthrough by construction)
    # ------------------------------------------------------------------

    def _encode_data(self, record: Dict[str, Any]) -> bytes:
        passthrough = {k: v for k, v in record.items() if k not in _RESERVED_KEYS}
        raw = json.dumps(passthrough).encode("utf-8")
        return self._compressor.compress(raw)

    def _decode_data(self, blob: bytes) -> Dict[str, Any]:
        raw = self._decompressor.decompress(blob)
        result: Dict[str, Any] = json.loads(raw.decode("utf-8"))
        return result

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _require_mutable(self) -> None:
        if self._immutable:
            raise ImmutableChunkStoreError(
                f"Chunk store at {self.db_path} was opened immutable=1 -- "
                f"writes are rejected. Opening a mutating path with "
                f"immutable=1 is a correctness bug, not a perf nit."
            )

    def write_batch(self, records: list) -> None:
        """Upsert a batch of chunk records in a single transaction.

        Each record must contain at least ``id`` and ``vector``. Every other
        key is preserved verbatim (passthrough by construction).
        """
        self._require_mutable()
        if not records:
            return

        rows = []
        for record in records:
            point_id = record["id"]
            vector_blob = self._encode_vector(point_id, record["vector"])
            data_blob = self._encode_data(record)
            path = record.get("payload", {}).get("path")
            rows.append((point_id, path, vector_blob, data_blob))

        self._conn.executemany(
            "INSERT OR REPLACE INTO chunks (point_id, path, vector, data) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _row_to_record(
        self, point_id: str, vector_blob: bytes, data_blob: bytes
    ) -> Dict[str, Any]:
        record = self._decode_data(data_blob)
        record["id"] = point_id
        record["vector"] = self._decode_vector(vector_blob)
        return record

    def read(self, point_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT point_id, vector, data FROM chunks WHERE point_id = ?",
            (point_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row[0], row[1], row[2])

    def stream_all(self):
        """Yield every stored record, one at a time.

        Uses a dedicated cursor so callers can iterate the full collection
        without materializing it in memory -- the primitive later stories
        (HNSW rebuild, id/path-index rebuild) need to replace their current
        ``rglob``-based readers. The cursor is guaranteed closed even if the
        caller stops iterating early or an exception propagates mid-stream.
        """
        cursor = self._conn.execute("SELECT point_id, vector, data FROM chunks")
        try:
            for point_id, vector_blob, data_blob in cursor:
                yield self._row_to_record(point_id, vector_blob, data_blob)
        finally:
            cursor.close()

    def count(self) -> int:
        """Return the number of chunk records currently stored."""
        row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return int(row[0])

    def all_point_ids(self) -> "set[str]":
        """Return the set of every stored point_id (Story #1456 AC7).

        Lightweight primary-key scan -- no data/vector decode. This is the
        SQLite-backed replacement for the retired ``id_index.bin`` point-id
        set for CHUNKS_DB-layout collections.
        """
        rows = self._conn.execute("SELECT point_id FROM chunks").fetchall()
        return {row[0] for row in rows}

    def distinct_paths(self) -> "set[str]":
        """Return the set of distinct non-null ``path`` values (Story #1456
        AC7). Uses the indexed ``path`` column -- no data/vector decode.
        Records with no path (NULL) are excluded.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT path FROM chunks WHERE path IS NOT NULL"
        ).fetchall()
        return {row[0] for row in rows}

    # ------------------------------------------------------------------
    # Payload-only update (AC4: mirrors
    # FilesystemVectorStore._batch_update_payload_only -- merge only the
    # specified payload fields; vector, chunk_text, metadata, and every
    # other payload key are preserved exactly as stored.)
    # ------------------------------------------------------------------

    def update_payload_fields(self, point_id: str, fields: Dict[str, Any]) -> bool:
        """Merge ``fields`` into the stored record's payload dict.

        Returns True if the point existed and was updated, False if the
        point was not found (mirrors the "skip gracefully" behavior of
        ``_batch_update_payload_only``).
        """
        self._require_mutable()
        row = self._conn.execute(
            "SELECT data FROM chunks WHERE point_id = ?", (point_id,)
        ).fetchone()
        if row is None:
            return False

        record = self._decode_data(row[0])
        existing_payload = record.get("payload", {})
        for key, value in fields.items():
            existing_payload[key] = value
        record["payload"] = existing_payload

        new_path = existing_payload.get("path")
        new_data_blob = self._compressor.compress(json.dumps(record).encode("utf-8"))
        self._conn.execute(
            "UPDATE chunks SET path = ?, data = ? WHERE point_id = ?",
            (new_path, new_data_blob, point_id),
        )
        self._conn.commit()
        return True

    def update_payload_fields_batch(self, updates: list) -> int:
        """Apply a batch of ``(point_id, fields)`` payload merges in ONE
        transaction (CLAUDE.md store_batch guidance: one commit, not one
        per row). Points not found are skipped gracefully. Returns the
        count of points actually updated.
        """
        self._require_mutable()
        if not updates:
            return 0

        updated_count = 0
        for point_id, fields in updates:
            row = self._conn.execute(
                "SELECT data FROM chunks WHERE point_id = ?", (point_id,)
            ).fetchone()
            if row is None:
                continue

            record = self._decode_data(row[0])
            existing_payload = record.get("payload", {})
            for key, value in fields.items():
                existing_payload[key] = value
            record["payload"] = existing_payload

            new_path = existing_payload.get("path")
            new_data_blob = self._compressor.compress(
                json.dumps(record).encode("utf-8")
            )
            self._conn.execute(
                "UPDATE chunks SET path = ?, data = ? WHERE point_id = ?",
                (new_path, new_data_blob, point_id),
            )
            updated_count += 1

        self._conn.commit()
        return updated_count

    # ------------------------------------------------------------------
    # Delete (AC4: mirrors FilesystemVectorStore.delete_points)
    # ------------------------------------------------------------------

    _DELETE_CHUNK_SIZE = 500  # stay well under SQLite's ~999 variable limit

    def delete(self, point_ids: list) -> int:
        """Delete a batch of points by id. Returns the number deleted.

        Non-existent ids are silently skipped (no-op), matching the
        existing filesystem-backed ``delete_points`` behavior. Deletion is
        chunked to respect SQLite's bound on the number of host parameters
        per statement (Messi Rule #14: bounded loops only).
        """
        self._require_mutable()
        if not point_ids:
            return 0

        deleted_total = 0
        for start in range(0, len(point_ids), self._DELETE_CHUNK_SIZE):
            chunk = point_ids[start : start + self._DELETE_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self._conn.execute(
                f"DELETE FROM chunks WHERE point_id IN ({placeholders})",
                chunk,
            )
            deleted_total += cursor.rowcount

        self._conn.commit()
        return deleted_total


# ---------------------------------------------------------------------------
# Immutable-mode gating factory (AC5)
# ---------------------------------------------------------------------------
#
# The predicate deciding mutable-vs-immutable lives in
# ``code_indexer.server.services.query_path_cache`` -- a SERVER-only module.
# It is imported lazily (function-local import, not module-level) so that
# the CLI startup path never pays for it unless a caller actually asks for
# immutable-mode gating. Unlike the module's other optional imports (e.g.
# ``coalesced_query_embedding``), which have a legitimate "not wired up in
# this environment" no-op fallback, there is NO safe fallback for THIS
# predicate: silently guessing mutable-vs-immutable when the real predicate
# cannot even be imported is precisely the kind of correctness gamble AC5
# exists to prevent (Messi Rule #2, Anti-Fallback) -- an unresolvable
# predicate must fail loudly (ImportError), never default to a guess. This
# module NEVER reimplements the predicate's logic -- it only calls the real
# function object.


def _resolve_immutable_predicate():
    """Return the real ``is_immutable_versioned_snapshot`` function object.

    Raises ImportError if the server package is unavailable. There is no
    fallback: guessing the mutable/immutable decision would risk exactly
    the corruption this gate exists to prevent.
    """
    from code_indexer.server.services.query_path_cache import (
        is_immutable_versioned_snapshot,
    )

    return is_immutable_versioned_snapshot


def open_chunk_store_for_path(
    db_path: Union[str, Path], collection_path: str
) -> ChunkStore:
    """Open a :class:`ChunkStore`, deciding mutable vs. immutable via the
    EXISTING ``is_immutable_versioned_snapshot()`` predicate applied to
    ``collection_path`` -- the same predicate that already gates
    ``skip_staleness_check`` (Bug #1181). Do NOT invent a parallel
    predicate; do NOT guess.

    Args:
        db_path: Path to the ``chunks.db``-equivalent SQLite file to open.
        collection_path: The collection directory path to test against the
            predicate (e.g. the base-clone collection path, or a
            ``.versioned/{alias}/v_<ts>/{collection}`` snapshot path).
    """
    predicate = _resolve_immutable_predicate()
    immutable = predicate(collection_path)
    return ChunkStore(db_path, immutable=immutable)
