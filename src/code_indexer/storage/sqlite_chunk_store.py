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
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Sequence, Union

import numpy as np
import zstandard

logger = logging.getLogger(__name__)

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
            # Issue #1459 remediation (post-review follow-up, same class as
            # round-2 Finding A): a naive f"file:{path}?immutable=1" string
            # mis-parses any path containing URI-special characters
            # ('?', '#', '%', spaces, unicode) -- SQLite's URI parser reads
            # a literal '?'/'#' in the path as the start of the
            # query/fragment, truncating the path before "immutable=1" is
            # even seen. This either fails to find the real file (false
            # "no data") or opens the truncated path in SQLite's default
            # read-write-create mode, creating a stray file --
            # Path.resolve().as_uri() produces a correctly percent-encoded
            # file:// URI, matching chunk_store_has_real_data's fix.
            uri = f"{Path(self.db_path).resolve().as_uri()}?immutable=1"
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

    def stream_for_index_rebuild(self, need_payload: bool):
        """Yield ``(point_id, vector, path, payload)`` for every stored
        record, WITHOUT the full-record decode ``stream_all()`` always pays
        for (Story #1461 salvage item #9, Epic #1454).

        HNSW rebuild only ever needs the vector, the point_id, and the
        top-level indexed ``path`` column in the common (unfiltered /
        visible_files-filtered) case -- it decodes the opaque ``data`` blob
        ONLY to read ``hidden_branches`` for the Bug #306 branch-visibility
        filter. Decompressing + JSON-parsing the ENTIRE text corpus
        (payload + chunk_text/git_blob_hash + diff) to read one column that
        already has its own dedicated, indexed SQL column is pure waste.

        Args:
            need_payload: When False, selects only ``point_id, vector,
                path`` -- the ``data`` column is never read, so
                ``_decode_data``/zstd-decompress/json.loads is never
                invoked, and ``payload`` is always yielded as ``None``.
                When True, decodes ``data`` exactly like ``stream_all()``
                and yields the decoded ``payload`` dict (``record.get(
                "payload", {})``) as the fourth element -- required for the
                hidden_branches filter.

        Byte-identical in result to reading the equivalent fields off
        ``stream_all()``'s records -- this is a pure I/O optimization, not
        a behavior change. The cursor is guaranteed closed even if the
        caller stops iterating early or an exception propagates mid-stream,
        mirroring ``stream_all()``'s own contract.
        """
        if need_payload:
            cursor = self._conn.execute(
                "SELECT point_id, vector, path, data FROM chunks"
            )
            try:
                for point_id, vector_blob, path, data_blob in cursor:
                    record = self._decode_data(data_blob)
                    vector = self._decode_vector(vector_blob)
                    yield point_id, vector, path, record.get("payload", {})
            finally:
                cursor.close()
        else:
            cursor = self._conn.execute("SELECT point_id, vector, path FROM chunks")
            try:
                for point_id, vector_blob, path in cursor:
                    vector = self._decode_vector(vector_blob)
                    yield point_id, vector, path, None
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

    def delete_stray_points_fail_closed(self, point_ids: list) -> int:
        """Delete point_ids inside a SINGLE transaction, fail-closed (Story
        #1457 AC7).

        Unlike :meth:`delete`, this method:
          - Sets ``PRAGMA synchronous=FULL`` so the commit is explicitly
            DURABLE (never a deferred/relaxed commit) -- a committed
            stray-row deletion must survive a crash.
          - Explicitly ROLLS BACK the whole transaction on ANY failure
            (whether raised during a DELETE statement or during the commit
            itself) -- no partial deletion is ever left committed.
          - Re-raises the original exception verbatim (never swallows it),
            so callers (temporal reconciliation's fail-closed contract)
            translate it into their own error type.

        Returns:
            Number of points actually deleted.
        """
        self._require_mutable()
        if not point_ids:
            return 0

        self._conn.execute("PRAGMA synchronous=FULL")
        try:
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
        except Exception:
            self._conn.rollback()
            raise


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


# ---------------------------------------------------------------------------
# Read-only, side-effect-free row-existence check (Issue #1459 remediation
# Findings 2/3/4)
# ---------------------------------------------------------------------------

_VALID_ON_ERROR_VALUES = ("treat_absent", "raise")

# Round 2 remediation, Finding B (refined by round 4): substrings of
# sqlite3.OperationalError messages, matched via stable,
# version-independent text -- the stdlib sqlite3 module does not expose a
# clean structured errno for this distinction.
#
# "no such table" is UNAMBIGUOUS: the file positively opened (SQLite
# reached the point of resolving a table name), so it genuinely has no
# "chunks" table yet -- always "no data yet", regardless of on_error.
#
# "unable to open database file" is AMBIGUOUS by itself -- round 4
# remediation found SQLite emits this IDENTICAL message for BOTH a
# genuinely-missing file AND an existing-but-unreadable file (permission
# denied, chmod 000). Message content alone cannot distinguish them, so
# this substring is NOT sufficient on its own to conclude "no data yet" --
# see the explicit Path.exists() check in chunk_store_has_real_data below.
_MISSING_SCHEMA_SUBSTRING = "no such table"
_UNABLE_TO_OPEN_SUBSTRING = "unable to open database file"


