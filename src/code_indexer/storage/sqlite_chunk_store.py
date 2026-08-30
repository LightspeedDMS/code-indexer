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
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple, Union

import numpy as np
import zstandard

from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PayloadChange:
    """Bug #1575 Part C: old-vs-new diff for ONE payload-only update applied
    via :meth:`ChunkStore.update_payload_fields_batch_with_diff`.

    ``update_payload_fields_batch`` returns only a row count -- a visibility
    change (path or ``hidden_branches``) is invisible to any change tracker
    without this diff. ``old_hidden_branches``/``new_hidden_branches`` are
    always tuples (hashable, orderable) even though the stored payload field
    is a JSON list.
    """

    point_id: str
    old_path: Optional[str]
    new_path: Optional[str]
    old_hidden_branches: Tuple[str, ...]
    new_hidden_branches: Tuple[str, ...]


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


class ChunkStoreUnavailableError(ChunkStoreError):
    """Bug #1746 Change 1: a FATAL chunk-store open/write failure -- the
    target ``chunks.db`` could not be opened or written to at all (e.g.
    ``sqlite3.OperationalError``, ``PermissionError``, or a schema-init
    failure), as opposed to an ordinary per-file processing failure
    (corrupt source file, embedding-provider error, etc.).

    This is intentionally a DISTINCT type from the broad ``ChunkStoreError``
    hierarchy's other members: callers up the stack (FileChunkingManager,
    HighThroughputProcessor, SmartIndexer) must be able to distinguish "this
    whole run cannot make progress, abort now" from "this one file failed,
    keep going" without guessing from an exception message. Production
    incident: a root-owned/unwritable chunks.db used to be silently
    converted into a per-file failure and the batch ran to completion,
    burning CPU on every remaining file for hours before anyone noticed
    (GitHub issue #1746).
    """


_LOCK_CONTENTION_SUBSTRINGS = (
    "database is locked",
    "database table is locked",
)


def is_fatal_chunk_store_write_error(exc: BaseException) -> bool:
    """Bug #1746 code review findings H1+H2: classify whether ``exc``
    (raised from an attempted chunk-store open/write) represents a FATAL
    condition -- one that will never succeed on retry (unwritable/
    root-owned file, corrupt database, disk full, read-only filesystem)
    -- versus a TRANSIENT one (lock contention under concurrent writers,
    which Python's default 5s sqlite busy-timeout can still exceed under
    real concurrent load). The CHUNKS_DB write path opens a fresh
    ``ChunkStore`` connection per ``upsert_points()`` call with no
    application-level write lock across worker threads, so lock
    contention under concurrent writes is EXPECTED, not exceptional --
    treating it as fatal would abort an entire indexing run on what
    should only fail the one file.

    Fatal (returns True):
      - ``sqlite3.DatabaseError`` -- note ``sqlite3.OperationalError`` IS-A
        (is a subclass of) ``DatabaseError``, so this also catches every
        OperationalError case (e.g. "unable to open database file"). It
        additionally catches a corrupt chunks.db, which sqlite raises as
        a plain ``DatabaseError`` directly (e.g. "file is not a
        database"), NOT via OperationalError -- EXCEPT when the message
        indicates transient lock contention
        (``_LOCK_CONTENTION_SUBSTRINGS``: "database is locked" /
        "database table is locked"), which is NOT fatal.
      - ``OSError`` -- note ``PermissionError`` IS-A ``OSError``, so this
        also catches every PermissionError case. It additionally catches
        disk-full (ENOSPC), which the OS raises as a plain ``OSError``,
        not ``PermissionError``.

    Not fatal (returns False): everything else, including the two lock
    messages above.
    """
    if isinstance(exc, sqlite3.DatabaseError):
        message = str(exc).lower()
        if any(substring in message for substring in _LOCK_CONTENTION_SUBSTRINGS):
            return False
        return True
    if isinstance(exc, OSError):
        return True
    return False


class CorruptChunkDataError(ChunkStoreError):
    """Raised when a stored chunk's opaque ``data`` blob or ``vector`` blob
    cannot be decoded -- a data-integrity failure (Codex-15 finding).

    Names the offending ``point_id`` and the failing field so callers
    (``read``/scroll/get_point) can surface it as a data-integrity error
    (e.g. translate to ``ScrollDataIntegrityError``) rather than let a raw
    ``zstandard.ZstdError`` / ``json.JSONDecodeError`` / numpy ``ValueError``
    escape. Fail loud (Messi Rule #13) -- never swallowed or silently
    skipped. A genuinely-missing row (``read`` -> ``None``) is a SEPARATE
    not-found contract, never this error.
    """


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