def chunk_store_has_real_data(
    db_path: Union[str, Path],
    *,
    on_error: Literal["treat_absent", "raise"] = "treat_absent",
) -> bool:
    """Read-only, side-effect-free row-existence check for a chunks.db file.

    Opens ``db_path`` in SQLite read-only URI mode (``mode=ro``) -- unlike a
    normal ``sqlite3.connect()``, ``mode=ro`` NEVER creates a missing file,
    so a status/health probe never has a write side effect (Issue #1459
    remediation Finding 2). The URI is built via ``Path.resolve().as_uri()``
    (round 2 remediation Finding A) so that path components containing
    URI-special characters (``?``, ``#``, ``%``, spaces, unicode) are
    correctly percent-encoded -- a naive ``f"file:{path}?mode=ro"`` string
    gets misparsed by SQLite's URI parser (a literal ``?``/``#`` in the path
    is read as the start of the query/fragment, truncating the path),
    which both produces a false negative AND, since the truncated "path" is
    then opened in the DEFAULT read-write-create mode instead of the
    intended ``mode=ro``, creates stray files at the misparsed sub-paths --
    a second instance of the exact "must never create files" contract
    violation this function exists to prevent.

    A GENUINELY MISSING file (``not Path(db_path).exists()``), or an
    existing-but-incomplete file with no "chunks" table yet, means
    genuinely no data yet -- returns False silently, always, regardless of
    ``on_error``. Round 4 remediation: SQLite's "unable to open database
    file" message is AMBIGUOUS on its own -- it is emitted identically for
    a genuinely-missing file AND for an existing-but-permission-denied
    file, so this function performs an explicit filesystem existence check
    before treating that message as "no data yet"; message content alone
    is never sufficient (see ``_MISSING_SCHEMA_SUBSTRING`` /
    ``_UNABLE_TO_OPEN_SUBSTRING``, and the ``Path.exists()`` check in the
    implementation, for the exact distinction).

    Any OTHER ``sqlite3.OperationalError`` -- an EXISTING file that could
    not be opened (permission denied, e.g. ``chmod 000``), "database is
    locked" from a real concurrent exclusive lock, or a disk I/O error --
    is NOT a "no data yet" state -- it is a genuine operational problem,
    and is dispatched through the SAME ``on_error`` contract as a
    genuinely CORRUPT file (``sqlite3.DatabaseError``, e.g. "file is not a
    database") (Finding 3 / round 2 remediation Finding B, refined by
    round 4 for the permission-denied case):
      - "treat_absent" (default): log a WARNING and return False. For
        read-only reporting surfaces (golden_repo_manager.py,
        repository_health_aggregator.py) that must degrade gracefully,
        never crash.
      - "raise": re-raise the original exception unchanged. For
        temporal_blank_out.py's destructive path, which must fail loudly
        on uncertain state (including "we don't know, the database is
        locked") rather than silently proceeding toward a hard-delete
        decision (Messi Rule #13).

    Never instantiates a full ``ChunkStore`` (which would trigger
    schema-creation side effects even hypothetically) -- queries
    ``SELECT COUNT(*) FROM chunks`` directly via the read-only connection.

    Raises:
        ValueError: If ``on_error`` is not one of "treat_absent"/"raise" --
            an unrecognized value must fail loudly, never silently fall
            back to either behavior.
    """
    if on_error not in _VALID_ON_ERROR_VALUES:
        raise ValueError(
            f"chunk_store_has_real_data: on_error must be one of "
            f"{_VALID_ON_ERROR_VALUES!r}, got {on_error!r}"
        )

    # resolve() never raises for a missing file (strict=False default) --
    # it is required because Path.as_uri() only accepts absolute paths, and
    # it produces the correct percent-encoding for URI-special characters
    # via the stdlib's own file-URI construction (round 2 Finding A).
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return bool(row is not None and int(row[0]) > 0)
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if _MISSING_SCHEMA_SUBSTRING in message:
            # Unambiguous: the file positively opened, it just has no
            # "chunks" table yet -- always "no data yet".
            return False
        if _UNABLE_TO_OPEN_SUBSTRING in message:
            # Round 4 remediation: this message alone is AMBIGUOUS --
            # SQLite emits it for both a genuinely-missing file AND an
            # existing-but-unreadable file (permission denied). Only a
            # genuinely-absent path is unconditional "no data yet"; an
            # existing-but-unopenable path is a real operational failure
            # and must be dispatched through on_error like any other
            # operational problem, never silently swallowed.
            if not Path(db_path).exists():
                return False
            if on_error == "raise":
                raise
            logger.warning(
                "chunk_store_has_real_data: chunks.db at %s exists but "
                "could not be opened (%s) -- treating as no data present",
                db_path,
                exc,
            )
            return False
        # Any other OperationalError (e.g. "database is locked" from a
        # real concurrent writer) -- NOT "no data yet". Round 2 remediation
        # Finding B: dispatch through the same on_error contract as
        # DatabaseError below, never silently swallow.
        if on_error == "raise":
            raise
        logger.warning(
            "chunk_store_has_real_data: chunks.db at %s raised an "
            "unexpected OperationalError (%s) -- treating as no data "
            "present",
            db_path,
            exc,
        )
        return False
    except sqlite3.DatabaseError as exc:
        # A sibling subclass of OperationalError, NOT re-caught by the
        # branch above -- genuine corruption (e.g. "file is not a
        # database").
        if on_error == "raise":
            raise
        logger.warning(
            "chunk_store_has_real_data: chunks.db at %s appears corrupt "
            "(%s) -- treating as no data present",
            db_path,
            exc,
        )
        return False
    finally:
        if conn is not None:
            conn.close()