# Bug #1575 Part A: chunked "WHERE ... IN (...)" batch size, kept well
# under SQLite's ~999-host-parameter limit (mirrors _DELETE_CHUNK_SIZE's
# existing rationale, reused here for a query rather than a delete).
_QUERY_IN_CHUNK_SIZE = 500

# Bug #1575 Part A (AC5): the canonical "content point" type value, matching
# `_prepare_vector_data_batch`'s `metadata.type` default (also see
# `GitAwareMetadataSchema.create_git_aware_metadata`, which stamps every
# semantic/multimodal record with `"type": "content"`).
_CONTENT_TYPE = "content"


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
        durable_synchronous: bool = False,
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
            durable_synchronous: Bug #1486 High Finding 3 -- when True,
                configures (and verifies) ``PRAGMA synchronous=FULL`` on
                this connection IMMEDIATELY at open time, before
                ``_ensure_schema()`` or any write transaction begins.
                SQLite raises ``sqlite3.OperationalError: Safety level
                may not be changed inside a transaction`` if this pragma
                is set mid-transaction, and setting it after prior
                commits does not retroactively apply to those commits --
                so it MUST be configured here, at open time, never
                inside :meth:`flush_durable`. Reserved EXCLUSIVELY for
                migration write connections -- :meth:`flush_durable`
                refuses to run unless this was set. The general
                per-chunk indexing ``ChunkStore`` path (this parameter
                left at its default False) is completely unaffected --
                a per-write NFS fsync would cripple indexing throughput.
        """
        self.db_path = Path(db_path)
        self._immutable = immutable
        self._durable_synchronous = durable_synchronous
        self._compressor = zstandard.ZstdCompressor()
        self._decompressor = zstandard.ZstdDecompressor()
        self._conn = self._open_connection()
        self._expected_dim = expected_dim
        if not immutable:
            try:
                if durable_synchronous:
                    self._configure_durable_synchronous()
                self._ensure_schema()
                if self._expected_dim is None:
                    self._expected_dim = self._load_persisted_dim()
            except BaseException:
                # Codex finding: a post-open init failure must not leak the
                # already-opened connection (fd/handle leak; resource
                # exhaustion on repeated failures). BaseException (not just
                # Exception) so KeyboardInterrupt/SystemExit during init
                # also close the connection before propagating.
                try:
                    self._conn.close()
                finally:
                    raise

    def _configure_durable_synchronous(self) -> None:
        """Bug #1486 High Finding 3: configure PRAGMA synchronous=FULL on
        this connection BEFORE any write transaction begins (never
        inside :meth:`flush_durable`, where a genuinely pending
        transaction would make this raise). Configures AND VERIFIES --
        never trusts blindly -- reading the pragma back and raising
        loudly if it did not actually take effect.
        """
        self._conn.execute("PRAGMA synchronous=FULL")
        row = self._conn.execute("PRAGMA synchronous").fetchone()
        if row is None or int(row[0]) != 2:  # 2 == FULL
            raise ChunkStoreError(
                f"ChunkStore: failed to configure PRAGMA synchronous=FULL "
                f"on the durable migration connection for {self.db_path} "
                f"(read back {row!r}) -- refusing to proceed with a "
                f"connection whose durability level could not be verified"
            )

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
            # Codex-15 finding: the post-connect() PRAGMA runs BEFORE
            # __init__'s post-open guard is even reached (that guard wraps
            # _ensure_schema/_configure_durable_synchronous/_load_persisted_dim
            # only). If this PRAGMA raises (e.g. "database is locked" when a
            # WAL file cannot be switched out of WAL mode), the
            # already-opened connection would leak. sqlite3.connect() itself
            # stays OUTSIDE this guard -- nothing is open to close if it
            # fails. BaseException so KeyboardInterrupt/SystemExit mid-PRAGMA
            # also close the connection before propagating.
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except BaseException:
                conn.close()
                raise
        return conn

    def _ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._ensure_type_column()
        self._conn.commit()

    def _ensure_type_column(self) -> None:
        """Bug #1575 Part A (AC5): backward-compatible migration adding an
        indexed ``type`` column to ``chunks``.

        The original schema had no ``type`` column -- a record's ``type``
        (the SAME value ``_prepare_vector_data_batch`` stores under
        ``metadata.type``, defaulting to ``"content"``) lived only inside
        the compressed, opaque ``data`` blob, unqueryable without decoding
        every row. ``ALTER TABLE ADD COLUMN`` is this project's established
        backward-compatible migration primitive (never DROP/RENAME/type
        change) -- idempotent via ``PRAGMA table_info`` so a database that
        already has the column (every open after the first, on any given
        collection) never re-runs ``ALTER``/backfill. The index creation
        itself stays unconditional (``CREATE INDEX IF NOT EXISTS``,
        idempotent on its own) so a database that already has the column
        but was somehow left without the index still gets it.

        Investigation (Bug #1575 issue text, "Part A" scope): every current
        production writer that ever sets ``payload.path`` also sets
        ``type == "content"`` (semantic AND multimodal content records,
        both built via ``GitAwareMetadataSchema.create_git_aware_metadata``);
        the ONE writer that uses a different ``type`` value
        (``"commit_chunk"``, temporal per-commit chunks built by
        ``temporal_point_builder.build_chunk_payload``) never sets
        ``payload.path`` at all (it uses ``paths``/``primary_path``
        instead) -- proven directly against that real writer code in
        ``tests/unit/storage/test_chunk_storage_1575_part_a_temporal_invariant.py``.
        A column is still added (rather than trusting that whole-codebase
        invariant indefinitely) because nothing enforces it against a
        FUTURE writer -- an indexed, authoritative ``type`` column is the
        durable, provable mechanism AC5 calls for.

        Two connections can race to migrate the same fresh database, and
        the loser's ``ALTER`` then raises ``duplicate column name``. This
        is recovered below as benign, deliberately skipping the loser's
        own backfill since the winner's backfill already covers it.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(chunks)")}
        column_added = "type" not in cols
        if column_added:
            try:
                self._conn.execute("ALTER TABLE chunks ADD COLUMN type TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
                column_added = False

        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(type)")

        if column_added:
            self._backfill_type_column()

    def _backfill_type_column(self) -> None:
        """One-time backfill of the newly-added ``type`` column from each
        existing row's decoded payload/metadata -- runs only immediately
        after :meth:`_ensure_type_column` adds the column to a
        pre-existing database (a fresh/empty table backfills nothing).

        Codex review follow-up (Bug #1575 Part A, finding 5): streams rows
        via a cursor instead of ``fetchall()`` so a large pre-migration
        collection's compressed payloads are not all held in memory at
        once during backfill.
        """
        cursor = self._conn.execute("SELECT point_id, data FROM chunks")
        try:
            for point_id, data_blob in cursor:
                record = self._decode_data(data_blob)
                record_type = self._record_type(record)
                self._conn.execute(
                    "UPDATE chunks SET type = ? WHERE point_id = ?",
                    (record_type, point_id),
                )
        finally:
            cursor.close()

    def close(self) -> None:
        self._conn.close()

    def flush_durable(self) -> None:
        """Bug #1486 (CRITICAL, data loss): force this store's pending
        writes to be DURABLE on the actual backing store, not merely
        reflected in a client-side page cache.

        Confirmed production root cause: fleet migration's read-back
        verification read chunks.db through the SAME NFS client that had
        just written it -- a fresh read through that client's page cache
        can report "correct" even though the write has not yet reached
        the NFS SERVER durably. Commits any pending transaction, then
        explicitly ``nfs_safe_fsync``'s the on-disk file AND its
        containing directory -- belt-and-suspenders against SQLite's own
        internal fsync not being sufficient to guarantee NFS-server-side
        durability under close-to-open cache semantics.

        Bug #1486 High Finding 3: this method deliberately does NOT set
        ``PRAGMA synchronous=FULL`` itself -- SQLite raises
        ``sqlite3.OperationalError: Safety level may not be changed
        inside a transaction`` if that pragma is changed mid-transaction
        (exactly the state this method is meant to flush), and setting
        it after prior commits would not retroactively apply to those
        already-committed writes anyway. The pragma must already have
        been configured at connection-OPEN time via
        ``durable_synchronous=True`` -- this method refuses to run
        otherwise (never silently proceeds on a connection whose
        durability level was never verified).

        Callers must invoke this AFTER the final write of a batch/pass
        and BEFORE any fresh-connection integrity re-verification whose
        result must be trusted to reflect genuinely durable state.
        """
        self._require_mutable()
        if not self._durable_synchronous:
            raise ChunkStoreError(
                f"ChunkStore.flush_durable() refused for {self.db_path}: "
                f"this store was not opened with durable_synchronous=True "
                f"-- flush_durable() is reserved for migration write "
                f"connections, which must configure+verify PRAGMA "
                f"synchronous=FULL at open time (never here). The general "
                f"per-chunk indexing path must never call this method."
            )
        self._conn.commit()

        fd = os.open(str(self.db_path), os.O_RDONLY)
        try:
            nfs_safe_fsync(fd)
        finally:
            os.close(fd)

        dir_fd = os.open(str(self.db_path.parent), os.O_RDONLY)
        try:
            nfs_safe_fsync(dir_fd)
        finally:
            os.close(dir_fd)

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

    @staticmethod
    def _record_type(record: Dict[str, Any]) -> str:
        """Return the canonical ``type`` value for a full record dict, using
        the SAME resolution order as ``_prepare_vector_data_batch``'s
        writer default: ``metadata.type`` first, else ``payload.type``,
        else the module-level ``_CONTENT_TYPE`` constant ("content").
        Defensively tolerates a missing/``None`` ``metadata``/``payload``
        key, and a falsy (``None``/empty-string) stored ``type`` value.
        """
        metadata = record.get("metadata") or {}
        payload = record.get("payload") or {}
        record_type = metadata.get("type") or payload.get("type") or _CONTENT_TYPE
        return str(record_type)

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
        key is preserved verbatim (passthrough by construction). Bug #1575
        Part A: the record's ``type`` (mirroring the writer's own
        ``metadata.type``/``payload.type`` convention, default
        ``"content"``) is ALSO persisted into the indexed ``type`` column so
        ``distinct_content_paths()`` can filter without decoding every row.
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
            record_type = self._record_type(record)
            rows.append((point_id, path, record_type, vector_blob, data_blob))

        self._conn.executemany(
            "INSERT OR REPLACE INTO chunks (point_id, path, type, vector, data) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _row_to_record(
        self, point_id: str, vector_blob: bytes, data_blob: bytes
    ) -> Dict[str, Any]:
        # Codex-15 finding: a corrupt on-disk blob must surface as a
        # contextual data-integrity error naming the point_id + the failing
        # field, never a raw zstd/json/numpy exception with no context.
        # zstandard.ZstdError -> corrupt compressed frame; json.JSONDecodeError
        # (a ValueError subclass) -> valid zstd but non-JSON payload;
        # UnicodeDecodeError -> non-UTF-8 payload; ValueError/TypeError ->
        # numpy.frombuffer on a mis-sized vector blob.
        try:
            record = self._decode_data(data_blob)
        except (
            zstandard.ZstdError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise CorruptChunkDataError(
                f"Chunk store at {self.db_path}: point {point_id!r} has a "
                f"corrupt 'data' blob that could not be decoded "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        record["id"] = point_id
        try:
            record["vector"] = self._decode_vector(vector_blob)
        except (ValueError, TypeError) as exc:
            raise CorruptChunkDataError(
                f"Chunk store at {self.db_path}: point {point_id!r} has a "
                f"corrupt 'vector' blob that could not be decoded "
                f"({type(exc).__name__}: {exc})"
            ) from exc
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

    def point_ids_after(self, cursor: Optional[str], limit: int) -> List[str]:
        """Bug #1575 Part B: keyset-pagination primitive replacing
        ``sorted(all_point_ids())`` on the scroll-pagination hot path.

        Returns up to ``limit`` point_ids strictly greater than ``cursor``,
        in ascending (Python-``sorted``-equivalent, since TEXT columns use
        SQLite's default BINARY collation) order -- via
        ``WHERE point_id > ? ORDER BY point_id LIMIT ?`` (or the
        unconditional ``ORDER BY point_id LIMIT ?`` form when ``cursor`` is
        None). Both forms use the ``point_id`` PRIMARY KEY's own implicit
        index, but produce two DIFFERENT (and both cheap) query plans --
        verified via ``EXPLAIN QUERY PLAN`` (Bug #1575 Part B Codex review,
        Finding 3, correcting an earlier overstated claim that both forms
        are identical):

        - ``cursor`` given: ``SEARCH TABLE chunks USING COVERING INDEX
          sqlite_autoindex_chunks_1 (point_id>?)`` -- an index SEARCH driven
          by the ``WHERE`` clause.
        - ``cursor`` is ``None``: ``SCAN TABLE chunks USING COVERING INDEX
          sqlite_autoindex_chunks_1`` -- there is no ``WHERE`` clause to
          search on, so SQLite reports a SCAN, not a SEARCH.

        Both are index-ONLY (``USING COVERING INDEX``) and never read the
        ``vector``/``data`` blob columns -- neither form is a full ROW scan.
        Do not claim they produce the same plan shape; only claim they are
        both cheap and both avoid a full-row scan. The cost of retrieving
        ONE page never depends on the total row count either way --
        unlike ``sorted(chunk_store.all_point_ids())``, which pulls every
        stored point_id into Python and re-sorts all of them on every call.

        Called by ``FilesystemVectorStore._scroll_points_chunks_db()`` in
        bounded batches to page through a CHUNKS_DB collection -- see that
        method for the wiring (Bug #1575 Part B).

        A cursor whose id has since been deleted resolves correctly: SQLite
        simply returns the next id strictly greater than the (now absent)
        cursor value -- no crash, no duplicate, no gap, matching the exact
        semantics ``bisect_right`` provided over the in-memory sorted list.

        Raises:
            ValueError: if ``limit`` is not a positive integer -- mirrors
                ``FilesystemVectorStore.scroll_points``'s own ``limit <= 0``
                guard rather than silently executing a malformed/no-op
                SQL LIMIT clause.
        """
        if limit <= 0:
            raise ValueError(
                f"point_ids_after limit must be a positive integer, got {limit!r}"
            )
        if cursor is None:
            rows = self._conn.execute(
                "SELECT point_id FROM chunks ORDER BY point_id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT point_id FROM chunks WHERE point_id > ? "
                "ORDER BY point_id LIMIT ?",
                (cursor, limit),
            ).fetchall()
        return [row[0] for row in rows]

    def _chunks_table_exists(self) -> bool:
        """2nd Codex review follow-up (Bug #1575 Part A, Gap 1): distinguish
        "the ``chunks`` table does not exist at all" from "the table
        exists but the ``type`` column is absent" -- ``PRAGMA
        table_info(chunks)`` (see :meth:`_has_type_column`) returns an
        EMPTY result set for BOTH cases, so it cannot tell them apart on
        its own.

        A genuinely virgin immutable snapshot (a ``chunks.db`` file that
        exists but was never populated -- immutable open skips
        ``_ensure_schema()`` entirely, see ``__init__``) has no ``chunks``
        table. That is a legitimately EMPTY collection, not an error, and
        callers must not let a query against a nonexistent table raise
        ``sqlite3.OperationalError: no such table: chunks``.

        Queries ``sqlite_master`` directly -- a pure, read-only metadata
        read, safe on an immutable connection (never ``CREATE TABLE``/
        ``ALTER TABLE`` here).
        """
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone()
        return row is not None

    def _has_type_column(self) -> bool:
        """Codex review follow-up (Bug #1575 Part A, CRITICAL finding 1):
        an IMMUTABLE open skips ``_ensure_schema()`` entirely (see
        ``__init__``'s ``if not immutable:`` guard) -- so a pre-migration
        chunks.db (created before the ``type`` column migration landed)
        opened with ``immutable=1`` has no ``type`` column. ``PRAGMA
        table_info`` is a pure metadata read, safe on an immutable
        connection -- never attempt ``ALTER TABLE`` here.
        """
        cursor = self._conn.execute("PRAGMA table_info(chunks)")
        try:
            cols = {row[1] for row in cursor.fetchall()}
        finally:
            cursor.close()
        return "type" in cols

    def distinct_content_paths(self) -> "set[str]":
        """Bug #1575 Part A (AC5): return the set of distinct non-null
        ``path`` values belonging ONLY to ``type == "content"`` rows.

        Replaces materializing every content payload
        (``_fetch_all_content_points``) just to derive the set of distinct
        file paths on record -- this reads the indexed ``path``/``type``
        columns only, never decoding ``data``, and retains ONLY the path
        strings (never a list of payloads).

        Codex review follow-up (CRITICAL finding 1): a pre-migration
        immutable-mode store (no ``type`` column, see :meth:`_has_type_column`)
        cannot run the indexed query -- it falls back to a decode-based
        content-type check via :meth:`_record_type`, streamed via a cursor
        (never ``fetchall()``) so a large legacy collection is not fully
        materialized in memory at once. This fallback NEVER attempts
        ``ALTER TABLE`` against what may be an immutable connection.

        2nd Codex review follow-up (Gap 1): a genuinely virgin immutable
        snapshot has NO ``chunks`` table at all (see
        :meth:`_chunks_table_exists`) -- that is a legitimately empty
        collection and returns an empty set, never
        ``sqlite3.OperationalError: no such table: chunks``.
        """
        if not self._chunks_table_exists():
            return set()

        if not self._has_type_column():
            result: "set[str]" = set()
            cursor = self._conn.execute(
                "SELECT path, data FROM chunks WHERE path IS NOT NULL"
            )
            try:
                for path, data_blob in cursor:
                    record = self._decode_data(data_blob)
                    if self._record_type(record) == _CONTENT_TYPE:
                        result.add(path)
            finally:
                cursor.close()
            return result

        cursor = self._conn.execute(
            "SELECT DISTINCT path FROM chunks WHERE path IS NOT NULL AND type = ?",
            (_CONTENT_TYPE,),
        )
        try:
            rows = cursor.fetchall()
        finally:
            cursor.close()
        return {row[0] for row in rows}

    def fetch_points_for_paths(
        self, paths: Iterable[str], *, payload_only: bool = False
    ) -> List[Dict[str, Any]]:
        """Bug #1575 Part A: batched, targeted fetch of full records for the
        given stored ``paths`` -- a ``WHERE path IN (...)`` lookup via the
        indexed ``path`` column, chunked to respect SQLite's ~999
        host-parameter limit (Messi Rule #14: bounded loops only). NEVER a
        full-table scan.

        Args:
            paths: Stored path values (RAW, exact form -- callers are
                responsible for any absolute/relative normalization before
                calling this method; the underlying column is an exact
                string match).
            payload_only: Codex review follow-up (Bug #1575 Part A, finding
                6) -- when True, the SQL query selects ``point_id, data``
                ONLY (never ``vector``) and each row is decoded via
                ``_row_to_payload_only_record`` -- the ``vector`` blob is
                neither selected nor decoded, and returned records have no
                ``vector`` key at all. For the ``FilesystemVectorStore``
                caller, which immediately discards the vector and only
                needs ``id``/``payload``. Default False preserves today's
                byte-identical full-record behavior for every other caller.

        Returns:
            Full decoded records (``id``, ``vector``, ``payload``, and
            every other passthrough field) for every row whose ``path``
            matches one of ``paths`` when ``payload_only=False``; the same
            records minus ``vector`` when ``payload_only=True``. Empty
            list for an empty/no-match input.

        Raises:
            ValueError: If ``paths`` is ``None`` -- an empty iterable
                (``set()``/``[]``) is the correct way to request "nothing",
                never ``None``.
        """
        if paths is None:
            raise ValueError(
                "fetch_points_for_paths: paths must not be None -- pass an "
                "empty set()/[] to request zero results"
            )

        # 2nd Codex review follow-up (Bug #1575 Part A, Gap 1): a genuinely
        # virgin immutable snapshot has no `chunks` table at all -- that is
        # a legitimately empty collection (no matches possible), never
        # `sqlite3.OperationalError: no such table: chunks`.
        if not self._chunks_table_exists():
            return []

        paths_list = list(paths)
        if not paths_list:
            return []

        results: List[Dict[str, Any]] = []
        for start in range(0, len(paths_list), _QUERY_IN_CHUNK_SIZE):
            batch = paths_list[start : start + _QUERY_IN_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in batch)
            if payload_only:
                rows = self._conn.execute(
                    f"SELECT point_id, data FROM chunks WHERE path IN ({placeholders})",
                    batch,
                ).fetchall()
                for point_id, data_blob in rows:
                    results.append(
                        self._row_to_payload_only_record(point_id, data_blob)
                    )
            else:
                rows = self._conn.execute(
                    f"SELECT point_id, vector, data FROM chunks WHERE path IN ({placeholders})",
                    batch,
                ).fetchall()
                for point_id, vector_blob, data_blob in rows:
                    results.append(
                        self._row_to_record(point_id, vector_blob, data_blob)
                    )
        return results

    def _row_to_payload_only_record(
        self, point_id: str, data_blob: bytes
    ) -> Dict[str, Any]:
        """Codex review follow-up (Bug #1575 Part A, finding 6): decode
        ONLY the ``data`` blob -- never touches/decodes the ``vector``
        blob at all. Used by :meth:`fetch_points_for_paths`'s
        ``payload_only=True`` path for callers (``FilesystemVectorStore``)
        that immediately discard the vector.
        """
        try:
            record = self._decode_data(data_blob)
        except (
            zstandard.ZstdError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise CorruptChunkDataError(
                f"Chunk store at {self.db_path}: point {point_id!r} has a "
                f"corrupt 'data' blob that could not be decoded "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        record["id"] = point_id
        return record

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

    def update_payload_fields_batch_with_diff(
        self, updates: List[Tuple[str, dict]]
    ) -> List[PayloadChange]:
        """Bug #1575 Part C: same batch payload-merge as
        :meth:`update_payload_fields_batch` (ONE transaction, ONE commit,
        points not found skipped gracefully) but additionally returns the
        old-vs-new diff for every row actually updated, so a caller can
        register precisely which point_ids had a VISIBILITY-relevant change
        (path or ``hidden_branches``) without a second read pass.

        A no-op merge (new value equals the old one) still produces a
        ``PayloadChange`` entry with ``old_* == new_*`` -- it is the
        CALLER's responsibility to decide whether that counts as a real
        change (e.g. only registering ``visibility_changed`` when they
        differ), mirroring how the SHARDED_JSON path's own comparison
        works.
        """
        self._require_mutable()
        if not updates:
            return []

        changes: List[PayloadChange] = []
        for point_id, fields in updates:
            row = self._conn.execute(
                "SELECT data FROM chunks WHERE point_id = ?", (point_id,)
            ).fetchone()
            if row is None:
                continue

            record = self._decode_data(row[0])
            existing_payload = record.get("payload", {})
            old_path = existing_payload.get("path")
            old_hidden_branches = tuple(existing_payload.get("hidden_branches", []))

            for key, value in fields.items():
                existing_payload[key] = value
            record["payload"] = existing_payload

            new_path = existing_payload.get("path")
            new_hidden_branches = tuple(existing_payload.get("hidden_branches", []))

            new_data_blob = self._compressor.compress(
                json.dumps(record).encode("utf-8")
            )
            self._conn.execute(
                "UPDATE chunks SET path = ?, data = ? WHERE point_id = ?",
                (new_path, new_data_blob, point_id),
            )
            changes.append(
                PayloadChange(
                    point_id=point_id,
                    old_path=old_path,
                    new_path=new_path,
                    old_hidden_branches=old_hidden_branches,
                    new_hidden_branches=new_hidden_branches,
                )
            )

        self._conn.commit()
        return changes

    # ------------------------------------------------------------------
    # Delete (AC4: mirrors FilesystemVectorStore.delete_points)
    # ------------------------------------------------------------------

    _DELETE_CHUNK_SIZE = 500  # stay well under SQLite's ~999 variable limit

    def get_paths_for_points(self, point_ids: list) -> Dict[str, str]:
        """Return a ``{point_id: path}`` mapping for the given point_ids.

        Bug #1575 Finding-1-regression fix: a point's stored ``path`` is
        unrecoverable once the row is deleted, so callers that need to keep
        an in-memory ``PathIndex`` in sync on delete (mirroring what the
        SHARDED_JSON delete path already does) MUST resolve paths BEFORE
        calling :meth:`delete`, never after. Points with no path (NULL) or
        that do not exist are simply absent from the returned mapping --
        never an error, matching :meth:`delete`'s own silent-skip semantics
        for non-existent ids. Read-only: safe on an immutable connection.
        Chunked to respect SQLite's bound on the number of host parameters
        per statement (Messi Rule #14: bounded loops only), reusing the
        same chunk size as :meth:`delete`.
        """
        if not point_ids:
            return {}

        result: Dict[str, str] = {}
        for start in range(0, len(point_ids), self._DELETE_CHUNK_SIZE):
            chunk = point_ids[start : start + self._DELETE_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT point_id, path FROM chunks "
                f"WHERE point_id IN ({placeholders}) AND path IS NOT NULL",
                chunk,
            ).fetchall()
            for point_id, path in rows:
                result[point_id] = path
        return result

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
    db_path: Union[str, Path], collection_path: str, *, read_only: bool = False
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
    immutable = read_only or predicate(collection_path)
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
