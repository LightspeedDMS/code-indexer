"""Filesystem-based vector storage with git-aware optimization.

Stores vectors in filesystem with path-as-vector quantization and git-aware chunk storage.
Following Story 2 requirements.
"""

import fcntl
import hashlib
import json
import os
import random
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union, Set, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    # Imported only for type-checking so the runtime CLI startup import budget
    # is unaffected (concurrent.futures stays a lazy import inside search()).
    from concurrent.futures import Executor
import threading
import numpy as np
import logging
import msgpack

from .vector_quantizer import VectorQuantizer
from .projection_matrix_manager import ProjectionMatrixManager
from .temporal_metadata_store import TemporalMetadataStore
from .hnsw_stale_logger import log_hnsw_stale
from code_indexer.utils.file_locking import fsync_directory, nfs_safe_fsync
from code_indexer.storage.shared.hnsw_sync_state import (
    HNSW_SYNC_SCHEMA_VERSION,
    HNSW_SYNC_STATE_FILENAME,
    HNSWSyncSession,
    HNSWSyncState,
    compute_dirty_transition,
    read_hnsw_sync_state,
    write_hnsw_sync_state,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout


class LocalIndexNotFoundError(RuntimeError):
    """Raised when a local HNSW index file is missing for a collection.

    This is a storage-layer error: the embedding provider completed successfully
    but the on-disk HNSW index does not exist.  Callers that discriminate between
    provider failures and local-storage failures (e.g. the parallel-query health
    monitor) must catch this exception type separately from generic provider errors
    so that a missing local index does not sin-bin the embedding provider.

    Remediation: run ``cidx index --rebuild-index`` in the affected repository.
    """


class ScrollDataIntegrityError(RuntimeError):
    """Raised by ``scroll_points()``'s legacy SHARDED_JSON id-map scan when a
    vector file that is genuinely PRESENT is unreadable as a record -- invalid
    JSON, a missing/invalid ``id`` field, or a duplicate stored ``id`` shared
    with another file.

    Fail loud (Messi #13): a read/pagination path must NEVER silently drop or
    collapse a record. Silently skipping a malformed file returned a short page
    with a terminal ``None`` cursor, falsely presenting a COMPLETE traversal
    (silent data loss); silently overwriting a duplicate id let one arbitrary
    file win. Both are surfaced here, naming the offending file(s).

    Deliberately distinct from a mid-scan ``FileNotFoundError`` (a concurrent
    server-mode fleet migration flipping the discriminator and deleting the
    legacy files): a VANISHED file is the Bug #1486 Finding-5 race and is
    absorbed + re-dispatched to the chunk store, never raised as an integrity
    error. Only a file that is PRESENT-but-malformed is an integrity fault.
    """


# Bug #1488 (Codex Finding B): self-describing scroll-cursor marker. Every
# ``scroll_points()`` next-cursor we emit is ``_SCROLL_CURSOR_PREFIX + <real
# point-id>`` (the SAME real point-id in BOTH the SHARDED_JSON and CHUNKS_DB
# layouts), so a cursor issued under one layout resumes correctly after a
# concurrent flip to the other. The prefix makes a cursor VALIDATABLE: a
# received cursor bearing it is a legitimate id-cursor we minted (honored even
# if the point was since deleted -> resume at next-greater), a legacy
# ``vector_<token>.json`` path cursor is translated, and anything else is
# garbage and fails LOUD (Messi #13) rather than silently restarting at
# offset 0. The token is deliberately not a real directory/point-id shape so a
# genuine point-id can never be mistaken for a cursor and vice-versa.
_SCROLL_CURSOR_PREFIX = "__cidx_scroll_v1__:"

#: Bug #1575 Part C: how often (in processed-point counts) the
#: visibility-aware incremental update reports progress.
_INCREMENTAL_PROGRESS_INTERVAL = 10

#: Bug #1575 Part C: named result-dict "action" values for
#: _resolve_and_publish_hnsw_sync(), replacing hard-coded literal strings.
_ACTION_REUSED = "reused"
_ACTION_INCREMENTAL = "incremental"
_ACTION_FULL_REBUILD = "full_rebuild"


def hnsw_cache_key_for_collection_path(
    collection_path: Union[str, Path], *, activation_id: Optional[str] = None
) -> str:
    """Module-level equivalent of ``FilesystemVectorStore
    .hnsw_cache_key_for_collection()`` -- the single authority for composing
    the shared HNSW/id_index cache key, usable by callers that only have a
    bare ``collection_path`` (and, for an activated-repo collection, its
    ``activation_id``) with no live store instance to hand.

    Bug #1542: the HNSW fleet orphan-repair sweep (``repair_executor.py``)
    invalidates the server-side cache after a successful repair but has no
    ``FilesystemVectorStore`` instance for the collection it just repaired --
    only a resolved ``collection_path``. Hand-building a bare-path key there
    (as it previously did) composes a DIFFERENT key than ``search()`` stores
    under (Story #1458 AC11's chunk-layout token, and this token), so the
    invalidation silently matches nothing -- the same defect class Bug #1538
    fixed for the temporal dispatch's post-shard eviction. Every such
    external caller MUST go through this function (or the instance method
    below, which delegates to it) rather than reconstructing the format.

    The layout token is resolved FRESH from disk here -- see
    ``hnsw_cache_key_for_collection()``'s docstring for why.
    """
    from code_indexer.storage.shared.chunk_layout import resolve_chunk_layout

    key = str(Path(collection_path).resolve())
    key = f"{key}:{resolve_chunk_layout(collection_path).value}"
    if activation_id is not None:
        key = f"{key}:{activation_id}"
    return key


# Story #1110 (S6 Chunk B): module-level lazy references so tests can patch them
# at `code_indexer.storage.filesystem_vector_store.*`.  Both are server-only; the
# CLI path never enters the `if parallel_executor is not None` branch with these
# imported, so ImportError is only expected in stripped unit-test environments.
try:
    from code_indexer.server.services.governed_call import (
        coalesced_query_embedding,
    )
except ImportError:  # pragma: no cover
    coalesced_query_embedding = None  # type: ignore[assignment]

try:
    from code_indexer.server.services.embedding_cache_audit import (
        _run_deep_fidelity_audit,
    )
except ImportError:  # pragma: no cover
    _run_deep_fidelity_audit = None  # type: ignore[assignment]

# Story #1293 (Epic #1288) S1b [A8]: the shared emit helper. Lazy-import-safe
# (same pattern as coalesced_query_embedding above) so the CLI startup import
# budget is unaffected -- emit_embed_event() itself no-ops when no writer is
# installed (CLI / solo / pre-lifespan), so calling it unconditionally here is
# safe on every path.
try:
    from code_indexer.server.services.search_embed_event_emit import (
        emit_embed_error_event,
        emit_embed_event,
    )
except ImportError:  # pragma: no cover
    emit_embed_event = None  # type: ignore[assignment]
    emit_embed_error_event = None  # type: ignore[assignment]


def _write_embed_meta_to_event_ctx(embed_meta: "Any", provider_name: str = "") -> None:
    """Story #1159: write embedding-cache metadata to the active SearchEventContext.

    Must be called from the MAIN REQUEST THREAD (not from a ThreadPoolExecutor
    worker) because Python 3.9 does not propagate ContextVar state into threads.
    Logs a warning on failure so query results are never blocked by telemetry.

    Args:
        embed_meta: EmbeddingCacheMetadata returned by coalesced_query_embedding.
        provider_name: Provider name string (e.g. "voyage-ai" or "cohere").
            Used to select the correct ctx field set (cohere_* vs voyage_*).
    """
    try:
        from code_indexer.server.services.search_event_context import _search_event_ctx

        event_ctx = _search_event_ctx.get(None)
        if event_ctx is not None:
            if "cohere" in provider_name.lower():
                event_ctx.cohere_cache_hit = embed_meta.key_found
                event_ctx.cohere_cache_mode = embed_meta.cache_mode
                event_ctx.cohere_latency_ms = embed_meta.provider_latency_ms
            else:
                event_ctx.voyage_cache_hit = embed_meta.key_found
                event_ctx.voyage_cache_mode = embed_meta.cache_mode
                event_ctx.voyage_latency_ms = embed_meta.provider_latency_ms
    except Exception as _exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "search_event_log: failed to write embed_meta to ctx: %s", _exc
        )

    # Story #1293 (Epic #1288) S1b [A8]: emit the durable search_embed_event
    # row for this FSV worker-thread embed, driven entirely by the meta
    # returned from the worker. This call happens on the MAIN (calling)
    # thread -- the same thread whose correlation_id ContextVar is correct
    # (the worker thread's own context is never used for emission). No-op
    # when role/outcome aren't yet classified or no writer is installed.
    if emit_embed_event is not None:
        try:
            emit_embed_event(embed_meta)
        except Exception as _emit_exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning(
                "search_embed_event: emit_embed_event failed: %s", _emit_exc
            )


# Minimum and default timeout (seconds) for git subprocess calls.
# GIT_TIMEOUT_SECONDS env var overrides the default; values below the minimum are clamped.
_DEFAULT_GIT_TIMEOUT_SECONDS = 5
MIN_GIT_TIMEOUT_SECONDS = 1


def _parse_git_timeout() -> int:
    """Parse GIT_TIMEOUT_SECONDS env var with safe fallback.

    Defaults to 5 when the env var is unset or contains a non-integer value.
    Clamps to a minimum of MIN_GIT_TIMEOUT_SECONDS (1) when the parsed value is below it.
    """
    try:
        value = int(os.getenv("GIT_TIMEOUT_SECONDS", str(_DEFAULT_GIT_TIMEOUT_SECONDS)))
        return max(value, MIN_GIT_TIMEOUT_SECONDS)
    except ValueError:
        return _DEFAULT_GIT_TIMEOUT_SECONDS


# Timeout for git subprocess calls. Configurable via GIT_TIMEOUT_SECONDS env var.
GIT_TIMEOUT_SECONDS = _parse_git_timeout()


def _parse_use_chunks_db_for_new_collections_env() -> bool:
    """Story #1456: opt-in gate for fresh semantic collections to be built
    using the consolidated chunks.db layout instead of sharded
    vector_*.json files. Defaults to False (legacy SHARDED_JSON layout)
    everywhere unless CIDX_CHUNKS_DB_NEW_COLLECTIONS is set to a truthy
    value ("1"/"true"/"yes", case-insensitive) -- this is the mechanism
    the ~20 existing FilesystemVectorStore call sites (CLI, daemon,
    server) automatically inherit WITHOUT any of them being individually
    modified.

    Bug #1486 Fix B briefly flipped this effective default to True; Story
    #1488 SUPERSEDED that decision. The CLI/daemon path deliberately keeps
    the SHARDED_JSON default, and the SERVER states the layout explicitly
    instead (it passes ``--new-collection-layout=chunks_db`` to every
    server-side ``cidx index`` child, mapped to the constructor param) --
    so a fresh server-provisioned collection is CHUNKS_DB by intent, while
    a lone CLI/daemon user is never silently opted in. Parsed fresh (not
    cached) so tests can monkeypatch the env var per-test.
    """
    return os.getenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class PathIndex:
    """Reverse index mapping file_path -> Set[point_id].

    Prevents duplicate chunks when files are re-indexed by maintaining
    a mapping from file paths to all point IDs associated with that file.
    This enables pre-upsert cleanup of old vectors before inserting new ones.

    Story #540: Fix duplicate chunks bug.
    """

    def __init__(self) -> None:
        """Initialize empty path index."""
        self._path_index: Dict[str, Set[str]] = {}

    def add_point(self, file_path: str, point_id: str) -> None:
        """Add a point_id to a file's set of point_ids.

        Args:
            file_path: Path to the file
            point_id: Point ID to add

        Note:
            If file_path doesn't exist in index, creates new set.
            Adding duplicate point_id is idempotent (set behavior).
        """
        if file_path not in self._path_index:
            self._path_index[file_path] = set()
        self._path_index[file_path].add(point_id)

    def remove_point(self, file_path: str, point_id: str) -> None:
        """Remove a point_id from a file's set of point_ids.

        Args:
            file_path: Path to the file
            point_id: Point ID to remove

        Note:
            If point_id is the last one for file_path, deletes the file's entry entirely.
            Removing nonexistent point_id or file_path is safe (no-op).
        """
        if file_path in self._path_index:
            self._path_index[file_path].discard(point_id)
            if not self._path_index[file_path]:
                del self._path_index[file_path]

    def get_point_ids(self, file_path: str) -> Set[str]:
        """Get all point_ids for a given file_path.

        Args:
            file_path: Path to the file

        Returns:
            Copy of the set of point_ids for this file (empty set if file not found)

        Note:
            Returns a copy to prevent external modification of internal state.
        """
        return self._path_index.get(file_path, set()).copy()

    def all_paths(self) -> Set[str]:
        """Return the set of every distinct file path currently tracked by
        this index (Bug #1575 Part A).

        Used by ``FilesystemVectorStore.distinct_content_paths()`` for the
        SHARDED_JSON layout -- a lightweight, memory-bounded read of the
        path set only (never point_ids or payloads).
        """
        return set(self._path_index.keys())

    def has_other_owner(self, point_id: str) -> bool:
        """Return True if any file in the index references the given point_id.

        Args:
            point_id: The point ID to look up.

        Returns:
            True if at least one file entry contains point_id; False otherwise.

        Thread safety:
            Callers MUST hold the enclosing _path_index_lock before calling
            this method. PathIndex has no internal lock — it relies on the
            caller's lock for safe concurrent access.

        Usage:
            Used by upsert_points STEP 1 (Bug #663 fix) to detect shared
            point_ids: after removing a file's path mapping, call this to
            check whether any other file still references the same point_id
            before scheduling deletion of the underlying vector file and
            _id_index entry.
        """
        return any(point_id in pids for pids in self._path_index.values())

    def merge_from(self, other: "PathIndex") -> None:
        """Merge all entries from *other* into this PathIndex.

        Uses add_point for each entry so the operation is idempotent: re-adding
        a (file_path, point_id) pair that already exists is a safe no-op (set
        semantics).

        Args:
            other: PathIndex whose entries will be added to self.

        Thread safety:
            Callers MUST hold the enclosing _path_index_lock before calling
            this method. PathIndex has no internal lock.

        Usage:
            Used by scroll_points lazy rebuild: after walking the collection
            on disk, merge the rebuilt index INTO the live index (rather than
            replacing it) so concurrent upserts that ran during the walk are
            not lost.
        """
        for file_path, point_ids in other._path_index.items():
            for point_id in point_ids:
                self.add_point(file_path, point_id)

    def snapshot(self) -> Dict[str, Set[str]]:
        """Return a copy of the internal path->point_ids mapping (each
        set copied too), safe to iterate WITHOUT racing concurrent
        add_point()/remove_point() mutations against the live object.

        Bug #1575 (unlocked-save race, dual-review Fix 3): ``save()``'s
        dict-comprehension iteration over the LIVE ``self._path_index``
        dict/sets could observe a concurrent mutation mid-iteration
        (``RuntimeError: dictionary changed size during iteration``) when
        the ``PathIndex`` object is saved by one thread while another
        thread mutates it. Copying under the caller's lock (this method
        does no locking itself) and iterating the COPY afterward
        eliminates the torn read.

        Thread safety: callers MUST hold the enclosing ``_path_index_lock``
        while calling this (same contract as ``has_other_owner``/
        ``merge_from``) -- it is the act of copying that must be atomic
        with respect to concurrent mutation, not anything the returned
        copy does afterward.
        """
        return {
            file_path: set(point_ids)
            for file_path, point_ids in self._path_index.items()
        }

    @staticmethod
    def _durable_msgpack_write(data: Dict[str, Any], path: Path) -> None:
        """Write ``data`` to ``path`` atomically AND durably: temp file +
        fsync + ``os.replace`` + parent-directory fsync (Bug #1407's
        established pattern, e.g. ``HNSWIndexManager.
        _atomic_write_metadata_durable``). Bug #1575 round 6 item 3: a
        bare ``open()``+``dump()`` left the target file vulnerable to
        truncation on a crash mid-write.
        """
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        fd_owned = False
        try:
            try:
                tmp_f = os.fdopen(tmp_fd, "wb")
                fd_owned = True
                with tmp_f:
                    msgpack.dump(data, tmp_f)
                    tmp_f.flush()
                    nfs_safe_fsync(tmp_f.fileno())
                os.replace(tmp_path_str, str(path))
            finally:
                if not fd_owned:
                    try:
                        os.close(tmp_fd)
                    except OSError as close_err:
                        logging.getLogger(__name__).warning(
                            "Failed to close unwritten temp fd %s: %s",
                            tmp_fd,
                            close_err,
                        )
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except OSError as cleanup_err:
                # Best-effort cleanup -- log and discard so the ORIGINAL
                # exception propagates.
                logging.getLogger(__name__).warning(
                    "Failed to clean up temp file %s: %s",
                    tmp_path_str,
                    cleanup_err,
                )
            raise
        fsync_directory(path.parent)

    @staticmethod
    def save_snapshot(snapshot: Dict[str, Set[str]], path: Path) -> None:
        """Save a PRE-CAPTURED snapshot (see :meth:`snapshot`) to disk
        using msgpack, durably (Bug #1575 round 6 item 3 -- see
        :meth:`_durable_msgpack_write`). Never touches any live
        ``PathIndex`` state, so it may safely be called without holding
        any lock -- the snapshot is already a private, non-shared copy.

        Args:
            snapshot: The result of a prior ``snapshot()`` call.
            path: File path to save to (will create parent directories).
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        # Convert sets to lists for msgpack serialization
        serializable_data = {
            file_path: list(point_ids) for file_path, point_ids in snapshot.items()
        }
        PathIndex._durable_msgpack_write(serializable_data, path)

    def save(self, path: Path) -> None:
        """Save path index to disk using msgpack.

        Args:
            path: File path to save to (will create parent directories)

        Note:
            Sets are serialized as lists in msgpack format. This method
            takes its OWN snapshot (no external lock coordination), so it
            is only safe to call when no concurrent mutator can be
            racing this SAME PathIndex instance (e.g. a private,
            not-yet-shared object, or a test). Code that saves a LIVE,
            SHARED PathIndex object (registered in
            ``FilesystemVectorStore._path_indexes``) must snapshot under
            ``_path_index_lock`` explicitly and call
            :meth:`save_snapshot` instead -- see
            ``FilesystemVectorStore._save_path_index``.
        """
        self.save_snapshot(self.snapshot(), path)

    @classmethod
    def load(cls, path: Path) -> "PathIndex":
        """Load path index from disk. Returns an empty ``PathIndex`` if
        the file is missing OR corrupt/truncated (Bug #1575 round 6 item
        3: never raise uncaught -- a bad bin must not brick
        ``begin_indexing()``; every caller's own "proven complete"
        machinery correctly triggers an authoritative rebuild instead).

        Args:
            path: File path to load from
        """
        instance = cls()

        if not path.exists():
            return instance

        try:
            with open(path, "rb") as f:
                serialized_data = msgpack.load(f)
        except (ValueError, msgpack.exceptions.UnpackException, OSError) as exc:
            logging.getLogger(__name__).warning(
                "Corrupt/unreadable path_index.bin at %s -- treating as absent (%s)",
                path,
                exc,
            )
            return instance

        if not isinstance(serialized_data, dict):
            logging.getLogger(__name__).warning(
                "path_index.bin at %s did not deserialize to a dict -- "
                "treating as absent",
                path,
            )
            return instance

        # Convert lists back to sets
        instance._path_index = {
            file_path: set(point_ids)
            for file_path, point_ids in serialized_data.items()
        }

        return instance


class FilesystemVectorStore:
    """Filesystem-based vector storage with git-aware optimization.

    Features:
    - Path-as-vector quantization for efficient storage
    - Git-aware chunk storage (blob hash for clean, text for dirty)
    - Thread-safe atomic writes
    - ID indexing for fast lookups
    """

    def __init__(
        self,
        base_path: Path,
        project_root: Optional[Path] = None,
        hnsw_index_cache: Optional[Any] = None,
        id_index_cache: Optional[Any] = None,
        skip_staleness_check: bool = False,
        memory_governor: Optional[Any] = None,
        use_chunks_db_for_new_collections: Optional[bool] = None,
        activation_id: Optional[str] = None,
        collection_meta_cache: Optional[Any] = None,
        # chunk_store_cache: typed Any (like hnsw_index_cache/id_index_cache/
        # collection_meta_cache above) to avoid a storage<->server import
        # cycle -- ChunkStoreThreadCache lives in storage.shared and is
        # lazily imported below; a precise type here would require an
        # eager top-level import this module deliberately avoids (Bug #1468
        # lazy-load discipline).
        chunk_store_cache: Optional[Any] = None,
        hnsw_num_threads: Optional[int] = None,
        # hnsw_sync_epoch_enabled: fail-closed gate, defaulting to True for
        # every existing CLI/solo/daemon caller. The server's storage-client
        # factory passes False in postgres/cluster storage mode, where
        # cross-node mutual exclusion for the mechanism this flag guards
        # could not be confirmed for every mutation entry point.
        hnsw_sync_epoch_enabled: bool = True,
    ):
        # collection_meta_cache: Story #1492 AC1 -- optional injected
        # CollectionMetaCache (server mode: a shared, cross-request
        # singleton; CLI/solo default: a fresh per-instance cache, still
        # eliminating the 4-5 intra-search-call redundant parses).
        # Constructed at the end of __init__ (see self._collection_meta_cache).
        """Initialize filesystem vector store.

        Args:
            base_path: Base directory for all collections
            project_root: Root directory of the project being indexed (for git operations)
            hnsw_index_cache: Optional HNSW index cache for server-side performance (Story #526)
            id_index_cache: Optional id_index cache for server-side performance (Bug #1078)
            skip_staleness_check: When True, skip _compute_file_hash in the git-repo Tier 1
                branch for immutable versioned snapshots (Bug #1181 Perf Fix #3). Default
                False preserves byte-identical CLI and mutable-path behavior.
            memory_governor: Optional MemoryGovernor for Story #1213 Story 3. Server mode
                passes get_memory_governor(); CLI leaves it None so eviction behavior is
                byte-identical to Bug #1171.
            use_chunks_db_for_new_collections: Story #1456 opt-in gate --
                when True, create_collection() marks fresh collections to
                be built using the consolidated chunks.db layout. When
                explicitly False, always uses the legacy sharded-JSON
                layout regardless of environment -- for every collection
                kind, temporal included. When None (default), a TEMPORAL
                collection resolves to CHUNKS_DB regardless of environment
                (Bug #1528: temporal never writes a new legacy
                vector_*.json file), while a SEMANTIC collection
                falls back to the CIDX_CHUNKS_DB_NEW_COLLECTIONS env var
                (which itself defaults to False when unset), so all ~20
                existing call sites inherit the SHARDED_JSON default
                without any of them needing individual changes.
                Story #1488: Bug #1486 Fix B's global default-flip was
                superseded -- the CLI/daemon path keeps SHARDED_JSON as
                the default, while the server passes an explicit
                ``--new-collection-layout=chunks_db`` (mapped to this
                param as True) at every server-side ``cidx index`` spawn
                site, so server-provisioned collections are CHUNKS_DB by
                explicit intent rather than a silent env default.
            activation_id: Story #1458 AC11 -- optional per-clone generation/
                identity token (a UUID stamped once at activated-repo clone
                materialization). Embedded into the HNSW/id_index shared
                cache keys via _activation_scoped_cache_key() so a
                deactivate-then-reactivate cycle that places a DIFFERENT
                clone at the SAME filesystem path is a guaranteed structural
                cache-miss, even when the chunks_db layout discriminator
                value is identical between the two clones (the discriminator
                alone is necessary but not sufficient for this case).
                Defaults to None for the CLI/solo/non-activated path, which
                keeps today's pure path-derived cache key byte-for-byte
                unchanged.
            hnsw_num_threads: Story #1493 flakiness investigation -- optional
                override forwarded to the HNSWIndexManager constructed by
                end_indexing() for its full-rebuild path (build_index/
                rebuild_from_vectors thread count for hnswlib's
                add_items()). None (default, every existing caller)
                preserves today's behavior exactly (HNSWIndexManager falls
                back to DEFAULT_HNSW_NUM_THREADS, hnswlib's own -1 "use
                every available core"). Tests needing fully deterministic,
                race-free HNSW graph construction may pass
                hnsw_num_threads=1; production never sets this.
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Initialize logger
        self.logger = logging.getLogger(__name__)

        # Store project root for git operations
        # If not provided, try to derive from base_path (go up two levels from .code-indexer/index/)
        if project_root is None:
            # base_path is typically .code-indexer/index/, so project_root is two levels up
            self.project_root = self.base_path.parent.parent
        else:
            self.project_root = Path(project_root)

        # Initialize components
        self.quantizer = VectorQuantizer(depth_factor=4, reduced_dimensions=64)
        self.matrix_manager = ProjectionMatrixManager()

        # ID index cache: {collection_name: {point_id: file_path}}
        self._id_index: Dict[str, Dict[str, Path]] = {}
        self._id_index_lock = threading.Lock()

        # Bug #1583: cache_keys for which get_point()'s reactive stale-index
        # rebuild has already been attempted this process. id_index.bin is a
        # CACHE, not an authority -- a vector_*.json file written outside the
        # normal write path (or a crash between writing the vector file and
        # persisting the updated id_index.bin) can leave it silently missing
        # an entry that genuinely exists on disk. get_point() heals this
        # REACTIVELY (only on an actual lookup miss, never on every load --
        # see get_point()'s docstring/comments for why an eager per-load scan
        # was rejected) and records the attempt here so a lookup for a
        # point_id that genuinely never existed triggers at most ONE full
        # disk rebuild per collection per process, not one per miss.
        self._id_index_reactive_rebuild_done: Set[str] = set()

        # File path cache: {collection_name: set of file paths}
        self._file_path_cache: Dict[str, set] = {}

        # Cache for collection metadata (read once, reuse forever)
        self._vector_size_cache: Dict[str, int] = {}
        self._collection_metadata_cache: Dict[str, Dict[str, Any]] = {}
        self._metadata_lock = threading.Lock()  # Protect cache from concurrent access

        # HNSW-001 & HNSW-002: Incremental update change tracking
        # Structure: {collection_name: {'added': set(), 'updated': set(), 'deleted': set()}}
        self._indexing_session_changes: Dict[str, Dict[str, set]] = {}

        # HNSW-001 (AC3): Daemon mode cache entry (optional, set by daemon service)
        # When set, enables in-memory HNSW updates for watch mode instead of disk I/O
        self.cache_entry: Optional[Any] = None

        # Story #526: Server-side HNSW index cache for 1800x performance improvement
        # When set, caches hnswlib.Index objects with TTL-based eviction
        self.hnsw_index_cache = hnsw_index_cache

        # Bug #1078: Server-side id_index cache to eliminate per-query pathlib deserialization
        # (~33% GIL time). Mirrors HNSW cache pattern. None in CLI/standalone mode.
        self.id_index_cache = id_index_cache

        # Story #1458 AC11: per-clone generation/identity token, embedded
        # into shared cache keys via _activation_scoped_cache_key(). None in
        # CLI/solo/non-activated mode (byte-identical pure-path key).
        self.activation_id: Optional[str] = activation_id

        # Bug #1181 Perf Fix #3: skip _compute_file_hash for immutable versioned snapshots.
        # Set True by the server layer when project_root is a proven-immutable .versioned path.
        # Default False preserves byte-identical CLI and mutable-path behavior.
        self.skip_staleness_check: bool = skip_staleness_check

        # Story #1213 Story 3: MemoryGovernor reference for conditional eviction.
        # Server mode injects get_memory_governor() via FilesystemBackend.get_vector_store_client().
        # CLI/solo leaves this None so eviction is byte-identical to Bug #1171.
        self.memory_governor: Optional[Any] = memory_governor

        # Story #1493 flakiness investigation: optional HNSWIndexManager
        # thread-count override, forwarded to the manager end_indexing()
        # constructs for its full-rebuild path. None (every existing
        # caller) preserves today's default multi-threaded behavior exactly.
        self._hnsw_num_threads: Optional[int] = hnsw_num_threads

        # Story #540: Path-to-point_ids reverse index for duplicate prevention
        # Structure: {collection_name: PathIndex}
        self._path_indexes: Dict[str, PathIndex] = {}
        # Bug #1575 Finding-1-regression fix: records, per cache key, whether
        # path_index.bin ACTUALLY EXISTED ON DISK the last time
        # begin_indexing() populated self._path_indexes for it -- an empty
        # PathIndex built from scratch (file missing) only ever learns
        # about the files THIS session touches, so it must never be
        # trusted as a complete picture of the whole collection. See
        # _get_live_session_path_index().
        self._path_index_loaded_from_file: Dict[str, bool] = {}
        # LOCK ORDER INVARIANT (B1): _id_index_lock and _path_index_lock must
        # NEVER be held simultaneously by delete_points.  delete_points collects
        # path-index work under _id_index_lock and applies it sequentially after
        # release.  upsert_points holds _path_index_lock for orphan cleanup and
        # may nest _id_index_lock inside — that direction is safe because the
        # outer _path_index_lock is not also held by any concurrent delete.
        # Violation of this invariant causes ABBA deadlock.
        self._path_index_lock = threading.Lock()

        # Story #669: Temporal metadata store for v2 format (lazy-initialized)
        self._temporal_metadata_store: Optional[TemporalMetadataStore] = None
        self._temporal_metadata_lock = threading.Lock()

        # Multimodal support: Track active subdirectory for each collection during indexing
        # Structure: {collection_name: subdirectory_or_none}
        self._active_subdirectories: Dict[str, Optional[str]] = {}

        # Bug #1575 Part C: replaces the bare, PER-STORE (not per-collection)
        # _branch_isolation_did_filtered_rebuild boolean -- a real, documented
        # scoping hazard (a sentinel set for collection A could be consumed by
        # collection B's still-pending end_indexing). Keyed by the RESOLVED
        # PHYSICAL collection path so one collection's rebuild/skip decision
        # is structurally unable to affect another's.
        self._hnsw_sync_sessions: Dict[str, "HNSWSyncSession"] = {}
        self._hnsw_sync_sessions_lock = threading.Lock()
        self._hnsw_sync_epoch_enabled: bool = hnsw_sync_epoch_enabled

        # Bug #1575 Part B: per-collection SHARDED_JSON scroll-session cache
        # -- the legacy scroll branch's id_to_file enumeration (rglob +
        # parse EVERY vector_*.json file) used to be rebuilt on EVERY single
        # page, ~(N/L) full O(N) rebuilds across one multi-page scroll. A
        # fresh scroll (offset=None) always rebuilds and write-through
        # populates this cache; a continuation call (offset given) reuses
        # it instead. Keyed via _id_cache_key so a nested-subdirectory
        # collection never collides with a same-named top-level one.
        self._scroll_sharded_json_index_cache: Dict[str, Dict[str, Path]] = {}
        self._scroll_sharded_json_index_cache_lock = threading.Lock()

        # Story #677: Memoize git repo root — invariant for the lifetime of this instance.
        # _repo_root_cached is the "already ran" sentinel (None is a valid cached value).
        self._cached_repo_root: Optional[Path] = None
        self._repo_root_cached: bool = False
        self._repo_root_lock: threading.Lock = threading.Lock()

        # Story #1456: production write-path opt-in (see __init__ docstring).
        # Bug #1528: the caller's TRI-STATE value is ALSO retained verbatim,
        # because create_collection() must distinguish "no layout
        # instruction given" (None -> env/context default) from an EXPLICIT
        # sharded_json request (False): TEMPORAL collections default to
        # CHUNKS_DB (temporal never writes a new legacy vector_*.json file)
        # while SEMANTIC collections keep Story #1488's SHARDED_JSON
        # CLI/daemon default.
        self._new_collection_layout_explicit: Optional[bool] = (
            use_chunks_db_for_new_collections
        )
        if use_chunks_db_for_new_collections is not None:
            self._use_chunks_db_for_new_collections: bool = (
                use_chunks_db_for_new_collections
            )
        else:
            self._use_chunks_db_for_new_collections = (
                _parse_use_chunks_db_for_new_collections_env()
            )
        # Per-collection in-memory intent, recorded by create_collection():
        # True means "this session is actively building this collection as
        # CHUNKS_DB" -- consulted BEFORE the on-disk discriminator exists
        # (see _is_chunks_db_collection).
        self._chunks_db_mode: Dict[str, bool] = {}

        # Story #1492 AC1: mtime-keyed cache of parsed collection_meta.json
        # (Finding C1, SEVERE -- eliminates the 4-5 redundant reads+parses
        # per search() call). Lazy-imported here (not module-level) to
        # match this file's existing chunk_layout import convention.
        if collection_meta_cache is None:
            from code_indexer.storage.shared.collection_meta_cache import (
                CollectionMetaCache,
            )

            collection_meta_cache = CollectionMetaCache()
        self._collection_meta_cache: Any = collection_meta_cache

        # Story #1492 AC3: per-thread cache of open ChunkStore handles
        # (Finding C5 -- avoids re-running schema DDL/dim-load/codec
        # construction on a repeat query against the same mutable
        # collection). threading.local()-based: NEVER shares a connection
        # across threads (Story #1456 AC7's binding sqlite3 contract).
        if chunk_store_cache is None:
            from code_indexer.storage.shared.chunk_store_cache import (
                ChunkStoreThreadCache,
            )

            chunk_store_cache = ChunkStoreThreadCache()
        self._chunk_store_cache: Any = chunk_store_cache

    def _is_chunks_db_collection(
        self, collection_name: str, collection_path: Path
    ) -> bool:
        """Story #1456: the single combined authority for "should THIS
        write/finalize operation treat this collection as CHUNKS_DB".

        Two cases:
        1. An in-progress FRESH build (THIS session's create_collection
           recorded intent in ``self._chunks_db_mode`` -- the on-disk
           discriminator does not exist yet, so ``resolve_chunk_layout()``
           alone would incorrectly say SHARDED_JSON during that window).
        2. A collection already consolidated in a PRIOR session (no
           in-memory intent in THIS fresh instance, but the durable
           discriminator is already committed -- the resolver correctly
           detects it).

        Read-only, post-completion consumers (search, get_point, etc.) use
        ``resolve_chunk_layout()`` directly -- they never have an active
        "building it right now" intent to consult.
        """
        if self._chunks_db_mode.get(collection_name):
            return True

        from code_indexer.storage.shared.chunk_layout import (
            ChunkLayout,
            resolve_chunk_layout,
        )

        return bool(resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB)

    def _id_cache_key(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> str:
        """Compose the shared bare-``collection_name``-keyed cache key
        (``self._id_index`` / ``self._vector_size_cache``) so a top-level
        collection (``base_path/X``) and a nested collection sharing the
        SAME name (``base_path/multimodal_index/X``) -- two DIFFERENT
        physical directories -- never collide in these in-memory caches.

        Returns ``collection_name`` UNCHANGED when ``subdirectory`` is
        falsy (``None`` or ``""``) -- byte-identical to every existing
        write-path/active-session caller, none of which ever pass a
        non-None ``subdirectory`` (confirmed: no ``begin_indexing`` /
        ``upsert_points`` / ``end_indexing`` call site in this codebase
        does). Returns a composed ``f"{subdirectory}::{collection_name}"``
        key when a subdirectory is given -- the ONLY reachable-today
        producer of a non-None ``subdirectory`` at these cache sites is
        ``MultiIndexQueryService``'s legacy multimodal read path, which
        always resolves the SAME (embedder-agnostic) physical directory
        for a given ``(collection_name, subdirectory)`` pair, so a
        composed-key read can never collide with itself.
        """
        if not subdirectory:
            return collection_name
        return f"{subdirectory}::{collection_name}"

    def _activation_scoped_cache_key(
        self, path_str: str, *, chunk_layout_token: Optional[str] = None
    ) -> str:
        """Story #1458 AC11: compose the shared (HNSW/id_index) cache key
        for ``path_str``, embedding the ``chunks_db`` layout-discriminator
        token (AC11 Technical Requirement #1) and this instance's
        ``activation_id`` (AC11 Technical Requirement #2 / Finding 7) when
        present.

        Returns ``path_str`` UNCHANGED when both ``chunk_layout_token`` is
        None and ``self.activation_id`` is None -- byte-identical to the
        pre-AC11 pure-path-derived key.

        ``chunk_layout_token`` (typically ``resolve_chunk_layout(...).value``,
        e.g. ``"sharded_json"`` or ``"chunks_db"``) is appended FIRST so a
        post-consolidation read (the discriminator flips at the SAME path)
        is a structural cache-miss, without requiring any active cross-node
        invalidation broadcast -- the miss is by-construction from the
        changed key.

        ``activation_id``, when set, is appended AFTER the layout token, so
        a different clone materialized at the SAME filesystem path
        (deactivate-then-reactivate) is ALSO a guaranteed structural
        cache-miss regardless of the layout-discriminator value, which alone
        is necessary but not sufficient for that case (Finding 7).
        """
        key = path_str
        if chunk_layout_token is not None:
            key = f"{key}:{chunk_layout_token}"
        if self.activation_id is not None:
            key = f"{key}:{self.activation_id}"
        return key

    def hnsw_cache_key_for_collection(self, collection_path: Path) -> str:
        """Return the EXACT shared-cache key ``search()`` stores this
        collection's HNSW entry under.

        Bug #1538: an external invalidation call site that hand-builds a bare
        path string composes a DIFFERENT key than ``search()`` does (the key
        embeds Story #1458 AC11's chunk-layout token and, for an activated
        repo, its ``activation_id``), so its ``invalidate()`` is a silent
        no-op. Every such caller must go through this method instead of
        reconstructing the format.

        The layout token is resolved FRESH from disk here -- deliberately not
        from ``_is_chunks_db_collection()``'s in-session build intent, which
        can diverge from the committed on-disk discriminator ``search()``
        actually keyed against (the same reasoning ``rebuild_hnsw_filtered()``
        documents at its own two ``invalidate()`` calls).
        """
        from code_indexer.storage.shared.chunk_layout import resolve_chunk_layout

        return self._activation_scoped_cache_key(
            str(Path(collection_path).resolve()),
            chunk_layout_token=resolve_chunk_layout(collection_path).value,
        )

    def _hnsw_sync_session_key(self, collection_path: Path) -> str:
        """Bug #1575 Part C: the RESOLVED PHYSICAL collection path is the
        sole key for ``self._hnsw_sync_sessions`` -- this is what makes one
        collection's rebuild/skip decision structurally unable to affect a
        different collection's (the exact scoping hazard the old bare
        ``_branch_isolation_did_filtered_rebuild`` boolean had).
        """
        return str(Path(collection_path).resolve())

    def _get_or_create_hnsw_sync_session(
        self, collection_path: Path, collection_name: str
    ) -> HNSWSyncSession:
        """Bug #1575 Part C: lazily create (or return the existing)
        per-physical-collection-path ``HNSWSyncSession``.

        When ``self._indexing_session_changes`` already has a live entry for
        ``collection_name`` (an active ``begin_indexing()``...
        ``end_indexing()`` bracket), the session's ``added``/``updated``/
        ``deleted`` sets are the SAME set objects (aliased, not copied) as
        that dict's -- so every ``upsert_points()``/``delete_points()`` call
        that already populates ``_indexing_session_changes`` is transitively
        tracked here with zero duplicate bookkeeping.

        When no such tracking exists (a mutation call outside any
        ``begin_indexing()`` bracket -- e.g. watch mode calling
        ``upsert_points()`` directly), the session gets fresh empty sets and
        ``complete_change_tracking`` is immediately set False: a mutation
        this store cannot precisely attribute must never be silently
        skipped at ``end_indexing()`` time, so it forces a full rebuild.
        """
        key = self._hnsw_sync_session_key(collection_path)
        with self._hnsw_sync_sessions_lock:
            session = self._hnsw_sync_sessions.get(key)
            if session is not None:
                return session

            layout = (
                ChunkLayout.CHUNKS_DB
                if self._is_chunks_db_collection(collection_name, collection_path)
                else ChunkLayout.SHARDED_JSON
            )
            prior = read_hnsw_sync_state(collection_path)
            start_epoch = prior.mutation_epoch if prior is not None else 0

            session = HNSWSyncSession(
                collection_path=collection_path,
                collection_name=collection_name,
                layout=layout,
                start_epoch=start_epoch,
            )

            tracked = self._indexing_session_changes.get(collection_name)
            if tracked is not None:
                session.added = tracked["added"]
                session.updated = tracked["updated"]
                session.deleted = tracked["deleted"]
            else:
                session.complete_change_tracking = False

            self._hnsw_sync_sessions[key] = session
            return session

    def abort_indexing(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> None:
        """Bug #1575 Part C: discard THIS process's in-memory
        ``HNSWSyncSession``/session-change tracking for ``collection_name``
        -- called when an indexing session is aborted (e.g. an exception
        propagates before ``end_indexing()`` runs).

        Deliberately does NOT touch the durable ``hnsw_sync`` state on disk:
        it correctly remains "dirty" from whatever mutations were already
        performed, so the next ``end_indexing()`` attempt (in this process
        or another) safely performs a full rebuild rather than trusting an
        abandoned in-memory session's incomplete tracking.
        """
        collection_path = self._get_collection_path(collection_name, subdirectory)
        key = self._hnsw_sync_session_key(collection_path)
        with self._hnsw_sync_sessions_lock:
            self._hnsw_sync_sessions.pop(key, None)
        if collection_name in self._indexing_session_changes:
            del self._indexing_session_changes[collection_name]
        if collection_name in self._active_subdirectories:
            del self._active_subdirectories[collection_name]

    def _mark_hnsw_dirty_before_mutation(
        self, collection_path: Path, collection_name: str
    ) -> None:
        """Bug #1575 Part C mutation protocol (dirty-before-write): acquire
        ``.index_rebuild.lock`` -> read+validate the current ``hnsw_sync``
        state -> durably write a "dirty" epoch transition BEFORE the caller
        performs its own storage mutation.

        No-op when ``self._hnsw_sync_epoch_enabled`` is False (AC46 cluster
        fail-closed gate) -- no session is created, so ``end_indexing()``
        observes "session missing" and always performs a full rebuild,
        byte-identical to pre-Part-C behavior.

        If the durable write fails, the exception propagates. Every caller
        of this method invokes it BEFORE its own storage mutation, so that
        propagation alone is sufficient to satisfy "if the dirty write
        fails, the mutation MUST NOT proceed".

        The lock is held ONLY for this read-decide-write sequence -- it is
        released (context manager exit) before this method returns, well
        before any embedding-provider call or other long-running work the
        caller might perform next.
        """
        if not self._hnsw_sync_epoch_enabled:
            return

        session = self._get_or_create_hnsw_sync_session(
            collection_path, collection_name
        )

        from .background_index_rebuilder import BackgroundIndexRebuilder

        rebuilder = BackgroundIndexRebuilder(collection_path)
        with rebuilder.acquire_lock():
            prior = read_hnsw_sync_state(collection_path)
            next_mutation, published = compute_dirty_transition(prior)

            branch = session.current_branch
            if branch is None and prior is not None:
                branch = prior.current_branch

            new_state = HNSWSyncState(
                schema_version=HNSW_SYNC_SCHEMA_VERSION,
                mutation_epoch=next_mutation,
                published_epoch=published,
                status="dirty",
                current_branch=branch,
                layout=session.layout.value,
            )
            write_hnsw_sync_state(collection_path, new_state)
            # Bug #1575 Part C review fix (Defect 2): record that THIS
            # session personally caused this exact epoch advancement --
            # the decision engine compares this count against the on-disk
            # epoch delta since the session started to detect a mutation
            # this session never observed (a different session/process
            # advanced the epoch independently).
            session.own_mutation_count += 1

    def _get_collection_path(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> Path:
        """Get the path to a collection, optionally within a subdirectory.

        Args:
            collection_name: Name of the collection
            subdirectory: Optional subdirectory (e.g., "multimodal_index")

        Returns:
            Path to the collection directory

        Example:
            _get_collection_path("my_coll") -> base_path/my_coll
            _get_collection_path("my_coll", "multimodal_index") -> base_path/multimodal_index/my_coll

        Story #1457 AC8: when this instance was constructed WITH a
        TemporalShardResolver AND collection_name parses as a temporal
        collection name AND the resolver finds a match, resolves through it
        instead of direct construction -- per-instance-gated, dual-mode.
        Every other case (no resolver, non-temporal name, no subdirectory
        override, or no resolver match) falls through UNCHANGED to direct
        construction, so this is byte-identical for every existing
        production call site (none of which inject a resolver yet).
        """
        if subdirectory:
            return self.base_path / subdirectory / collection_name

        # Bug #1529: NO resolver indirection. Story #1457's pointer-first
        # TemporalShardResolver hook is retired -- a temporal shard's physical
        # path is fixed from first creation, so a collection path is always a
        # direct, deterministic construction. The store is pointed AT the
        # right root by its base_path (see temporal_server_paths.py), which is
        # the only place the temporal location is decided.
        return self.base_path / collection_name

    def create_collection(
        self, collection_name: str, vector_size: int, subdirectory: Optional[str] = None
    ) -> bool:
        """Create a new collection with projection matrix.

        Args:
            collection_name: Name of the collection
            vector_size: Size of input vectors (e.g., 1536)
            subdirectory: Optional subdirectory path (e.g., "multimodal_index")

        Returns:
            True if created successfully
        """
        collection_path = self._get_collection_path(collection_name, subdirectory)
        collection_path.mkdir(parents=True, exist_ok=True)

        # Story #726: Removed _ensure_gitignore() call.
        # CIDX must not modify files outside .code-indexer/ directory.
        # The .gitignore modification was causing git pull failures in golden repositories.

        # Create projection matrix for this collection
        output_dim = 64  # Target 64-dim for 32-char hex path
        projection_matrix = self.matrix_manager.create_projection_matrix(
            input_dim=vector_size, output_dim=output_dim
        )

        # Save projection matrix
        self.matrix_manager.save_matrix(projection_matrix, collection_path)

        # Compute quantization range dynamically from projection matrix dimensions
        # Uses random projection theory: projected vectors have std ≈ sqrt(output_dim / input_dim)
        # We use ±3σ to cover 99.7% of the distribution
        std_estimate = np.sqrt(output_dim / vector_size)
        min_val = -3 * std_estimate
        max_val = 3 * std_estimate

        # Create collection metadata with dynamically computed quantization range
        # Range will be used for locality-preserving fixed-range scalar quantization
        metadata = {
            "name": collection_name,
            "vector_size": vector_size,
            "created_at": datetime.utcnow().isoformat(),
            "quantization_range": {
                "min": float(min_val),  # Dynamically computed from matrix dimensions
                "max": float(max_val),
            },
        }

        # Store subdirectory in metadata if provided
        if subdirectory:
            metadata["subdirectory"] = subdirectory

        metadata_path = collection_path / "collection_meta.json"
        self._atomic_write_json(metadata_path, metadata, fsync=True)

        # Initialize ID index for this collection. Keyed via _id_cache_key
        # using the EXPLICIT subdirectory param (not _active_subdirectories,
        # which is only populated by begin_indexing -- create_collection can
        # run before begin_indexing, so relying on that fallback here would
        # incorrectly resolve to the bare/top-level key even for a nested
        # subdirectory build) so a nested create_collection() never resets
        # the top-level collection's in-memory _id_index entry (Codex NEW
        # Finding 1).
        #
        # Dual-review correction (Fix 3, Codex 95%): also discard any
        # pre-existing _id_index_reactive_rebuild_done marker for this SAME
        # cache_key. Without this, a collection whose marker was already set
        # (e.g. from an earlier confirmed-negative reactive scan, or --
        # pre-Fix-2 -- an earlier successful heal) would have its in-memory
        # _id_index reset to {} here while the marker stayed set, silently
        # suppressing every future lookup for a point that genuinely exists
        # on disk. Not reachable via any live production call site today
        # (every real caller guards with collection_exists() first), but
        # closed anyway for correctness/defense-in-depth.
        with self._id_index_lock:
            _create_collection_cache_key = self._id_cache_key(
                collection_name, subdirectory
            )
            self._id_index[_create_collection_cache_key] = {}
            self._id_index_reactive_rebuild_done.discard(_create_collection_cache_key)

        # Story #1456: record CHUNKS_DB build intent for THIS session. The
        # on-disk discriminator is committed later (end_indexing), only
        # AFTER chunks.db + all its indexes are durable -- see AC1.
        #
        # Bug #1528: temporal collections used to be excluded from CHUNKS_DB
        # UNCONDITIONALLY here, which discarded even an explicit caller
        # request (including the server's own
        # `--new-collection-layout=chunks_db` child arg) and made Epic
        # #1454's consolidation inert for exactly the workload that
        # motivated it -- one real repo accumulated 487,076 individual
        # vector_*.json files. Temporal is now the STRICTEST case instead of
        # the exempt one: a fresh temporal collection is built as CHUNKS_DB
        # by DEFAULT (no flag, no env var, no server context required), and
        # only an EXPLICIT sharded_json request from the caller
        # (`use_chunks_db_for_new_collections=False`, e.g. a legacy-layout
        # test fixture) still produces the legacy layout. Semantic
        # collections are UNCHANGED: Story #1488's context-dependent
        # default (SHARDED_JSON for CLI/daemon, explicit chunks_db from the
        # server) still governs them.
        if TemporalMetadataStore.is_temporal_collection(collection_name):
            build_as_chunks_db = self._new_collection_layout_explicit is not False
            if not build_as_chunks_db:
                # Bug #1529 review item 4: the storage layer cannot tell a
                # test fabricating pre-#1528 data from a production mistake,
                # so it must not refuse -- real fleet data is still
                # SHARDED_JSON until migrated, and the legacy read/migrate
                # paths have to stay exercisable. But this combination has no
                # legitimate production caller (the CLI refuses
                # --new-collection-layout=sharded_json with --index-commits;
                # the server always requests chunks_db), so an occurrence in
                # a real deployment log is a five-alarm signal and must not
                # be silent.
                self.logger.warning(
                    "Building TEMPORAL collection %s in the legacy "
                    "SHARDED_JSON layout because the caller explicitly "
                    "requested it. Temporal indexing must never write legacy "
                    "vector_*.json files (Bug #1528) -- if this appears in a "
                    "server or CLI log, a caller is bypassing that rule.",
                    collection_name,
                )
        else:
            build_as_chunks_db = self._use_chunks_db_for_new_collections
        if build_as_chunks_db:
            self._chunks_db_mode[collection_name] = True

        return True

    def collection_exists(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> bool:
        """Check if collection exists and has a valid metadata file.

        A collection is considered to exist only when its ``collection_meta.json``
        is present, non-empty, parses as JSON, and contains a ``vector_size``
        field.  An empty or corrupt file (e.g. from a crashed write) returns
        False so the indexing path recreates the collection cleanly (Bug #1223
        Defect B self-heal).

        Args:
            collection_name: Name of the collection
            subdirectory: Optional subdirectory path (e.g., "multimodal_index")

        Returns:
            True if collection exists with a valid metadata file
        """
        # Story #1492 AC1: routed through the shared mtime-keyed cache so a
        # search() call that follows this check with a vector_size read
        # (and is_stale()/resolve_chunk_layout() calls) reuses the SAME
        # parsed content instead of re-reading the file. Behavior is
        # unchanged: a missing/empty/corrupt/non-dict file resolves to
        # None from the cache, matching the prior try/except-False path
        # exactly (isinstance/membership semantics preserved -- "vector_size"
        # in meta is False for a dict lacking the key, and the cache never
        # returns a non-dict value at all).
        collection_path = self._get_collection_path(collection_name, subdirectory)
        meta = self._collection_meta_cache.get(collection_path)
        if meta is None:
            return False
        return "vector_size" in meta

    def list_collections(self) -> List[str]:
        """List all collections.

        Returns:
            List of collection names
        """
        collections = []
        for path in self.base_path.iterdir():
            if path.is_dir():
                metadata_path = path / "collection_meta.json"
                if metadata_path.exists():
                    collections.append(path.name)
        return collections

    def begin_indexing(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> None:
        """Prepare for batch indexing operations.

        Called ONCE before indexing session starts. Clears file path cache to ensure
        fresh data after indexing.

        Args:
            collection_name: Name of the collection to begin indexing
            subdirectory: Optional subdirectory path (e.g., "multimodal_index")

        Note:
            This is part of the storage provider lifecycle interface that enables O(n)
            performance by deferring index rebuilding until end_indexing().

            HNSW-001 & HNSW-002: Initializes change tracking for incremental HNSW updates.
            Story #540: Loads PathIndex for duplicate prevention during upserts.
        """
        self.logger.info(
            f"Beginning indexing session for collection '{collection_name}'"
            + (f" in subdirectory '{subdirectory}'" if subdirectory else "")
        )

        # Store active subdirectory for this collection
        self._active_subdirectories[collection_name] = subdirectory

        # Clear file path cache for this collection
        with self._id_index_lock:
            if collection_name in self._file_path_cache:
                del self._file_path_cache[collection_name]

        # HNSW-001 & HNSW-002: Initialize change tracking for incremental updates
        self._indexing_session_changes[collection_name] = {
            "added": set(),
            "updated": set(),
            "deleted": set(),
        }

        # Story #540: Load PathIndex for duplicate prevention. Keyed via
        # _id_cache_key using the EXPLICIT subdirectory param so a nested
        # begin_indexing() call never reuses/collides with the top-level
        # collection's in-memory PathIndex (Codex NEW Finding 2).
        with self._path_index_lock:
            _begin_indexing_cache_key = self._id_cache_key(
                collection_name, subdirectory
            )
            # Bug #1575 Finding-1-regression fix: record whether
            # path_index.bin actually exists on disk RIGHT NOW, regardless
            # of whether the in-memory entry below is reused or freshly
            # loaded -- this is the "was the picture ever proven complete"
            # signal _get_live_session_path_index() gates its cache-trust
            # decision on (used by distinct_content_paths()/
            # fetch_points_for_paths() -- Part A. NOT by
            # _calculate_and_save_unique_file_count(), whose SHARDED_JSON
            # branch abandoned this fast-path trust entirely). A cheap
            # single stat() call.
            _begin_indexing_collection_path = self._get_collection_path(
                collection_name, subdirectory
            )
            self._path_index_loaded_from_file[_begin_indexing_cache_key] = (
                _begin_indexing_collection_path / "path_index.bin"
            ).exists()
            if _begin_indexing_cache_key not in self._path_indexes:
                self._path_indexes[_begin_indexing_cache_key] = self._load_path_index(
                    collection_name
                )

        self.logger.debug(f"Change tracking initialized for '{collection_name}'")

    def end_indexing(
        self,
        collection_name: str,
        progress_callback: Optional[Any] = None,
        skip_hnsw_rebuild: bool = False,
        subdirectory: Optional[str] = None,
        force_full_rebuild: bool = False,
        clear_stale: bool = True,
    ) -> Dict[str, Any]:
        """Finalize indexing by rebuilding HNSW and ID indexes.

        Called ONCE after all upsert_points() operations complete. This is where
        the O(n²) → O(n) optimization happens - we rebuild indexes only once instead
        of after every upsert.

        Args:
            collection_name: Name of the collection
            progress_callback: Optional callback for progress reporting
            skip_hnsw_rebuild: If True, skip HNSW rebuild and mark index as stale
                             (watch mode optimization - defer rebuild to query time)
            subdirectory: Optional subdirectory path (e.g., "multimodal_index")
            force_full_rebuild: Bug #1407 Amendment 3 -- when True, takes
                             PRECEDENCE over the _branch_isolation_did_filtered_rebuild
                             sentinel, skip_hnsw_rebuild, and session-change
                             detection: discards this collection's tracked
                             session changes and directly runs a non-filtered
                             rebuild_from_vectors(). Used by the temporal
                             per-shard finalize barrier for a was_stale shard
                             (an incremental append onto a possibly-inconsistent
                             .bin would be wrong). CRITICAL: bypasses the
                             sentinel WITHOUT consuming/resetting it -- a set
                             sentinel may belong to a DIFFERENT collection's
                             still-pending end_indexing (re-guards Bug #941).
            clear_stale: Bug #1407 Amendment 1/2 -- when True (default,
                             today's unchanged fleet-wide behavior), the
                             underlying HNSW writer marks the index fresh
                             (is_stale=False). When False, staleness is
                             PRESERVED through this call -- only the caller's
                             own explicit HNSWIndexManager.clear_stale() call,
                             made strictly after end_indexing() returns
                             successfully, may mark the shard fresh.

        Returns:
            Status dictionary with rebuild results and hnsw_skipped flag

        Raises:
            ValueError: If collection doesn't exist

        Note:
            Before this optimization, upsert_points() rebuilt indexes after EVERY file,
            causing O(n²) complexity. Now we rebuild indexes ONCE at the end.

            Watch Mode Optimization: When skip_hnsw_rebuild=True, HNSW rebuild is
            deferred to query time via staleness marking. This prevents watch mode
            from spending 5-10 seconds rebuilding HNSW after every batch of file changes.
        """
        collection_path = self._get_collection_path(collection_name, subdirectory)

        if not self.collection_exists(collection_name, subdirectory):
            # Bug #1575 Part C: a stale in-memory HNSWSyncSession for this
            # exact collection path (created by an earlier upsert_points()/
            # delete_points() this same process) must never survive a
            # failed end_indexing() attempt -- discard it here so the next
            # begin_indexing()/mutation for this collection starts with a
            # fresh session rather than reusing stale added/updated/deleted
            # tracking sets against a collection that is about to be (or
            # was already) recreated from scratch.
            self._discard_hnsw_sync_session(collection_path)
            raise ValueError(f"Collection '{collection_name}' does not exist")

        self.logger.info(f"Finalizing indexes for collection '{collection_name}'...")

        # Story #1456 AC1: computed ONCE, up front. _is_chunks_db_collection
        # (not the bare resolver) correctly detects an IN-PROGRESS fresh
        # build too -- the on-disk discriminator does not exist yet during
        # the very first end_indexing() call for a new CHUNKS_DB collection.
        _end_indexing_is_chunks_db = self._is_chunks_db_collection(
            collection_name, collection_path
        )
        from code_indexer.storage.shared.chunk_layout import ChunkLayout

        _end_indexing_layout_override = (
            ChunkLayout.CHUNKS_DB if _end_indexing_is_chunks_db else None
        )

        # Bug #1575 Part C: the visibility-epoch decision engine replaces
        # the old sentinel/session-change-based incremental-vs-full-rebuild
        # logic entirely.
        hnsw_skipped = False
        hnsw_action: Optional[str] = None

        if skip_hnsw_rebuild:
            # Watch mode: preserve existing behavior EXACTLY -- defer
            # rebuild to query time via staleness marking. hnsw_sync stays
            # dirty on disk (the dirty-before-mutation protocol already
            # wrote it at each upsert/delete call within this session), so
            # the NEXT end_indexing() call correctly forces a full rebuild.
            from .hnsw_index_manager import HNSWIndexManager

            vector_size = self._get_vector_size(collection_name)
            hnsw_manager = HNSWIndexManager(
                vector_dim=vector_size,
                space="cosine",
                num_threads=self._hnsw_num_threads,
            )
            hnsw_manager.mark_stale(collection_path)
            hnsw_skipped = True
            self.logger.info(
                f"HNSW rebuild skipped for '{collection_name}' (watch mode), "
                f"marked as stale for query-time rebuild"
            )
            # Bug #1575 Part C review finding: this in-memory session must
            # be discarded here too, exactly like every other terminal path
            # in this method -- otherwise it survives (still aliased to
            # THIS session's now-frozen added/updated/deleted sets) into
            # the NEXT begin_indexing()/end_indexing() cycle for this same
            # collection, where it would be wrongly reused instead of a
            # fresh session correctly aliased to that next cycle's own
            # tracking sets (silently omitting that next cycle's real
            # mutations from a "trusted" incremental update).
            self._discard_hnsw_sync_session(collection_path)
        else:
            sync_result = self._resolve_and_publish_hnsw_sync(
                collection_name,
                subdirectory=subdirectory,
                progress_callback=progress_callback,
                force_full_rebuild=force_full_rebuild,
                clear_stale=clear_stale,
            )
            hnsw_action = sync_result["action"]
            self.logger.info(
                f"HNSW sync for '{collection_name}': {hnsw_action} "
                f"({sync_result['reason']})"
            )

        # Story #1456 AC7: CHUNKS_DB collections never load or write
        # id_index.bin here -- point-id resolution is exclusively via the
        # chunk store, and vector_count is read directly from it.
        if _end_indexing_is_chunks_db:
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                vector_count = chunk_store.count()
            finally:
                chunk_store.close()
        else:
            # Save ID index to disk (ALWAYS - needed for queries)
            from .id_index_manager import IDIndexManager

            id_manager = IDIndexManager()
            # Keyed via _id_cache_key using the EXPLICIT subdirectory
            # parameter so finalizing a nested indexing session reads/writes
            # its OWN _id_index entry, never the top-level collection's
            # (Codex NEW Finding 1).
            _end_indexing_id_cache_key = self._id_cache_key(
                collection_name, subdirectory
            )
            with self._id_index_lock:
                # BUG FIX: Load ID index from disk if not in memory (reconciliation path)
                # When reconciliation finds all commits indexed and calls end_indexing(),
                # _id_index is empty because no new vectors were upserted.
                if (
                    _end_indexing_id_cache_key not in self._id_index
                    or not self._id_index[_end_indexing_id_cache_key]
                ):
                    self._id_index[_end_indexing_id_cache_key] = self._load_id_index(
                        collection_name, subdirectory
                    )

                if _end_indexing_id_cache_key in self._id_index:
                    id_manager.save_index(
                        collection_path, self._id_index[_end_indexing_id_cache_key]
                    )

            vector_count = len(self._id_index.get(_end_indexing_id_cache_key, {}))

        # Bug #1575 Part A Round 3, Fix A: calculate (and durably persist to
        # collection_meta.json) the unique file count BEFORE saving
        # path_index.bin below. This method's fallback path (taken when no
        # active-session PathIndex is trusted, e.g. this session's
        # path_index.bin did not exist at begin_indexing() time) REPAIRS
        # self._path_indexes[cache_key] in place with the complete,
        # authoritative picture it was forced to compute
        # (_rebuild_and_repair_path_index, via _resolve_authoritative_path_
        # index) -- so the path_index.bin save immediately below this
        # persists that repaired, complete PathIndex rather than the
        # partial, session-own one that would otherwise be saved as-is.
        # Reordered from AFTER the path_index.bin save (its pre-fix
        # position) to BEFORE it: computing this count never depended on
        # path_index.bin already being saved, so this is a pure reorder,
        # not a new dependency.
        unique_file_count = self._calculate_and_save_unique_file_count(
            collection_name, collection_path, subdirectory=subdirectory
        )

        # Story #540: Save path index to disk. Keyed via _id_cache_key using
        # the EXPLICIT subdirectory parameter (Codex NEW Finding 2).
        # Bug #1575 Fix 3: the membership check + live-reference lookup is
        # done under a quick _path_index_lock scope, but _save_path_index()
        # itself is now called OUTSIDE the lock -- it acquires
        # _path_index_lock internally to take its own snapshot before
        # writing (see _save_path_index's docstring), and this is a plain
        # (non-reentrant) threading.Lock, so holding it here across that
        # call would deadlock the same thread trying to re-acquire it.
        _end_indexing_path_cache_key = self._id_cache_key(collection_name, subdirectory)
        with self._path_index_lock:
            _end_indexing_live_path_index = self._path_indexes.get(
                _end_indexing_path_cache_key
            )
        if _end_indexing_live_path_index is not None:
            self._save_path_index(
                collection_name,
                _end_indexing_live_path_index,
                subdirectory=subdirectory,
            )

        # Story #1456 AC1 (mandatory FINAL step): chunks.db + HNSW +
        # path_index are all durable above -- ONLY NOW is it safe to commit
        # the discriminator that makes this collection discoverable as
        # CHUNKS_DB to every other reader. Idempotent: a no-op re-commit on
        # every subsequent re-index session of an already-consolidated
        # collection is harmless.
        if _end_indexing_is_chunks_db:
            from code_indexer.storage.shared.chunk_layout import (
                write_chunks_db_discriminator,
            )

            write_chunks_db_discriminator(collection_path)

        self.logger.info(
            f"Indexing finalized for '{collection_name}': {vector_count} vectors indexed "
            f"({unique_file_count} unique files)"
        )

        result = {
            "status": "ok",
            "vectors_indexed": vector_count,
            "unique_files": unique_file_count,
            "collection": collection_name,
            "hnsw_skipped": hnsw_skipped,
        }

        # Add HNSW update type if incremental was used. force_full_rebuild
        # reuses the "already handled" {} sentinel but ran a full rebuild,
        # not an incremental update -- must not be mislabeled (Bug #1407).
        if hnsw_action == _ACTION_INCREMENTAL:
            result["hnsw_update"] = "incremental"

        # Clean up active subdirectory tracking
        if collection_name in self._active_subdirectories:
            del self._active_subdirectories[collection_name]

        # Bug #1575 Part C: this tracking dict must always be reset at the
        # end of an indexing session regardless of which decision-engine
        # path ran (reuse/incremental/full-rebuild) -- pre-Part-C behavior
        # only cleared it inside the old incremental-update branch, but
        # every path now needs a fresh dict for the NEXT session. Guarded
        # by _id_index_lock, matching upsert_points()/delete_points()'s own
        # locking convention for this exact dict.
        with self._id_index_lock:
            if collection_name in self._indexing_session_changes:
                del self._indexing_session_changes[collection_name]

        return result

    def set_hnsw_branch_context(
        self,
        collection_name: str,
        current_branch: Optional[str],
        visible_files: Set[str],
        subdirectory: Optional[str] = None,
    ) -> None:
        """Bug #1575 Part C item 5: register branch-isolation context for
        THIS collection's session WITHOUT performing any rebuild --
        ``end_indexing()``'s decision engine reads ``current_branch`` /
        ``visible_files`` from the session to decide reuse / incremental /
        full-rebuild.

        A branch switch (``current_branch`` differing from the currently
        PUBLISHED ``hnsw_sync.current_branch``) always forces a full
        filtered rebuild at ``end_indexing()`` time -- even when
        ``visible_files`` is byte-for-byte identical to the previous
        branch's -- because the comparison happens on ``current_branch``
        itself, never on a bare path-set fingerprint (AC48).

        No-op when ``self._hnsw_sync_epoch_enabled`` is False (AC46 cluster
        fail-closed gate) -- performs ZERO work in that case, including no
        collection_path resolution.
        """
        if not self._hnsw_sync_epoch_enabled:
            return
        collection_path = self._get_collection_path(collection_name, subdirectory)
        session = self._get_or_create_hnsw_sync_session(
            collection_path, collection_name
        )
        session.current_branch = current_branch
        session.visible_files = set(visible_files)
        session.branch_context_set = True

    def _resolve_hnsw_rebuild_reason(
        self,
        prior: Optional[HNSWSyncState],
        session: Optional[HNSWSyncSession],
    ) -> Optional[str]:
        """Bug #1575 Part C decision algorithm, first branch: returns a
        non-None reason string when a FULL filtered rebuild is required
        (fail-safe), or None when the epoch/session state is well-formed
        enough to consider reuse-or-incremental instead. Never called for
        the separate, unconditional ``force_full_rebuild=True`` path (see
        ``_resolve_and_publish_hnsw_sync``), which always short-circuits
        before this method is reached.
        """
        if prior is None:
            return (
                "no valid hnsw_sync state (missing/malformed/pre-existing collection)"
            )
        if session is None:
            return "no in-memory session tracked for this indexing run"
        if not session.complete_change_tracking:
            return "incomplete change tracking for this session"
        if session.current_branch != prior.current_branch:
            return (
                f"branch changed ({prior.current_branch!r} -> "
                f"{session.current_branch!r})"
            )
        # Bug #1575 Part C review fix (Defect 2): a session must never be
        # trusted as a COMPLETE record of every mutation since the last
        # clean publish unless BOTH of the following hold. Each guards a
        # distinct, independently-reproduced failure sequence -- neither
        # implies the other, both are required.
        #
        # (1) This session started tracking from the exact epoch that is
        #     CURRENTLY still the published (durable-clean) one. If it
        #     started already-dirty (e.g. resuming after a DIFFERENT,
        #     since-discarded/aborted session already advanced
        #     mutation_epoch beyond published_epoch), this session has no
        #     knowledge of that earlier session's mutations.
        if session.start_epoch != prior.published_epoch:
            return (
                f"session start_epoch ({session.start_epoch}) does not "
                f"match the currently published epoch "
                f"({prior.published_epoch}) -- a different session's "
                "mutations may never have been applied to the HNSW graph"
            )
        # (2) Every epoch increment since this session started is fully
        #     accounted for by this session's OWN mutation calls. A
        #     foreign session/process (a different in-process session, or
        #     a completely separate FilesystemVectorStore instance/process
        #     against the same on-disk collection) that mutated
        #     concurrently would advance mutation_epoch without ever
        #     going through THIS session's tracking -- check (1) alone
        #     cannot detect that, since it only inspects the epoch value
        #     at THIS session's start, not what happened during its
        #     lifetime.
        epoch_delta_since_start = prior.mutation_epoch - session.start_epoch
        if epoch_delta_since_start != session.own_mutation_count:
            return (
                f"epoch advanced by {epoch_delta_since_start} since this "
                f"session started, but this session only tracked "
                f"{session.own_mutation_count} of its own mutations -- a "
                "concurrent mutation this session never observed occurred"
            )
        return None

    def _publish_clean_hnsw_sync(
        self,
        collection_path: Path,
        epoch: int,
        current_branch: Optional[str],
        layout: ChunkLayout,
    ) -> None:
        """Bug #1575 Part C: durably publish a CLEAN hnsw_sync state
        (``mutation_epoch == published_epoch``) after a successful
        rebuild/reuse/incremental decision.

        No-op when ``self._hnsw_sync_epoch_enabled`` is False (AC46
        cluster fail-closed gate, review Defect 3b) -- the rebuild itself
        already ran (correct, safe, full/unfiltered), but the mechanism is
        not supposed to be active at all in this mode, so no ``hnsw_sync``
        bookkeeping key is ever written. Leaving one behind would be
        wrongly trusted as authoritative by any reader that later (or
        incorrectly) believes the mechanism IS active for this collection.
        """
        if not self._hnsw_sync_epoch_enabled:
            return
        write_hnsw_sync_state(
            collection_path,
            HNSWSyncState(
                schema_version=HNSW_SYNC_SCHEMA_VERSION,
                mutation_epoch=epoch,
                published_epoch=epoch,
                status="clean",
                current_branch=current_branch,
                layout=layout.value,
            ),
        )

    def _discard_hnsw_sync_session(self, collection_path: Path) -> None:
        """Bug #1575 Part C: drop THIS process's in-memory session for
        ``collection_path`` after a decision has been fully published --
        the next ``end_indexing()`` call for this collection starts fresh,
        re-reading the (now clean) durable state.
        """
        key = self._hnsw_sync_session_key(collection_path)
        with self._hnsw_sync_sessions_lock:
            self._hnsw_sync_sessions.pop(key, None)

    def _read_published_hnsw_vector_count(self, collection_path: Path) -> int:
        """Bug #1575 Part C: the vector count currently published in the
        HNSW artifact (used for the "reused byte-for-byte" result), read
        directly from ``collection_meta.json['hnsw_index']['vector_count']``.
        Fail-safe: returns 0 on any read/parse/shape error (never raises) --
        acceptable here because this is purely a REPORTING value; the
        artifact's actual validity was already proven by
        ``validate_hnsw_artifact_for_reuse`` before this is called.
        """
        meta_file = collection_path / "collection_meta.json"
        try:
            with open(meta_file) as f:
                metadata = json.load(f)
            if not isinstance(metadata, dict):
                raise ValueError("collection_meta.json did not parse to a dict")
            hnsw_info = metadata.get("hnsw_index")
            if not isinstance(hnsw_info, dict):
                raise ValueError("hnsw_index metadata section is not a dict")
            return int(hnsw_info.get("vector_count", 0))
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            AttributeError,
        ) as exc:
            self.logger.debug(
                "Could not read published HNSW vector_count for %s: %s (reporting 0)",
                collection_path,
                exc,
            )
            return 0

    def _perform_full_filtered_rebuild(
        self,
        # Any: HNSWIndexManager itself types its hnswlib.Index handle as
        # Any (a C extension with no Python type stubs) -- matching that
        # existing convention rather than inventing a narrower type here.
        hnsw_manager: Any,
        collection_path: Path,
        session: Optional[HNSWSyncSession],
        progress_callback: Optional[Any],
        clear_stale: bool,
        layout_override: Optional[ChunkLayout],
    ) -> int:
        """Bug #1575 Part C: perform a full rebuild using the SESSION's
        branch-filter context when available (a "full FILTERED rebuild",
        the decision algorithm's fail-safe branch) -- ``lock_already_held``
        because the caller already holds ``.index_rebuild.lock`` for the
        whole decision.
        """
        visible_files = None
        current_branch = None
        if session is not None and session.branch_context_set:
            visible_files = session.visible_files
            current_branch = session.current_branch
        return int(
            hnsw_manager.rebuild_from_vectors(
                collection_path=collection_path,
                progress_callback=progress_callback,
                visible_files=visible_files,
                current_branch=current_branch,
                clear_stale=clear_stale,
                layout_override=layout_override,
                project_root=self.project_root,
                lock_already_held=True,
            )
        )

    def _finish_full_rebuild(
        self,
        # Any: HNSWIndexManager types its hnswlib.Index handle as Any (a C
        # extension with no Python type stubs) -- matching that convention.
        hnsw_manager: Any,
        collection_path: Path,
        collection_name: str,
        session: Optional[HNSWSyncSession],
        prior: Optional[HNSWSyncState],
        progress_callback: Optional[Any],
        clear_stale: bool,
        layout_override: Optional[ChunkLayout],
        layout: ChunkLayout,
        reason: str,
        force_unfiltered: bool = False,
    ) -> Dict[str, Any]:
        """Bug #1575 Part C: full rebuild -- UNFILTERED when
        ``force_unfiltered`` (Bug #1407 Amendment 3 parity), else
        session-FILTERED -- then publish a clean epoch and discard the
        session.
        """
        if force_unfiltered:
            with self._id_index_lock:
                if collection_name in self._indexing_session_changes:
                    del self._indexing_session_changes[collection_name]
            count = hnsw_manager.rebuild_from_vectors(
                collection_path=collection_path,
                progress_callback=progress_callback,
                clear_stale=clear_stale,
                layout_override=layout_override,
                lock_already_held=True,
            )
            current_branch = None
        else:
            count = self._perform_full_filtered_rebuild(
                hnsw_manager,
                collection_path,
                session,
                progress_callback,
                clear_stale,
                layout_override,
            )
            current_branch = session.current_branch if session else None

        new_epoch = prior.mutation_epoch if prior is not None else 1
        self._publish_clean_hnsw_sync(
            collection_path, new_epoch, current_branch, layout
        )
        self._discard_hnsw_sync_session(collection_path)
        return {
            "vector_count": count,
            "action": _ACTION_FULL_REBUILD,
            "reason": reason,
        }

    def _try_reuse_clean_epoch(
        self,
        collection_path: Path,
        hnsw_manager: Any,
        session: Optional[HNSWSyncSession],
    ) -> Optional[Dict[str, Any]]:
        """Bug #1575 Part C: attempt byte-for-byte reuse for a clean epoch.
        Never propagates an unexpected exception from the validator --
        treated as "cannot prove reusable" (the uniform fail-safe contract).
        Returns None to signal the caller must fall back to a full rebuild.
        """
        expected_branch = session.current_branch if session else None
        expected_filtered = bool(session and session.branch_context_set)
        try:
            ok, why = hnsw_manager.validate_hnsw_artifact_for_reuse(
                collection_path,
                expected_branch=expected_branch,
                expected_filtered=expected_filtered,
            )
        except Exception as exc:
            ok, why = False, f"validation raised: {exc}"
        if not ok:
            self.logger.info("HNSW reuse rejected for %s: %s", collection_path, why)
            return None
        vector_count = self._read_published_hnsw_vector_count(collection_path)
        self._discard_hnsw_sync_session(collection_path)
        return {
            "vector_count": vector_count,
            "action": _ACTION_REUSED,
            "reason": "clean epoch, valid artifact",
        }

    def _try_incremental_dirty_epoch(
        self,
        collection_name: str,
        collection_path: Path,
        session: Optional[HNSWSyncSession],
        prior: HNSWSyncState,
        progress_callback: Optional[Any],
        clear_stale: bool,
        layout_override: Optional[ChunkLayout],
        layout: ChunkLayout,
        subdirectory: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Bug #1575 Part C: attempt the visibility-aware incremental
        update for a dirty epoch. Returns None (fall back to full rebuild)
        on any failure -- AC27b: never publish a partial index.
        """
        if session is None:
            return None  # unreachable in practice; fail safe, never crash
        try:
            incremental_result = self._apply_visibility_aware_incremental_update(
                collection_name=collection_name,
                collection_path=collection_path,
                session=session,
                progress_callback=progress_callback,
                clear_stale=clear_stale,
                layout_override=layout_override,
                subdirectory=subdirectory,
            )
        except Exception as exc:
            self.logger.info(
                "Visibility-aware incremental update raised for %s: %s",
                collection_name,
                exc,
            )
            return None
        if incremental_result is None:
            return None
        self._publish_clean_hnsw_sync(
            collection_path, prior.mutation_epoch, session.current_branch, layout
        )
        self._discard_hnsw_sync_session(collection_path)
        return {
            "vector_count": incremental_result["vectors"],
            "action": _ACTION_INCREMENTAL,
            "reason": "visibility-aware incremental update",
        }

    def _prepare_hnsw_sync_context(
        self, collection_name: str, subdirectory: Optional[str]
    ) -> Tuple[Path, Any, Any, ChunkLayout, Optional[ChunkLayout]]:
        """Bug #1575 Part C: resolve the layout/manager/rebuilder context
        shared by every branch of the decision engine.

        The two ``Any``-typed return slots are ``HNSWIndexManager`` and
        ``BackgroundIndexRebuilder`` instances -- typed ``Any`` here purely
        because both classes are constructed via LAZY imports (two lines
        below) to avoid an eager module-level import, not because a
        concrete type is unavailable.
        """
        collection_path = self._get_collection_path(collection_name, subdirectory)
        vector_size = self._get_vector_size(collection_name, subdirectory)
        is_chunks_db = self._is_chunks_db_collection(collection_name, collection_path)
        layout = ChunkLayout.CHUNKS_DB if is_chunks_db else ChunkLayout.SHARDED_JSON
        layout_override = layout if is_chunks_db else None

        from .hnsw_index_manager import HNSWIndexManager
        from .background_index_rebuilder import BackgroundIndexRebuilder

        hnsw_manager = HNSWIndexManager(
            vector_dim=vector_size, space="cosine", num_threads=self._hnsw_num_threads
        )
        rebuilder = BackgroundIndexRebuilder(collection_path)
        return collection_path, hnsw_manager, rebuilder, layout, layout_override

    def _attempt_reuse_or_incremental(
        self,
        hnsw_manager: Any,
        collection_path: Path,
        collection_name: str,
        session: Optional[HNSWSyncSession],
        prior: HNSWSyncState,
        progress_callback: Optional[Any],
        clear_stale: bool,
        layout_override: Optional[ChunkLayout],
        layout: ChunkLayout,
        subdirectory: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Bug #1575 Part C: tiny dispatcher -- a clean epoch tries reuse, a
        dirty epoch tries the visibility-aware incremental update. Returns
        None (caller falls back to a full rebuild) when the attempted path
        fails.
        """
        if prior.mutation_epoch == prior.published_epoch:
            return self._try_reuse_clean_epoch(collection_path, hnsw_manager, session)
        return self._try_incremental_dirty_epoch(
            collection_name,
            collection_path,
            session,
            prior,
            progress_callback,
            clear_stale,
            layout_override,
            layout,
            subdirectory,
        )

    def _resolve_and_publish_hnsw_sync(
        self,
        collection_name: str,
        subdirectory: Optional[str] = None,
        # progress_callback: Optional[Any] matches this file's existing
        # established convention for this exact parameter name (see the
        # pre-existing rebuild_hnsw_filtered/end_indexing signatures).
        progress_callback: Optional[Any] = None,
        force_full_rebuild: bool = False,
        clear_stale: bool = True,
    ) -> Dict[str, Any]:
        """Bug #1575 Part C -- the visibility-epoch decision engine. Called
        by both ``end_indexing()`` and ``rebuild_hnsw_filtered()``. Acquires
        ``.index_rebuild.lock`` ONCE for the whole decision (AC24/AC45).
        """
        collection_path, hnsw_manager, rebuilder, layout, layout_override = (
            self._prepare_hnsw_sync_context(collection_name, subdirectory)
        )

        with rebuilder.acquire_lock():
            prior = read_hnsw_sync_state(collection_path)
            session_key = self._hnsw_sync_session_key(collection_path)
            with self._hnsw_sync_sessions_lock:
                session = self._hnsw_sync_sessions.get(session_key)

            try:
                if force_full_rebuild:
                    return self._finish_full_rebuild(
                        hnsw_manager,
                        collection_path,
                        collection_name,
                        session,
                        prior,
                        progress_callback,
                        clear_stale,
                        layout_override,
                        layout,
                        "force_full_rebuild requested (unfiltered)",
                        force_unfiltered=True,
                    )

                reason = self._resolve_hnsw_rebuild_reason(prior, session)
                if reason is None:
                    assert prior is not None
                    result = self._attempt_reuse_or_incremental(
                        hnsw_manager,
                        collection_path,
                        collection_name,
                        session,
                        prior,
                        progress_callback,
                        clear_stale,
                        layout_override,
                        layout,
                        subdirectory,
                    )
                    if result is not None:
                        return result
                    reason = "reuse/incremental attempt failed"

                return self._finish_full_rebuild(
                    hnsw_manager,
                    collection_path,
                    collection_name,
                    session,
                    prior,
                    progress_callback,
                    clear_stale,
                    layout_override,
                    layout,
                    reason,
                )
            except Exception:
                # A failure anywhere in the decision/build/publish sequence
                # must not leave a stale in-memory session lingering for a
                # SUBSEQUENT begin_indexing() call in the SAME process (it
                # would otherwise reference sets from an abandoned
                # _indexing_session_changes generation). The durable
                # on-disk hnsw_sync state is unaffected -- already dirty
                # from the mutation -- so the next attempt safely forces a
                # full rebuild via the "no session tracked" fail-safe path.
                self._discard_hnsw_sync_session(collection_path)
                raise

    def rebuild_hnsw_filtered(
        self,
        collection_name: str,
        visible_files: Set[str],
        subdirectory: Optional[str] = None,
        progress_callback: Optional[Any] = None,
        current_branch: Optional[str] = None,
    ) -> int:
        """Bug #1575 Part C: thin compatibility wrapper. Registers branch
        context via ``set_hnsw_branch_context()`` then IMMEDIATELY resolves
        and publishes via the SAME decision engine ``end_indexing()`` uses
        -- required for standalone callers with NO surrounding
        ``begin_indexing()``/``end_indexing()`` bracket (e.g. the
        golden-repo post-CoW-branch belt-and-suspenders cleanup), which
        still expect this call to perform the decision synchronously and
        return the resulting vector count.

        The git-aware refresh path (``hide_files_not_in_branch_thread_safe``)
        no longer calls this method directly -- it calls
        ``set_hnsw_branch_context()`` and lets the SAME
        ``begin_indexing()``/``end_indexing()`` bracket's ``end_indexing()``
        call decide.
        """
        collection_path = self._get_collection_path(collection_name, subdirectory)

        self.set_hnsw_branch_context(
            collection_name, current_branch, visible_files, subdirectory=subdirectory
        )

        result = self._resolve_and_publish_hnsw_sync(
            collection_name,
            subdirectory=subdirectory,
            progress_callback=progress_callback,
        )
        count = int(result["vector_count"])

        from code_indexer.storage.shared.chunk_layout import resolve_chunk_layout

        # Story #1458 AC11: invalidate() must target the EXACT key search()'s
        # get_or_load() stored the entry under, freshly resolved (not an
        # in-session build-intent value, which can diverge from what
        # search() actually keys against).
        _invalidate_layout_token = resolve_chunk_layout(collection_path).value
        if self.hnsw_index_cache is not None:
            self.hnsw_index_cache.invalidate(
                self._activation_scoped_cache_key(
                    str(collection_path.resolve()),
                    chunk_layout_token=_invalidate_layout_token,
                )
            )
        if self.id_index_cache is not None:
            self.id_index_cache.invalidate(
                self._activation_scoped_cache_key(
                    str(collection_path.resolve()),
                    chunk_layout_token=_invalidate_layout_token,
                )
            )

        self.logger.info(
            f"HNSW filtered rebuild (compat wrapper) complete for "
            f"'{collection_name}': {count} vectors "
            f"({result['action']}: {result['reason']})"
        )

        return count

    def _get_vector_size(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> int:
        """Get vector size for collection (cached to avoid repeated file I/O).

        This method implements caching to eliminate the O(n²) behavior of reading
        collection_meta.json on every upsert operation. Before optimization, we were
        reading a 163KB JSON file 1,127+ times. Now we read it once and cache the result.

        Args:
            collection_name: Name of the collection
            subdirectory: Optional explicit subdirectory (e.g.
                "multimodal_index"). When provided, wins over the
                active-indexing-session fallback below -- required for a
                caller (e.g. ``scroll_points``) resolving a nested
                collection OUTSIDE an active indexing session, where
                ``_active_subdirectories`` is empty (Codex-16 Finding 4).
                When None, falls back to the active-indexing subdirectory
                recorded for this collection, byte-identical to every
                existing caller that omits this argument.

        Returns:
            Vector size (dimensions) for the collection

        Raises:
            RuntimeError: If collection metadata is corrupted, missing, or invalid

        Note:
            Thread-safe via _metadata_lock to prevent race conditions during concurrent
            indexing operations.
        """
        with self._metadata_lock:
            if subdirectory is None:
                subdirectory = self._active_subdirectories.get(collection_name)
            cache_key = self._id_cache_key(collection_name, subdirectory)
            if cache_key not in self._vector_size_cache:
                # Load metadata ONCE
                collection_path = self._get_collection_path(
                    collection_name, subdirectory
                )
                meta_file = collection_path / "collection_meta.json"

                if not meta_file.exists():
                    raise RuntimeError(
                        f"Collection metadata not found: {meta_file}. "
                        f"Collection may be corrupted or not properly initialized."
                    )

                try:
                    with open(meta_file) as f:
                        metadata = json.load(f)

                    vector_size = metadata.get("vector_size")
                    if vector_size is None:
                        raise RuntimeError(
                            f"Collection metadata missing 'vector_size' field: {meta_file}"
                        )

                    # Cache for future use
                    self._vector_size_cache[cache_key] = vector_size
                    self._collection_metadata_cache[collection_name] = metadata

                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Collection metadata file corrupted (invalid JSON): {meta_file}. "
                        f"Error: {e}. You may need to recreate the collection."
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to read collection metadata: {meta_file}. Error: {e}"
                    )

            return self._vector_size_cache[cache_key]

    def _load_quantization_range(self, collection_name: str) -> tuple[float, float]:
        """Load quantization range from collection metadata (cached).

        This method now uses the cached metadata from _get_vector_size() to avoid
        repeated file I/O during upsert operations.

        Args:
            collection_name: Name of the collection

        Returns:
            Tuple of (min_val, max_val) for quantization range
        """
        # Use cached metadata via _metadata_lock
        with self._metadata_lock:
            if collection_name in self._collection_metadata_cache:
                metadata = self._collection_metadata_cache[collection_name]
                quant_range = metadata.get(
                    "quantization_range", {"min": -2.0, "max": 2.0}
                )
                return (quant_range["min"], quant_range["max"])

        # Fallback: read from disk if not cached (shouldn't happen if using lifecycle properly)
        collection_path = self.base_path / collection_name
        metadata_path = collection_path / "collection_meta.json"

        if not metadata_path.exists():
            return (-2.0, 2.0)

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
                quant_range = metadata.get(
                    "quantization_range", {"min": -2.0, "max": 2.0}
                )
                return (quant_range["min"], quant_range["max"])
        except (json.JSONDecodeError, KeyError):
            return (-2.0, 2.0)

    def _get_temporal_metadata_store(self) -> TemporalMetadataStore:
        """Get or initialize temporal metadata store (lazy initialization).

        Returns:
            TemporalMetadataStore instance for temporal collection

        Story #669: Lazy-initialize metadata store for temporal collection only
        """
        with self._temporal_metadata_lock:
            if self._temporal_metadata_store is None:
                from code_indexer.services.temporal.temporal_collection_naming import (
                    LEGACY_TEMPORAL_COLLECTION,
                )

                temporal_collection_path = self.base_path / LEGACY_TEMPORAL_COLLECTION
                self._temporal_metadata_store = TemporalMetadataStore(
                    temporal_collection_path
                )
            return self._temporal_metadata_store

    def _upsert_points_chunks_db(
        self,
        collection_name: str,
        points: List[Dict[str, Any]],
        collection_path: Path,
        progress_callback: Optional[Any] = None,
        subdirectory: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Story #1456: CHUNKS_DB write path.

        Writes go into ``chunks.db`` via ONE ``ChunkStore.write_batch()``
        call (not per-point files) -- no quantization/hex-path directory
        sharding is needed since there is only one file per collection.
        Reuses the SAME record-preparation helpers (blob-hash/git lookups,
        ``_prepare_vector_data_batch``) as the legacy sharded-JSON path, so
        the stored record shape is byte-identical field-for-field.

        Orphan cleanup (Story #540 duplicate-prevention semantics) is
        preserved via the SAME ``PathIndex``, but eviction targets
        chunk-store rows (``ChunkStore.delete``) instead of unlinking files.

        Bug #1528: TEMPORAL collections are now a PRIMARY consumer of this
        path (they used to be excluded from CHUNKS_DB entirely). Because of
        that, this method owes them the same two side effects the legacy
        path performs and nothing else does: the batched temporal METADATA
        write (a separate store used by reconciliation, the incremental
        gate, and at-commit scoping) and the SKIP of the git
        blob-hash/uncommitted lookups. Any future write-path side effect
        added to one branch MUST be added to both.

        Args:
            subdirectory: Optional explicit subdirectory (e.g.
                "multimodal_index"), threaded from upsert_points' own
                already-resolved local ``subdirectory`` variable so this
                method's ``self._path_indexes`` access uses the SAME cache
                key as every other write-path site for the SAME physical
                collection. ``None`` (every existing caller) is
                byte-identical to the pre-fix bare-key behavior.
        """
        expected_dims = self._get_vector_size(collection_name, subdirectory)
        repo_root = self._get_repo_root()

        file_paths = [
            p.get("payload", {}).get("path", "")
            for p in points
            if p.get("payload", {}).get("path")
        ]
        blob_hashes: Dict[str, str] = {}
        uncommitted_files: set = set()
        # Bug #1528: skip the git blob-hash/uncommitted lookups for temporal
        # collections, exactly as the legacy path does (FIX 1 -- avoids Errno
        # 7 argument-list overflow and pointless git work on large temporal
        # indexes, whose payload paths are historical commit diffs).
        if (
            repo_root is not None
            and file_paths
            and not TemporalMetadataStore.is_temporal_collection(collection_name)
        ):
            blob_hashes = self._get_blob_hashes_batch(file_paths, repo_root)
            uncommitted_files = self._check_uncommitted_batch(file_paths, repo_root)

        # Story #540: pre-upsert orphan detection via PathIndex, identical
        # dedup semantics to the legacy path -- only the eviction mechanism
        # differs (chunk-store row delete instead of file unlink).
        from collections import defaultdict

        points_by_file: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for point in points:
            file_path = point.get("payload", {}).get("path", "")
            if file_path:
                points_by_file[file_path].append(point)

        # Keyed via _id_cache_key using the explicit subdirectory param so
        # this CHUNKS_DB write path never collides with a bare-name
        # top-level/nested collection pair (Codex NEW Finding 2).
        _upsert_cdb_path_cache_key = self._id_cache_key(collection_name, subdirectory)
        orphan_ids: List[str] = []
        with self._path_index_lock:
            if _upsert_cdb_path_cache_key not in self._path_indexes:
                self._path_indexes[_upsert_cdb_path_cache_key] = (
                    self._lazy_load_path_index_tracked(
                        collection_name, _upsert_cdb_path_cache_key
                    )
                )
            path_index = self._path_indexes[_upsert_cdb_path_cache_key]

            for file_path, file_points in points_by_file.items():
                new_point_ids = {p["id"] for p in file_points}
                old_point_ids = path_index.get_point_ids(file_path)
                orphan_point_ids = old_point_ids - new_point_ids

                for orphan_id in orphan_point_ids:
                    path_index.remove_point(file_path, orphan_id)
                    if not path_index.has_other_owner(orphan_id):
                        orphan_ids.append(orphan_id)

            for point in points:
                point_id = point["id"]
                file_path = point.get("payload", {}).get("path", "")
                if file_path:
                    path_index.add_point(file_path, point_id)

        records: List[Dict[str, Any]] = []
        for idx, point in enumerate(points, 1):
            point_id = point["id"]
            vector = np.array(point["vector"])
            payload = point.get("payload", {})
            chunk_text = point.get("chunk_text")
            file_path = payload.get("path", "")

            if vector.dtype == object or vector.dtype == np.dtype("O"):
                raise ValueError(
                    f"Point {point_id} has invalid vector with dtype={vector.dtype}. "
                    f"Vector contains non-numeric values."
                )
            if vector.shape[0] != expected_dims:
                raise ValueError(
                    f"Point {point_id} has vector dimension {vector.shape[0]}, "
                    f"expected {expected_dims}"
                )

            if progress_callback:
                file_path_for_callback = Path(file_path) if file_path else Path("")
                progress_callback(
                    idx, len(points), file_path_for_callback, info=file_path
                )

            record = self._prepare_vector_data_batch(
                point_id=point_id,
                vector=vector,
                payload=payload,
                chunk_text=chunk_text,
                repo_root=repo_root,
                blob_hashes=blob_hashes,
                uncommitted_files=uncommitted_files,
            )
            records.append(record)

            # HNSW-001 & HNSW-002: track for incremental updates. Unlike the
            # legacy path, added-vs-updated is not distinguished here (no
            # cheap "did this point_id already exist" check without a
            # per-point chunk-store read) -- both buckets feed the SAME
            # add_or_update_vector() call downstream, so this only affects
            # cosmetic added/updated counts in logs, never correctness.
            if collection_name in self._indexing_session_changes:
                self._indexing_session_changes[collection_name]["added"].add(point_id)

        from code_indexer.storage.sqlite_chunk_store import open_chunk_store_for_path

        chunk_store = open_chunk_store_for_path(
            collection_path / "chunks.db", str(collection_path)
        )
        try:
            if records:
                chunk_store.write_batch(records)
            if orphan_ids:
                chunk_store.delete(orphan_ids)
        finally:
            chunk_store.close()

        # Bug #1528: the temporal METADATA store is a SEPARATE store from the
        # chunk data (shared temporal_metadata.db in solo mode, PostgreSQL in
        # cluster mode) and is what reconcile_temporal_index, the incremental
        # gate and at-commit scoping read. Its batch write used to exist only
        # in the legacy sharded-JSON loop, so routing temporal through this
        # CHUNKS_DB path silently stopped populating it. Same batch API and
        # same flush-AFTER-success ordering as the legacy path (Bug #1206):
        # chunk rows are durable first, then ONE metadata transaction, so a
        # crash in between leaves chunks without metadata and the indexer
        # re-indexes those commits on resume (deterministic point ids ->
        # INSERT OR REPLACE, never duplicates).
        if TemporalMetadataStore.is_temporal_collection(collection_name) and points:
            metadata_store = self._get_temporal_metadata_store()
            metadata_store.save_metadata_batch(
                [(point["id"], point.get("payload", {})) for point in points]
            )
            metadata_store.checkpoint_wal()

        if orphan_ids and collection_name in self._indexing_session_changes:
            self._indexing_session_changes[collection_name]["deleted"].update(
                orphan_ids
            )

        # Bug #1575 round 6, item 4 (Codex claim, confirmed real by
        # investigation): this method returns directly from
        # upsert_points()'s early CHUNKS_DB dispatch, so it never reaches
        # the SHARDED_JSON-only Gap D persist further down in
        # upsert_points() itself. An out-of-session upsert here (e.g.
        # watch mode) mutates the live in-memory PathIndex for orphan/dedup
        # detection but, pre-fix, never persisted that update to
        # path_index.bin -- leaving the on-disk bin stale across a process
        # boundary exactly like the SHARDED_JSON/delete-side gaps this
        # story already closed. Gated via
        # _persist_out_of_session_path_index() (same provenance discipline
        # as Gap D/B): never blindly persists an unproven/partial picture.
        if points and collection_name not in self._indexing_session_changes:
            self._persist_out_of_session_path_index(
                collection_name,
                _upsert_cdb_path_cache_key,
                subdirectory,
            )

        return {"status": "ok", "count": len(points)}

    def upsert_points(
        self,
        collection_name: Optional[str],
        points: List[Dict[str, Any]],
        progress_callback: Optional[Any] = None,
        watch_mode: bool = False,
        subdirectory: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store vectors in filesystem with git-aware optimization.

        Args:
            collection_name: Name of the collection (if None, auto-resolves to only collection)
            points: List of point dictionaries with id, vector, payload
            progress_callback: Optional callback(current, total, Path, info) for progress reporting
            watch_mode: If True, triggers immediate real-time HNSW updates (HNSW-001)
            subdirectory: Optional subdirectory path (e.g., "multimodal_index")

        Returns:
            Status dictionary with operation result

        Raises:
            ValueError: If collection_name is None and multiple collections exist

        Note:
            HNSW-001 (Watch Mode): When watch_mode=True, updates HNSW index immediately
            after upserting points, enabling real-time semantic search without delays.

            HNSW-002 (Batch Mode): When watch_mode=False and session changes are tracked,
            changes are accumulated for batch incremental update at end_indexing().
        """
        # Auto-resolve collection_name if None
        if collection_name is None:
            available_collections = self.list_collections()
            if len(available_collections) == 0:
                raise ValueError("No collections available. Create a collection first.")
            elif len(available_collections) == 1:
                collection_name = available_collections[0]
            else:
                raise ValueError(
                    f"collection_name is required when multiple collections exist. "
                    f"Available collections: {', '.join(available_collections)}"
                )

        # Use subdirectory from active tracking if not provided
        if subdirectory is None:
            subdirectory = self._active_subdirectories.get(collection_name)

        collection_path = self._get_collection_path(collection_name, subdirectory)

        if not self.collection_exists(collection_name, subdirectory):
            raise ValueError(f"Collection '{collection_name}' does not exist")

        # Bug #1575 Part C: dirty-before-write -- durably mark the hnsw_sync
        # epoch dirty BEFORE any of this call's storage mutations happen.
        self._mark_hnsw_dirty_before_mutation(collection_path, collection_name)

        # Story #1456: CHUNKS_DB collections take a completely separate,
        # simpler write path (no quantization/hex-path directory sharding
        # needed -- there is only one file per collection). Dispatched here,
        # before the projection-matrix load the legacy path needs but
        # CHUNKS_DB does not.
        if self._is_chunks_db_collection(collection_name, collection_path):
            return self._upsert_points_chunks_db(
                collection_name,
                points,
                collection_path,
                progress_callback,
                subdirectory=subdirectory,
            )

        # Load projection matrix (singleton-cached in ProjectionMatrixManager)
        try:
            projection_matrix = self.matrix_manager.load_matrix(collection_path)
        except FileNotFoundError:
            # Bug #1264: the shard-prep self-heal in temporal_indexer.py (Bug
            # #1242) only covers shards it enumerates ahead of the write, in a
            # different module from this call. Any other path that reaches
            # upsert_points() for a collection whose projection_matrix.npy is
            # missing on disk still hard-crashed here. Self-heal AT the write
            # chokepoint itself instead: reuse the exact copy/regenerate logic
            # from Bug #1242 (no duplicated matrix-creation logic), then retry
            # the load once. A genuine failure to heal still propagates loudly.
            vector_size = self._get_vector_size(collection_name, subdirectory)
            source_collection_path: Optional[Path] = None
            if TemporalMetadataStore.is_temporal_collection(collection_name):
                from code_indexer.services.temporal.temporal_collection_naming import (
                    base_collection_name,
                )

                base_name = base_collection_name(collection_name)
                if base_name != collection_name:
                    base_path = self._get_collection_path(base_name, subdirectory)
                    if base_path.exists():
                        source_collection_path = base_path

            self.logger.warning(
                "Bug #1264: projection_matrix.npy missing for collection '%s' "
                "at write time -- self-healing (source=%s)",
                collection_name,
                source_collection_path,
            )

            from code_indexer.services.temporal.temporal_projection_matrix import (
                _ensure_shard_has_projection_matrix,
            )

            _ensure_shard_has_projection_matrix(
                collection_path, source_collection_path, vector_size
            )
            self.matrix_manager._matrix_cache.pop(str(collection_path.absolute()), None)
            projection_matrix = self.matrix_manager.load_matrix(collection_path)

        # Get expected vector dimensions from projection matrix
        expected_dims = projection_matrix.shape[0]

        # Load quantization range for locality-preserving quantization
        min_val, max_val = self._load_quantization_range(collection_name)

        # Detect git repo root once for batch operation
        repo_root = self._get_repo_root()

        # Batch git operations for performance
        file_paths = [
            p.get("payload", {}).get("path", "")
            for p in points
            if p.get("payload", {}).get("path")
        ]
        blob_hashes = {}
        uncommitted_files = set()

        # Skip blob hash lookup for temporal collection (FIX 1: Avoid Errno 7 on large temporal indexes)
        from code_indexer.services.temporal.temporal_collection_naming import (
            is_temporal_collection as _is_temporal_collection,
        )

        if (
            repo_root is not None
            and file_paths
            and not _is_temporal_collection(collection_name)
        ):
            blob_hashes = self._get_blob_hashes_batch(file_paths, repo_root)
            uncommitted_files = self._check_uncommitted_batch(file_paths, repo_root)

        # Resolved ONCE for this call: the SAME cache key used for every
        # self._id_index / self._path_indexes access below, so a nested
        # upsert_points() call never reads/writes a bare-name top-level
        # collection's cache entry (Codex NEW Finding 1/2).
        _upsert_cache_key = self._id_cache_key(collection_name, subdirectory)

        # Ensure ID index exists for this collection (also loads file path cache)
        with self._id_index_lock:
            if _upsert_cache_key not in self._id_index:
                self._id_index[_upsert_cache_key] = self._load_id_index(
                    collection_name, subdirectory
                )
            # Ensure file path cache exists (in case ID index was manually populated)
            if collection_name not in self._file_path_cache:
                self._file_path_cache[collection_name] = set()

        # Story #540: Pre-upsert cleanup to prevent duplicates
        # Group points by file_path and clean up old vectors before upserting new ones
        from collections import defaultdict

        points_by_file = defaultdict(list)
        for point in points:
            file_path = point.get("payload", {}).get("path", "")
            if file_path:
                points_by_file[file_path].append(point)

        # CRITICAL FIX (Story #540 Code Review): Refactor to minimize lock hold time
        # STEP 1: Gather orphan metadata INSIDE lock (fast, no I/O)
        orphans_to_delete = []  # List of (file_path, orphan_id, vector_file_path)

        with self._path_index_lock:
            # CRITICAL FIX (Story #540 Code Review): Lazy-load path index if not already loaded
            # This handles watch mode scenario where upsert_points can be called WITHOUT begin_indexing()
            if _upsert_cache_key not in self._path_indexes:
                self._path_indexes[_upsert_cache_key] = (
                    self._lazy_load_path_index_tracked(
                        collection_name, _upsert_cache_key
                    )
                )

            path_index = self._path_indexes[_upsert_cache_key]

            # Gather orphan point_ids for each file
            for file_path, file_points in points_by_file.items():
                # Get new point_ids that will be upserted
                new_point_ids = {p["id"] for p in file_points}

                # Get old point_ids from path index
                old_point_ids = path_index.get_point_ids(file_path)

                # Identify orphaned point_ids (in old but not in new)
                orphan_point_ids = old_point_ids - new_point_ids

                # Bug #663: Remove file_path's orphaned points from path index
                # first — this file no longer owns these points regardless of what
                # happens next. Then only schedule deletion for truly isolated orphans
                # (no other file in the path index still references the same point_id).
                if orphan_point_ids:
                    for orphan_id in orphan_point_ids:
                        path_index.remove_point(file_path, orphan_id)

                    with self._id_index_lock:
                        for orphan_id in orphan_point_ids:
                            # Skip if another file still holds this point_id.
                            # path_index.remove_point was already called above for
                            # file_path, so has_other_owner only finds other files.
                            # _path_index_lock is held by the outer context.
                            if path_index.has_other_owner(orphan_id):
                                continue

                            if orphan_id in self._id_index.get(_upsert_cache_key, {}):
                                vector_file = self._id_index[_upsert_cache_key][
                                    orphan_id
                                ]
                                orphans_to_delete.append(
                                    (file_path, orphan_id, vector_file)
                                )

        # STEP 2: Perform file deletions OUTSIDE lock (I/O operations)
        # This releases both _path_index_lock and _id_index_lock before I/O
        for file_path, orphan_id, vector_file in orphans_to_delete:
            # Delete vector JSON file from disk
            # Use try/except to handle race condition: another thread may delete same file
            try:
                if vector_file.exists():
                    vector_file.unlink()
            except FileNotFoundError:
                # File already deleted by another thread - this is safe to ignore
                pass

        # STEP 3: Update _id_index INSIDE lock (fast, just dict updates)
        # path_index updates were already done in STEP 1 (Bug #663 fix).
        if orphans_to_delete:
            with self._id_index_lock:
                for file_path, orphan_id, vector_file in orphans_to_delete:
                    # Bug #663 defense-in-depth: a concurrent thread may have
                    # re-written this point_id between STEP 2 (file delete) and
                    # STEP 3. Only evict if the stored path still matches the
                    # vector_file captured in STEP 1.
                    current_path = self._id_index.get(_upsert_cache_key, {}).get(
                        orphan_id
                    )
                    if current_path != vector_file:
                        # Concurrent thread re-populated — do not evict
                        continue

                    del self._id_index[_upsert_cache_key][orphan_id]

                    # Track deletion for HNSW incremental updates
                    if collection_name in self._indexing_session_changes:
                        self._indexing_session_changes[collection_name]["deleted"].add(
                            orphan_id
                        )

        # Bug #1206 Fix 1: For temporal collections, accumulate (point_id, payload) rows
        # so we can call save_metadata_batch ONCE per upsert_points call instead of
        # once per vector (which caused N connect/commit/fsync cycles under 8 threads).
        # hash_prefix is deterministic (sha256(point_id)[:16]), so we compute it upfront
        # and use it for filenames without touching the DB in the per-vector loop.
        is_temporal = TemporalMetadataStore.is_temporal_collection(collection_name)
        temporal_batch_rows: List[
            tuple
        ] = []  # accumulates (point_id, payload) for batch

        # Process all points
        total_points = len(points)
        for idx, point in enumerate(points, 1):
            try:
                point_id = point["id"]
                vector = np.array(point["vector"])
                payload = point.get("payload", {})
                chunk_text = point.get("chunk_text")  # Extract chunk_text from root
                file_path = payload.get("path", "")

                # LAYER 2 VALIDATION: Validate vector is numeric, not object array
                if vector.dtype == object or vector.dtype == np.dtype("O"):
                    raise ValueError(
                        f"Point {point_id} has invalid vector with dtype={vector.dtype}. "
                        f"Vector contains non-numeric values. First 5 values: {point['vector'][:5]}"
                    )

                # Validate vector dimension matches expected
                if vector.shape[0] != expected_dims:
                    raise ValueError(
                        f"Point {point_id} has vector dimension {vector.shape[0]}, expected {expected_dims}"
                    )

                # Progress reporting
                if progress_callback:
                    # Pass empty Path("") instead of None to avoid path division errors
                    file_path_for_callback = Path(file_path) if file_path else Path("")
                    progress_callback(
                        idx, total_points, file_path_for_callback, info=file_path
                    )

                # Quantize vector to hex path
                if projection_matrix is None:
                    raise RuntimeError(
                        f"Projection matrix is None for collection {collection_name}"
                    )

                # Matrix multiplication (matrix is singleton-cached in ProjectionMatrixManager)
                reduced = vector @ projection_matrix

                # Use fixed-range scalar quantization for locality preservation
                quantized_bits = self.quantizer._quantize_to_2bit(
                    reduced, min_val, max_val
                )
                hex_path = self.quantizer._bits_to_hex(quantized_bits)
            except Exception as e:
                import traceback

                print(f"ERROR in upsert_points loop iteration {idx}: {e}")
                print("Traceback:")
                traceback.print_exc()
                raise

            # Split hex path into directory structure
            segments = self.quantizer._split_hex_path(hex_path)

            # Create directory structure
            dir_path = collection_path
            for segment in segments[:-1]:
                dir_path = dir_path / segment
            _dir_path_is_new = is_temporal and not dir_path.exists()
            dir_path.mkdir(parents=True, exist_ok=True)
            if _dir_path_is_new:
                # Bug #1407 Foundation: fsync a vector file's freshly-created
                # parent directory on create (temporal only -- scoped to
                # avoid a fleet-wide perf regression on the general indexing
                # path, which is not the correctness gap this closes).
                _new_dir_fd = os.open(str(dir_path), os.O_RDONLY)
                try:
                    nfs_safe_fsync(_new_dir_fd)
                finally:
                    os.close(_new_dir_fd)

            # Story #669: Use hash-based filenames for temporal collections (v2 format)
            # This prevents OSError when point_ids exceed 255 characters.
            # Bug #1206 Fix 1: compute hash_prefix from the deterministic formula
            # WITHOUT calling save_metadata per vector.  The DB batch write happens
            # AFTER all vector files are written (save_metadata_batch call below).
            if is_temporal:
                # V2 format: hash-based filename (28 chars total).
                # hash_prefix is sha256(point_id)[:16] — same as generate_hash_prefix().
                metadata_store = self._get_temporal_metadata_store()
                hash_prefix = metadata_store.generate_hash_prefix(point_id)
                vector_file = dir_path / f"vector_{hash_prefix}.json"
                temporal_batch_rows.append((point_id, payload))
            else:
                # Original format: point_id with slashes replaced (non-temporal collections)
                vector_file = dir_path / f"vector_{point_id.replace('/', '_')}.json"

            # Prepare vector data with git-aware storage (using batch results)
            vector_data = self._prepare_vector_data_batch(
                point_id=point_id,
                vector=vector,
                payload=payload,
                chunk_text=chunk_text,
                repo_root=repo_root,
                blob_hashes=blob_hashes,
                uncommitted_files=uncommitted_files,
            )

            # Atomic write to filesystem. Bug #1407/#1223: fsync temporal
            # vector JSON writes for crash-durability; non-temporal writes
            # stay fsync=False (unchanged, avoids a fleet-wide perf hit).
            self._atomic_write_json(vector_file, vector_data, fsync=is_temporal)

            # Update ID index and file path cache
            with self._id_index_lock:
                # Check if point existed before (for change tracking)
                point_existed = point_id in self._id_index.get(_upsert_cache_key, {})

                # Bug #1579: capture the point's PREVIOUS on-disk vector_file
                # path BEFORE it gets overwritten below. The directory a
                # point_id lands in is derived from its VECTOR (quantized
                # projection), while the filename is derived from the
                # point_id itself -- so a re-upsert of the same point_id with
                # a marginally different vector can quantize to a DIFFERENT
                # directory, leaving the OLD file behind as a "shifted
                # duplicate" sharing the same point_id. This is distinct from
                # the orphan-cleanup mechanism above (STEP 1-3): that only
                # fires when a point_id vanishes entirely from a file's chunk
                # set (in old_point_ids but not new_point_ids) -- a
                # persisting-but-relocated point_id is never in
                # orphan_point_ids since it stays in new_point_ids.
                previous_vector_file = self._id_index.get(_upsert_cache_key, {}).get(
                    point_id
                )

                self._id_index[_upsert_cache_key][point_id] = vector_file

                # Update file path cache.
                # Use setdefault because delete_points may have evicted the cache
                # entry between the initialization at begin_indexing() and this
                # point, causing a KeyError under concurrent upsert+delete.
                if file_path:
                    self._file_path_cache.setdefault(collection_name, set()).add(
                        file_path
                    )

                # HNSW-001 & HNSW-002: Track changes for incremental updates
                if collection_name in self._indexing_session_changes:
                    if point_existed:
                        self._indexing_session_changes[collection_name]["updated"].add(
                            point_id
                        )
                    else:
                        self._indexing_session_changes[collection_name]["added"].add(
                            point_id
                        )

            # Bug #1579: delete the stale prior-location file for a relocated
            # point_id OUTSIDE the id_index lock (I/O should not happen while
            # holding _id_index_lock, matching this file's lock-minimizing
            # convention used above for orphan-cleanup deletions).
            if previous_vector_file is not None and previous_vector_file != vector_file:
                try:
                    if previous_vector_file.exists():
                        previous_vector_file.unlink()
                except FileNotFoundError:
                    # File already deleted by another thread - safe to ignore
                    pass

            # Story #540: Update path index with new point_id
            with self._path_index_lock:
                if _upsert_cache_key in self._path_indexes and file_path:
                    self._path_indexes[_upsert_cache_key].add_point(file_path, point_id)

        # Bug #1206 Fix 1: flush the accumulated temporal metadata batch in ONE
        # transaction after all vector files have been written.  This is the
        # flush-after-success ordering: vectors on disk first, then metadata DB
        # commit, so a crash between writes leaves vectors without metadata —
        # the indexer re-indexes on resume (deterministic point_id → hash_prefix
        # means the re-index OVERWRITEs, no duplicates).
        if is_temporal and temporal_batch_rows:
            metadata_store = self._get_temporal_metadata_store()
            metadata_store.save_metadata_batch(temporal_batch_rows)
            metadata_store.checkpoint_wal()

        # HNSW-001: Watch mode real-time HNSW update
        if watch_mode:
            # In watch mode, update HNSW immediately for all upserted points
            # Note: Watch mode can be called outside of indexing sessions,
            # so we don't rely on _indexing_session_changes tracking
            if points:
                self._update_hnsw_incrementally_realtime(
                    collection_name=collection_name,
                    changed_points=points,
                    progress_callback=progress_callback,
                )

        # Codex review Finding 1 (Bug #1575 Part B): a point written here may
        # be invisible to an already-open scroll session's cached SHARDED_JSON
        # id_to_file enumeration -- evict it so the NEXT continuation page
        # observes this write, instead of silently freezing a page-1 snapshot.
        self._invalidate_scroll_sharded_json_cache(collection_name, subdirectory)

        # Bug #1575 Gap D: mirrors delete_points()'s Round 3 Fix B for the
        # insertion side. An upsert with NO active indexing session for
        # this collection (e.g. watch mode, or any other out-of-session
        # upsert_points() call) is never followed by an end_indexing()
        # call that would otherwise persist this update to
        # path_index.bin -- leaving the on-disk bin stale across a process
        # boundary, exactly like the delete-side gap. Persist immediately
        # so a LATER session's begin_indexing() loads an accurate picture
        # for Part B's (Story #540) cross-session duplicate-prevention
        # (_calculate_and_save_unique_file_count() no longer consults this
        # cache at all -- that fast-path trust was abandoned entirely).
        # Called OUTSIDE _path_index_lock: _save_path_index() nests
        # _id_index_lock for this (SHARDED_JSON) layout, and the B1
        # lock-order invariant requires this method to never hold both
        # locks simultaneously.
        #
        # Round 6 (opus/Codex CRITICAL finding): the live in-memory
        # PathIndex here is only safe to persist AS-IS when it was proven
        # complete (loaded from an existing bin, or previously repaired) --
        # _persist_out_of_session_path_index() gates on
        # self._path_index_loaded_from_file and forces an authoritative
        # rebuild-and-repair otherwise, never blindly writing an
        # unproven/partial picture that could undercount the collection.
        if points and collection_name not in self._indexing_session_changes:
            self._persist_out_of_session_path_index(
                collection_name,
                _upsert_cache_key,
                subdirectory,
            )

        # Return success - index rebuilding now happens in end_indexing() (O(n) not O(n²))
        # This fixes the performance disaster where we rebuilt indexes after EVERY file.
        # Now indexes are rebuilt ONCE at the end of the indexing session.
        return {"status": "ok", "count": len(points)}

    def count_points(self, collection_name: str) -> int:
        """Count vectors in collection using metadata (fast path) or ID index (fallback).

        Performance optimization: Reads vector_count from collection_meta.json
        instead of loading the full ID index (400K entries). This reduces
        cidx status time from 9+ seconds to <50ms for large collections.

        Args:
            collection_name: Name of the collection

        Returns:
            Number of vectors in collection
        """
        # Fast path: Try reading count from metadata
        collection_path = self.base_path / collection_name
        meta_file = collection_path / "collection_meta.json"

        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    metadata = json.load(f)

                # Check if hnsw_index exists with vector_count
                if "hnsw_index" in metadata:
                    vector_count = metadata["hnsw_index"].get("vector_count")
                    if isinstance(vector_count, int):
                        return vector_count
            except (json.JSONDecodeError, KeyError, OSError):
                # If metadata read fails, fall through to ID index path
                pass

        # Story #1456 AC3/AC7: CHUNKS_DB fallback -- COUNT(*) on chunks.db,
        # never the retired id_index.bin.
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                return int(chunk_store.count())
            finally:
                chunk_store.close()

        # Fallback path: Load ID index (original behavior). Resolved via
        # _active_subdirectories (this method has no subdirectory param) so
        # a nested collection read during an active indexing session hits
        # its OWN cache entry, never a bare-name top-level collision.
        _count_points_subdirectory = self._active_subdirectories.get(collection_name)
        _count_points_cache_key = self._id_cache_key(
            collection_name, _count_points_subdirectory
        )
        with self._id_index_lock:
            if _count_points_cache_key not in self._id_index:
                self._id_index[_count_points_cache_key] = self._load_id_index(
                    collection_name, _count_points_subdirectory
                )
            return len(self._id_index[_count_points_cache_key])

    def delete_points(
        self, collection_name: str, point_ids: List[str]
    ) -> Dict[str, Any]:
        """Delete vectors from filesystem.

        Args:
            collection_name: Name of the collection
            point_ids: List of point IDs to delete

        Returns:
            Status dictionary with deletion result

        Note:
            HNSW-001 & HNSW-002: Tracks deletions for incremental HNSW updates.
        """
        # Story #1456 AC7: CHUNKS_DB collections delete via the chunk store
        # directly -- id_index.bin is never read or written for this method.
        # Uses the combined _is_chunks_db_collection authority (not the bare
        # resolver) so this is correct even mid-build, before end_indexing()
        # commits the discriminator.
        collection_path = self._get_collection_path(collection_name)

        # Bug #1575 Part C: dirty-before-write -- durably mark the hnsw_sync
        # epoch dirty BEFORE any of this call's storage mutations happen.
        self._mark_hnsw_dirty_before_mutation(collection_path, collection_name)

        # Resolved via _active_subdirectories (this method has no
        # subdirectory param) so a nested collection's delete targets its
        # OWN _id_index/_path_indexes cache entry, never a bare-name
        # top-level collision. Computed ONCE, shared by both the CHUNKS_DB
        # and SHARDED_JSON branches below.
        _delete_points_subdirectory = self._active_subdirectories.get(collection_name)
        _delete_points_cache_key = self._id_cache_key(
            collection_name, _delete_points_subdirectory
        )

        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            path_idx = None
            try:
                # Bug #1575 Finding-1-regression fix: a point's path is
                # unrecoverable once its row is deleted, so it MUST be
                # resolved BEFORE calling delete() -- never after -- to
                # keep the live in-memory PathIndex in sync (mirroring
                # what the SHARDED_JSON branch below already does).
                point_paths = chunk_store.get_paths_for_points(point_ids)
                # Bug #1575 Part A Round 3, Fix C: hold _path_index_lock
                # across BOTH the SQLite delete commit and the in-memory
                # PathIndex removal so a concurrent upsert_points() call
                # for the SAME collection (which holds this SAME lock
                # while mutating self._path_indexes -- see
                # _upsert_points_chunks_db) can never interleave between
                # the DB delete committing and the cache reflecting it.
                # Reproduced without this: the DB and the live PathIndex
                # briefly disagreeing on the same point_id.
                with self._path_index_lock:
                    deleted_count = chunk_store.delete(point_ids)
                    if point_paths:
                        if _delete_points_cache_key not in self._path_indexes:
                            self._path_indexes[_delete_points_cache_key] = (
                                self._lazy_load_path_index_tracked(
                                    collection_name, _delete_points_cache_key
                                )
                            )
                        path_idx = self._path_indexes[_delete_points_cache_key]
                        for point_id, file_path in point_paths.items():
                            path_idx.remove_point(file_path, point_id)
            finally:
                chunk_store.close()

            if deleted_count > 0 and collection_name in self._indexing_session_changes:
                self._indexing_session_changes[collection_name]["deleted"].update(
                    point_ids
                )

            # Bug #1575 Part A Round 3, Fix B: a delete with NO active
            # indexing session for this collection is never followed by an
            # end_indexing() call that would otherwise persist this update
            # to path_index.bin (e.g. smart_indexer.py's reconcile path and
            # watch-mode deletion-only batch handling, both of which call
            # delete_file_branch_aware() -> delete_by_filter() ->
            # delete_points() and return BEFORE begin_indexing() is ever
            # called). Persist immediately so the on-disk bin never goes
            # stale across a process boundary. Called OUTSIDE
            # _path_index_lock: _save_path_index() nests _id_index_lock for
            # the SHARDED_JSON layout, and the B1 lock-order invariant
            # above requires delete_points() to never hold both locks
            # simultaneously.
            #
            # Round 6: gated via _persist_out_of_session_path_index() --
            # see the identical rationale at Gap D's upsert-side persist.
            if (
                path_idx is not None
                and collection_name not in self._indexing_session_changes
            ):
                self._persist_out_of_session_path_index(
                    collection_name,
                    _delete_points_cache_key,
                    _delete_points_subdirectory,
                )

            return {"status": "ok", "deleted": deleted_count}

        deleted = 0
        # Collect (file_path, point_id) pairs for path-index removal.
        # Applied AFTER releasing _id_index_lock to avoid nesting
        # _path_index_lock inside _id_index_lock (ABBA deadlock risk — B1).
        path_index_removals = []

        with self._id_index_lock:
            if _delete_points_cache_key not in self._id_index:
                self._id_index[_delete_points_cache_key] = self._load_id_index(
                    collection_name, _delete_points_subdirectory
                )

            index = self._id_index[_delete_points_cache_key]

            for point_id in point_ids:
                if point_id in index:
                    vector_file = index[point_id]

                    # Story #540: Get file_path from vector data before deletion
                    file_path = None
                    if vector_file.exists():
                        try:
                            with open(vector_file) as f:
                                vector_data = json.load(f)
                                file_path = vector_data.get("payload", {}).get("path")
                        except (json.JSONDecodeError, KeyError, OSError) as exc:
                            self.logger.debug(
                                "Could not read vector file during delete "
                                "(path-index entry may not be cleaned up): %s — %s",
                                vector_file,
                                exc,
                            )

                        # Delete file
                        vector_file.unlink()
                        deleted += 1

                    # Remove from index
                    del index[point_id]

                    # HNSW-001 & HNSW-002: Track deletion for incremental updates
                    if collection_name in self._indexing_session_changes:
                        self._indexing_session_changes[collection_name]["deleted"].add(
                            point_id
                        )

                    # Queue path-index removal — applied outside this lock block
                    # to avoid nesting _path_index_lock inside _id_index_lock.
                    if file_path:
                        path_index_removals.append((file_path, point_id))

            # Clear file path cache since file structure changed
            if deleted > 0 and collection_name in self._file_path_cache:
                del self._file_path_cache[collection_name]

        # Apply path-index removals AFTER releasing _id_index_lock.
        # This eliminates the nested lock acquisition that caused the ABBA deadlock.
        if path_index_removals:
            with self._path_index_lock:
                # Bug #1575 Finding-1-regression fix: a bare "is the cache
                # key already present" check silently SKIPPED the update
                # when delete_points() is called with no prior lazy
                # population (e.g. delete_by_filter()'s real call pattern,
                # which never calls begin_indexing()/upsert_points() first)
                # -- leaving the stale on-disk path_index.bin to resurface
                # the deleted file on the next session. Load it first
                # instead, mirroring the SAME lazy-population idiom
                # upsert_points()/begin_indexing() already use.
                if _delete_points_cache_key not in self._path_indexes:
                    self._path_indexes[_delete_points_cache_key] = (
                        self._lazy_load_path_index_tracked(
                            collection_name, _delete_points_cache_key
                        )
                    )
                path_idx = self._path_indexes[_delete_points_cache_key]
                for file_path, point_id in path_index_removals:
                    path_idx.remove_point(file_path, point_id)

            # Bug #1575 Part A Round 3, Fix B: persist immediately when
            # there is no active indexing session for this collection --
            # see the CHUNKS_DB branch above for the full rationale. Called
            # OUTSIDE _path_index_lock (already released by this point) to
            # respect the B1 lock-order invariant: _save_path_index() nests
            # _id_index_lock for this (SHARDED_JSON) layout, and
            # delete_points() must never hold both locks simultaneously.
            #
            # Round 6: gated via _persist_out_of_session_path_index() --
            # see the identical rationale at Gap D's upsert-side persist.
            if collection_name not in self._indexing_session_changes:
                self._persist_out_of_session_path_index(
                    collection_name,
                    _delete_points_cache_key,
                    _delete_points_subdirectory,
                )

        return {"status": "ok", "deleted": deleted}

    def _prepare_vector_data(
        self,
        point_id: str,
        vector: np.ndarray,
        payload: Dict[str, Any],
        repo_root: Optional[Path],
    ) -> Dict[str, Any]:
        """Prepare vector data with git-aware storage logic.

        Args:
            point_id: Unique point identifier
            vector: Vector data
            payload: Point payload
            repo_root: Git repository root (None if not a git repo)

        Returns:
            Dictionary ready for JSON serialization
        """
        data = {
            "id": point_id,
            "vector": vector.tolist(),
            "file_path": payload.get("path", ""),
            "start_line": payload.get("start_line", 0),
            "end_line": payload.get("end_line", 0),
            "metadata": {
                "language": payload.get("language", ""),
                "type": payload.get("type", "content"),
            },
        }

        file_path = payload.get("path", "")

        # Git-aware chunk storage logic
        if repo_root:
            # Check if this specific file has uncommitted changes
            has_uncommitted = self._file_has_uncommitted_changes(file_path, repo_root)

            if not has_uncommitted:
                # File is clean: try to get blob hash
                blob_hash = self._get_git_blob_hash(file_path, repo_root)
                if blob_hash:
                    # Store only blob hash (space efficient)
                    data["git_blob_hash"] = blob_hash
                    data["indexed_with_uncommitted_changes"] = False
                else:
                    # File not in git (untracked): store chunk text
                    data["chunk_text"] = payload.get("content", "")
                    data["indexed_with_uncommitted_changes"] = True
            else:
                # File has uncommitted changes: store chunk text
                data["chunk_text"] = payload.get("content", "")
                data["indexed_with_uncommitted_changes"] = True
        else:
            # Non-git repo: always store chunk_text
            data["chunk_text"] = payload.get("content", "")

        return data

    def _get_repo_root(self) -> Optional[Path]:
        """Get git repository root directory (memoized).

        The git repo root is invariant for the lifetime of this instance.
        Both positive (Path) and negative (None) results are cached after the
        first call so the subprocess runs at most once per instance.

        Returns:
            Path to git repo root, or None if not a git repo
        """
        with self._repo_root_lock:
            if self._repo_root_cached:
                return self._cached_repo_root
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=self.project_root,  # Use project_root instead of base_path
                    capture_output=True,
                    text=True,
                    timeout=GIT_TIMEOUT_SECONDS,
                )
                if result.returncode == 0:
                    self._cached_repo_root = Path(result.stdout.strip())
                else:
                    self._cached_repo_root = None
            except (subprocess.TimeoutExpired, FileNotFoundError):
                self._cached_repo_root = None
            self._repo_root_cached = True
            return self._cached_repo_root

    def _file_has_uncommitted_changes(self, file_path: str, repo_root: Path) -> bool:
        """Check if a specific file has uncommitted changes.

        Args:
            file_path: Relative path to file from repo root
            repo_root: Git repository root

        Returns:
            True if file has uncommitted changes, False otherwise
        """
        try:
            # Check git status for this specific file
            result = subprocess.run(
                ["git", "status", "--porcelain", file_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5,
            )

            # If output is non-empty, file has uncommitted changes
            return len(result.stdout.strip()) > 0

        except (subprocess.TimeoutExpired, FileNotFoundError):
            # If git command fails, assume file has changes (safe fallback)
            return True

    def _get_git_blob_hash(self, file_path: str, repo_root: Path) -> Optional[str]:
        """Get git blob hash for a file.

        Args:
            file_path: Relative path to file
            repo_root: Git repository root

        Returns:
            Git blob hash or None if not found
        """
        try:
            result = subprocess.run(
                ["git", "ls-tree", "HEAD", file_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0 and result.stdout:
                # Parse output: "mode type hash\tfilename"
                parts = result.stdout.split()
                if len(parts) >= 3:
                    return parts[2]  # Return blob hash

            return None

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _atomic_write_json(
        self, file_path: Path, data: Dict[str, Any], fsync: bool = False
    ) -> None:
        """Atomically write JSON data to file.

        Uses write-to-temp-then-rename pattern for atomicity.  Each file write
        is independent: the OS-level atomic rename guarantees that a reader
        sees either the old file or the new file, never a partial write.  No
        process-wide lock is needed — concurrent writes to DISTINCT files
        proceed in parallel (Bug #1206 Fix 3).

        On any exception the ``.tmp`` file is cleaned up so no orphans
        accumulate (Bug #1223 Defect A).

        Args:
            file_path: Target file path
            data: Data to serialize as JSON
            fsync: If True, call ``f.flush()`` + ``os.fsync()`` before the
                rename to ensure the data is durable on disk before the old
                file is replaced.  Use True for critical metadata files
                (e.g. ``collection_meta.json``).  Leave False (default) for
                high-frequency per-vector data files where fsync would be a
                performance bottleneck (Bug #1223 perf fix).
        """
        # Use a per-call unique tmp filename so concurrent writes to the same
        # target file each own their tmp file and don't race on rename/delete.
        tmp_file = file_path.with_name(
            f"{file_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
        )

        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            # Atomic rename — visible to readers only after this completes.
            # Last writer wins; all intermediate writers produce valid JSON files.
            tmp_file.replace(file_path)
        except Exception:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def load_id_index(self, collection_name: str) -> set:
        """Load ID index and return set of existing point IDs.

        Public method for external components that need to check existing points.

        Args:
            collection_name: Name of the collection

        Returns:
            Set of existing point IDs
        """
        # Story #1456 AC7: CHUNKS_DB collections source the point-id set
        # directly from chunks.db -- id_index.bin is never read or written.
        collection_path = self._get_collection_path(collection_name)
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                return set(chunk_store.all_point_ids())
            finally:
                chunk_store.close()

        id_index = self._load_id_index(collection_name)
        return set(id_index.keys())

    def _load_id_index(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> Dict[str, Path]:
        """Load ID index from persistent binary file for fast loading.

        Uses IDIndexManager to load from id_index.bin binary file.  If the
        file is corrupt (CorruptIDIndexError), automatically repairs it by
        calling rebuild_from_vectors() and returns the rebuilt map.  Any other
        exception propagates unchanged.

        Falls back to directory scan only when id_index.bin does not exist
        (backward compatibility with indexes created before the binary index).

        Args:
            collection_name: Name of the collection
            subdirectory: Optional subdirectory (e.g. "multimodal_index") for
                a nested collection. ``None`` (every existing write-path
                caller) resolves via ``self.base_path / collection_name``,
                byte-identical to the pre-fix behavior. When provided,
                resolves via ``self._get_collection_path(collection_name,
                subdirectory)`` instead, so a nested collection's
                ``id_index.bin`` (and its ``vector_*.json`` rglob fallback)
                is read from its REAL location instead of a non-existent
                top-level directory.

        Returns:
            Dictionary mapping point IDs to file paths
        """
        from .id_index_manager import CorruptIDIndexError, IDIndexManager

        collection_path = self._get_collection_path(collection_name, subdirectory)
        index_manager = IDIndexManager()

        try:
            index = index_manager.load_index(collection_path)
        except CorruptIDIndexError as exc:
            self.logger.warning(
                "id_index.bin corrupt for collection '%s' (%s); "
                "auto-repairing via rebuild_from_vectors()",
                collection_name,
                exc,
            )
            return index_manager.rebuild_from_vectors(collection_path)

        if index:
            return index

        # Fallback: scan by filename pattern — only when binary index absent
        fallback: Dict[str, Path] = {}
        for json_file in collection_path.rglob("vector_*.json"):
            filename = json_file.name
            if filename.startswith("vector_") and filename.endswith(".json"):
                point_id = filename[7:-5]
                fallback[point_id] = json_file

        return fallback

    def _load_file_paths(self, collection_name: str, id_index: Dict[str, Path]) -> set:
        """Load file paths from JSON files using ID index.

        This is a separate operation from loading the ID index, allowing operations
        that only need vector counts to avoid parsing JSON files.

        Args:
            collection_name: Name of the collection
            id_index: ID index mapping point IDs to file paths

        Returns:
            Set of unique file paths
        """
        file_paths = set()

        # Parse JSON files to extract file paths
        for json_file in id_index.values():
            try:
                with open(json_file) as f:
                    data = json.load(f)

                # Extract file path from payload only
                file_path = data.get("payload", {}).get("path", "")
                if file_path:
                    file_paths.add(file_path)

            except (json.JSONDecodeError, KeyError, FileNotFoundError):
                # Skip corrupted or missing files
                continue

        return file_paths

    def _load_path_index(self, collection_name: str) -> PathIndex:
        """Load path index from persistent binary file.

        Loads the reverse index mapping file_path -> Set[point_id] from
        path_index.bin in the collection directory. Returns empty PathIndex
        if file doesn't exist (new collection or pre-Story #540 index).

        Args:
            collection_name: Name of the collection

        Returns:
            PathIndex instance with loaded mappings (empty if file doesn't exist)

        Note:
            Story #540: Prevents duplicate chunks by tracking all point_ids per file.
        """
        subdirectory = self._active_subdirectories.get(collection_name)
        collection_path = self._get_collection_path(collection_name, subdirectory)
        path_index_file = collection_path / "path_index.bin"

        # Load from disk or return empty if file doesn't exist
        return PathIndex.load(path_index_file)

    def _save_path_index(
        self,
        collection_name: str,
        path_index: PathIndex,
        subdirectory: Optional[str] = None,
    ) -> None:
        """Save path index to persistent binary file.

        Saves the reverse index mapping file_path -> Set[point_id] to
        path_index.bin in the collection directory.

        Args:
            collection_name: Name of the collection
            path_index: PathIndex instance to save
            subdirectory: Optional explicit subdirectory (e.g.
                "multimodal_index"). When provided, wins over the
                active-indexing-session fallback below -- required so
                ``_rebuild_path_index_from_disk`` (Codex-16 Finding 3) can
                persist a rebuilt index at the correct nested location when
                called OUTSIDE an active indexing session, where
                ``_active_subdirectories`` is empty. When None, falls back
                to the active-indexing subdirectory recorded for this
                collection, byte-identical to every existing caller that
                omits this argument.

        Note:
            Story #540: Persists path index for duplicate prevention across sessions.
        """
        if subdirectory is None:
            subdirectory = self._active_subdirectories.get(collection_name)
        collection_path = self._get_collection_path(collection_name, subdirectory)
        path_index_file = collection_path / "path_index.bin"

        # Bug #1575 unlocked-save race (dual-review Fix 3, both Claude
        # opus and Codex independently reproduced): path_index is
        # frequently the SAME live, still-mutable object registered in
        # self._path_indexes -- PathIndex.save() iterated its internal
        # dict/sets directly, so a concurrent add_point()/remove_point()
        # call (from another upsert_points()/delete_points() call for the
        # SAME collection, arriving after this method's caller released
        # _path_index_lock) could mutate the dict/set mid-iteration,
        # raising "RuntimeError: dictionary changed size during
        # iteration". Snapshotting under _path_index_lock first --
        # mirroring this SAME method's own id_index_copy idiom a few
        # lines below -- then writing the snapshot outside the lock
        # preserves the B1 lock-ordering invariant (no lock held during
        # I/O) while eliminating the torn read.
        with self._path_index_lock:
            path_index_snapshot = path_index.snapshot()
        PathIndex.save_snapshot(path_index_snapshot, path_index_file)

        # Story #1456 AC7: CHUNKS_DB collections never write id_index.bin --
        # path_index.bin above is unaffected/preserved, but the legacy
        # co-persist write below is skipped entirely for this layout.
        # MUST use the combined _is_chunks_db_collection authority (not the
        # bare resolver): this method runs from end_indexing() BEFORE the
        # discriminator is committed (AC1 ordering), so the bare resolver
        # alone would still see SHARDED_JSON during a fresh build's very
        # first call and silently write an empty id_index.bin (a real bug
        # this exact guard is fixing).
        if self._is_chunks_db_collection(collection_name, collection_path):
            return

        # Co-persist id_index when it is already in memory so that a cold-start
        # scroll_points fast path does not fall back to rglob in _load_id_index.
        # Shallow copy is taken under lock to avoid concurrent mutation during I/O.
        with self._id_index_lock:
            cache_key = self._id_cache_key(collection_name, subdirectory)
            raw = self._id_index.get(cache_key)
            id_index_copy: Optional[Dict[str, Path]] = (
                dict(raw) if raw is not None else None
            )
        if id_index_copy is not None:
            from .id_index_manager import IDIndexManager

            IDIndexManager().save_index(collection_path, id_index_copy)

    # Story #726: _ensure_gitignore() method removed.
    # CIDX must NEVER modify files outside .code-indexer/ directory.
    # The .gitignore modification was causing git pull failures in golden repositories.

    def _prepare_vector_data_batch(
        self,
        point_id: str,
        vector: np.ndarray,
        payload: Dict[str, Any],
        chunk_text: Optional[str],
        repo_root: Optional[Path],
        blob_hashes: Dict[str, str],
        uncommitted_files: set,
    ) -> Dict[str, Any]:
        """Prepare vector data using batch git operation results.

        Args:
            point_id: Unique point identifier
            vector: Vector data
            payload: Point payload
            chunk_text: Content text at root level (optimization path, optional)
            repo_root: Git repository root (None if not a git repo)
            blob_hashes: Dict of file_path -> blob_hash from batch operation
            uncommitted_files: Set of files with uncommitted changes

        Returns:
            Dictionary ready for JSON serialization
        """
        data = {
            "id": point_id,
            "vector": vector.tolist(),
            # file_path, start_line, end_line removed - already in payload as path, line_start, line_end
            "metadata": {
                "language": payload.get("language", ""),
                "type": payload.get("type", "content"),
            },
            "payload": payload,  # Store full payload for search operations
        }

        file_path = payload.get("path", "")
        payload_type = payload.get("type", "")

        # Check if this is a commit message - these should ALWAYS store chunk_text
        # Commit messages are indexed as searchable entities and need their content stored
        if payload_type == "commit_message":
            # Commit messages: always store chunk_text
            if chunk_text is not None:
                data["chunk_text"] = chunk_text
            else:
                # MESSI Rule #2 (Anti-Fallback): Fail fast instead of masking bugs
                raise RuntimeError(
                    f"Missing chunk_text for vector with payload_type={payload_type}. "
                    f"This indicates an indexing bug. Vector ID: {point_id}"
                )
        # Check if this is a temporal diff - these should ALWAYS store content
        # Temporal diffs represent historical commit content at specific points in time,
        # NOT current working tree state. Using current HEAD blob hash would be meaningless.
        elif payload_type == "commit_diff":
            # Storage optimization: added/deleted files use pointer-based storage
            if payload.get("reconstruct_from_git"):
                # Added/deleted files: NO chunk_text storage (pointer only)
                # Content can be reconstructed from git on query using commit hash
                # This provides 88% storage reduction for these file types
                pass  # Don't store chunk_text
            else:
                # Modified files: store diff in chunk_text
                # Prefer chunk_text from point root (optimization path)
                if chunk_text is not None:
                    data["chunk_text"] = chunk_text
                else:
                    # Legacy: extract from payload if present
                    data["chunk_text"] = payload.get("content", "")

            # Remove content from payload to avoid duplication
            if "content" in data["payload"]:
                del data["payload"]["content"]
        # Git-aware chunk storage logic using batch results (for regular files only)
        elif repo_root and file_path:
            has_uncommitted = file_path in uncommitted_files

            if not has_uncommitted and file_path in blob_hashes:
                # File is clean and in git: store only blob hash (space efficient)
                data["git_blob_hash"] = blob_hashes[file_path]
                data["indexed_with_uncommitted_changes"] = False
                # Remove content from payload to avoid duplication
                if "content" in data["payload"]:
                    del data["payload"]["content"]
            else:
                # File has uncommitted changes or untracked: store chunk text
                # Prefer chunk_text from point root (optimization path)
                if chunk_text is not None:
                    data["chunk_text"] = chunk_text
                else:
                    # Legacy: extract from payload if present
                    data["chunk_text"] = payload.get("content", "")
                data["indexed_with_uncommitted_changes"] = True
                # Remove content from payload (stored in chunk_text instead)
                if "content" in data["payload"]:
                    del data["payload"]["content"]
        else:
            # Non-git repo: always store chunk_text
            # Prefer chunk_text from point root (optimization path)
            if chunk_text is not None:
                data["chunk_text"] = chunk_text
            else:
                # Legacy: extract from payload if present
                data["chunk_text"] = payload.get("content", "")
            # Remove content from payload (stored in chunk_text instead)
            if "content" in data["payload"]:
                del data["payload"]["content"]

        return data

    def _get_blob_hashes_batch(
        self, file_paths: List[str], repo_root: Path
    ) -> Dict[str, str]:
        """Get git blob hashes for multiple files in batched git calls.

        Args:
            file_paths: List of file paths relative to repo root
            repo_root: Git repository root

        Returns:
            Dictionary mapping file_path to blob_hash

        Note:
            FIX 2: Batches git ls-tree calls to avoid "Argument list too long" error (Errno 7)
            when processing thousands of files. Each batch processes up to 100 files.
        """
        try:
            # Batch to avoid "Argument list too long" error (Errno 7)
            BATCH_SIZE = 100
            blob_hashes = {}

            for i in range(0, len(file_paths), BATCH_SIZE):
                batch = file_paths[i : i + BATCH_SIZE]
                result = subprocess.run(
                    ["git", "ls-tree", "HEAD"] + batch,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode == 0 and result.stdout:
                    # Parse output: "mode type hash\tfilename"
                    for line in result.stdout.strip().split("\n"):
                        if not line:
                            continue
                        parts = line.split()
                        if len(parts) >= 3:
                            blob_hash = parts[2]
                            # Filename is after tab
                            tab_idx = line.find("\t")
                            if tab_idx >= 0:
                                filename = line[tab_idx + 1 :]
                                blob_hashes[filename] = blob_hash

            return blob_hashes

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}

    def _check_uncommitted_batch(self, file_paths: List[str], repo_root: Path) -> set:
        """Check which files have uncommitted changes in batched git calls.

        Args:
            file_paths: List of file paths to check
            repo_root: Git repository root

        Returns:
            Set of file paths with uncommitted changes

        Note:
            Batches git status calls to avoid "Argument list too long" error (Errno 7)
            when processing thousands of files. Each batch processes up to 100 files.
        """
        try:
            # Batch to avoid "Argument list too long" error (Errno 7)
            BATCH_SIZE = 100
            uncommitted = set()

            for i in range(0, len(file_paths), BATCH_SIZE):
                batch = file_paths[i : i + BATCH_SIZE]
                result = subprocess.run(
                    ["git", "status", "--porcelain"] + batch,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode == 0:
                    # Parse output format: "XY filename"
                    # When file paths are provided as arguments, format is "XY filename" (status codes + space + filename)
                    # X = index status (position 0), Y = worktree status (position 1), space (position 2), filename (position 3+)
                    # However, when filtering by files, the format drops the leading space for clean index
                    for line in result.stdout.strip().split("\n"):
                        if not line:
                            continue
                        # The status codes are in positions 0-1, space at position 2 (or 1 if no leading space)
                        # Safe approach: find the first space and take everything after it
                        space_idx = line.find(" ")
                        if space_idx >= 0 and space_idx < len(line) - 1:
                            filename = line[space_idx + 1 :]
                            if filename:
                                uncommitted.add(filename)

            return uncommitted

        except (subprocess.TimeoutExpired, FileNotFoundError):
            # If git command fails, assume all files have changes (safe fallback)
            return set(file_paths)

    def _get_point_from_chunk_store(
        self, collection_path: Path, point_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read a single point from a CHUNKS_DB collection's chunk store.

        Story #1456 AC7: point-id resolution for CHUNKS_DB collections goes
        exclusively through the chunk store -- id_index.bin is never read.
        Opened on the calling/main thread (sqlite3 connections are not shared
        across threads). Shared by ``get_point()``'s primary CHUNKS_DB branch
        and its Bug #1486 re-resolve fallback so the read logic exists once.
        """
        from code_indexer.storage.sqlite_chunk_store import (
            open_chunk_store_for_path,
        )

        chunk_store = open_chunk_store_for_path(
            collection_path / "chunks.db", str(collection_path)
        )
        try:
            record = chunk_store.read(point_id)
        finally:
            chunk_store.close()

        if record is None:
            return None

        result = {
            "id": record["id"],
            "vector": record["vector"],
            "payload": record.get("payload", {}),
        }
        if "chunk_text" in record:
            result["chunk_text"] = record["chunk_text"]
        return result

    def get_point(
        self,
        point_id: str,
        collection_name: str,
        subdirectory: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a specific point by ID.

        Args:
            point_id: Point ID to retrieve
            collection_name: Name of the collection
            subdirectory: Optional subdirectory path within base_path (e.g.
                "multimodal_index"). Threaded from the scroll fast path so a
                nested collection hydrates from its real location; None
                (every existing caller) is byte-identical to the pre-fix
                top-level resolution.

        Returns:
            Point data with id, vector, and payload, or None if not found

        Raises:
            ScrollDataIntegrityError: If the point's on-disk record is PRESENT
                but malformed (invalid JSON, a non-dict JSON root, or a record
                missing its required ``id``/``vector`` field). A vanished file
                (FileNotFoundError between the existence check and the open) is
                NOT an error -- it is the concurrent-flip signal and returns
                None / re-resolves to the chunk store.
        """
        # Story #1456 AC7: CHUNKS_DB collections resolve via the chunk store
        # directly -- id_index.bin is never read or written for this method.
        # Uses the combined _is_chunks_db_collection authority (not the bare
        # resolver) so this is correct even mid-build, before end_indexing()
        # commits the discriminator.
        from .id_index_manager import IDIndexManager

        collection_path = self._get_collection_path(collection_name, subdirectory)
        if self._is_chunks_db_collection(collection_name, collection_path):
            return self._get_point_from_chunk_store(collection_path, point_id)

        # Legacy SHARDED_JSON path. Compute the result under the id-index lock.
        # A vanished file (FileNotFoundError) leaves both outputs None and falls
        # through to the re-resolve below. A PRESENT-but-malformed record is
        # captured in ``malformed`` and raised/redispatched AFTER releasing the
        # lock (Codex-15 LOW, Messi #13) -- never a raw AttributeError from an
        # unguarded ``data.get()`` on a non-dict root, and never a silent None
        # that masks corruption as a plain miss.
        legacy_result: Optional[Dict[str, Any]] = None
        # (kind, exception_or_None) describing a present-but-malformed record.
        malformed: Optional[Tuple[str, Optional[Exception]]] = None
        with self._id_index_lock:
            cache_key = self._id_cache_key(collection_name, subdirectory)
            if cache_key not in self._id_index:
                self._id_index[cache_key] = self._load_id_index(
                    collection_name, subdirectory
                )

            index = self._id_index[cache_key]

            vector_file = index.get(point_id)
            if (
                vector_file is None
                and cache_key not in self._id_index_reactive_rebuild_done
            ):
                # Bug #1583: id_index.bin is a CACHE, not an authority -- a
                # vector_*.json file written outside the normal
                # upsert_points()/end_indexing() write path (or a crash
                # between writing the vector file and persisting the
                # updated id_index.bin) can leave a genuinely-present point
                # invisible to this lookup. Detected REACTIVELY here, only
                # on an actual miss, rather than eagerly inside
                # _load_id_index() on every load: an eager per-load
                # directory scan was tried first and rejected during
                # review -- it broke Bug #677's zero-rglob-on-the-fast-path
                # invariant (test_path_index_fast_path_after_reload, which
                # resolves point data via this SAME get_point() call from
                # scroll_points()'s PathIndex fast path) and, at fleet
                # scale (some repos have hundreds of thousands of
                # vector_*.json files -- see the Chunk Storage Layout
                # notes), would have turned every single cold collection
                # load into a full directory walk. This reactive form pays
                # the rebuild cost only on a genuine miss, and the
                # _id_index_reactive_rebuild_done marker caps that cost at
                # ONE rebuild per collection per process, so a lookup for a
                # point_id that legitimately never existed does not
                # re-trigger it on every call.
                #
                # Dual-review correction (Fix 1, opus HIGH/HIGH): the
                # original fix called ``rebuild_from_vectors()``, which
                # DURABLY WRITES ``id_index.bin`` and takes
                # ``.index_rebuild.lock`` -- turning this READ-path method
                # into a WRITE. On an immutable ``.versioned/`` snapshot
                # (this project's hard invariant: NEVER modify
                # ``.versioned/`` paths) that write can corrupt the
                # snapshot. Use the side-effect-free
                # ``scan_vectors_for_id_map()`` instead -- it NEVER reads or
                # writes ``id_index.bin``. Any exception it raises (e.g.
                # ``DuplicateSourceIdError`` from a genuine duplicate-source
                # condition, or a ``PermissionError`` from a non-writable/
                # unreadable directory) is caught here and degraded to a
                # plain miss -- ``get_point()`` never raised on a simple
                # miss before this mechanism existed, and it must not start
                # now.
                #
                # Dual-review correction (Fix 2, Codex 99%): the marker is
                # added to ``_id_index_reactive_rebuild_done`` ONLY AFTER
                # the scan completes, and ONLY when the scan still does NOT
                # find ``point_id`` -- never before the scan runs, and never
                # on a successful heal. A FAILED scan (exception) must not
                # permanently mark the collection done (the underlying
                # problem may later be corrected), and a SUCCESSFUL heal for
                # one point must not permanently disarm reactive rebuild for
                # a DIFFERENT point written out-of-band later in the same
                # process (proven by
                # test_two_successive_bypass_writes_both_heal_in_same_process).
                # Residual limitation, stated precisely: the marker is
                # PER-COLLECTION (``cache_key``), not per-point_id. Once a
                # scan has run and genuinely NOT found the requested
                # point_id (a confirmed-negative outcome), the marker is set
                # and NO further reactive scan runs for this cache_key in
                # this process -- including for a DIFFERENT point later
                # written out-of-band. Only a scan that FINDS the requested
                # point_id, or a scan that FAILS (exception), leaves the
                # marker unset and the collection eligible for a future
                # reactive scan.
                try:
                    rebuilt_index = IDIndexManager().scan_vectors_for_id_map(
                        collection_path
                    )
                except Exception as scan_exc:
                    self.logger.warning(
                        "get_point(): reactive id-index scan failed for "
                        "collection %r (point_id=%r): %s -- degrading to a "
                        "plain miss; a later lookup may retry once the "
                        "underlying condition is corrected",
                        collection_name,
                        point_id,
                        scan_exc,
                    )
                else:
                    self._id_index[cache_key] = rebuilt_index
                    index = rebuilt_index
                    vector_file = index.get(point_id)
                    if vector_file is None:
                        self._id_index_reactive_rebuild_done.add(cache_key)
            if vector_file is not None and vector_file.exists():
                try:
                    with open(vector_file) as f:
                        data = json.load(f)
                except FileNotFoundError:
                    # Bug #1486 Finding 5 (TOCTOU): the legacy vector_*.json
                    # vanished between the exists() check and this open() -- a
                    # concurrent server-mode migration deleted it AFTER flipping
                    # the discriminator. Treat it as a plain legacy miss: leave
                    # everything None and fall through to the re-resolve below,
                    # which hydrates the row from chunks.db.
                    legacy_result = None
                except json.JSONDecodeError as json_exc:
                    # A PRESENT but corrupt file. Deferred: a concurrent flip
                    # (discriminator now CHUNKS_DB) makes this a doomed migration
                    # remnant -> redispatch; otherwise genuine corruption ->
                    # fail loud (decided AFTER the lock, see below). Bind to a
                    # non-except-scoped name so the reference survives past this
                    # block (the except-bound name is deleted on exit).
                    malformed = ("json", json_exc)
                else:
                    if not isinstance(data, dict):
                        malformed = ("nondict", None)
                    elif "id" not in data:
                        malformed = ("noid", None)
                    elif "vector" not in data:
                        malformed = ("novec", None)
                    else:
                        legacy_result = {
                            "id": data["id"],
                            "vector": data["vector"],
                            "payload": data.get("payload", {}),
                        }
                        if "chunk_text" in data:
                            legacy_result["chunk_text"] = data["chunk_text"]

        if legacy_result is not None:
            return legacy_result

        # Bug #1486 (Codex Finding 4): re-resolve the committed discriminator on
        # the calling thread. A concurrent server-mode fleet migration may have
        # flipped it to CHUNKS_DB and deleted the legacy vector_*.json /
        # id_index.bin files. The flip is the atomic swap point (committed
        # durably BEFORE legacy deletion). A permanently-SHARDED_JSON collection
        # re-resolves to SHARDED_JSON (one cheap top-level JSON key read).
        from code_indexer.storage.shared.chunk_layout import (
            ChunkLayout,
            resolve_chunk_layout,
        )

        flipped = resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        # Codex-15 LOW (JSONDecodeError redispatch decision): a discriminator
        # flip to CHUNKS_DB is authoritative -- the discriminator flips ONLY via
        # a migration, and the flip is committed BEFORE the legacy files are
        # deleted. So a malformed/half-deleted legacy record observed alongside a
        # flip is a doomed migration remnant, not genuine corruption: redispatch
        # to the chunk store (uniformly, for every malformation kind). ABSENT a
        # flip, a present-but-malformed record is genuine corruption on a
        # still-SHARDED_JSON collection and fails LOUD naming the file, rather
        # than being silently swallowed as a plain miss (which returned None and
        # dropped the row from the caller's view).
        if malformed is not None:
            if flipped:
                return self._get_point_from_chunk_store(collection_path, point_id)
            kind, malformed_exc = malformed
            if kind == "json":
                raise ScrollDataIntegrityError(
                    f"legacy vector file {str(vector_file)!r} is not valid JSON "
                    f"({malformed_exc}); refusing to treat a corrupt present "
                    f"record as a missing point"
                ) from malformed_exc
            if kind == "nondict":
                raise ScrollDataIntegrityError(
                    f"legacy vector file {str(vector_file)!r} has a non-dict "
                    f"JSON root; refusing to treat a malformed present record "
                    f"as a missing point"
                )
            if kind == "noid":
                raise ScrollDataIntegrityError(
                    f"legacy vector file {str(vector_file)!r} has no 'id' field; "
                    f"refusing to treat a malformed present record as a missing "
                    f"point"
                )
            # kind == "novec"
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(vector_file)!r} is missing the "
                f"required 'vector' field; refusing to treat a malformed "
                f"present record as a missing point"
            )

        if flipped:
            return self._get_point_from_chunk_store(collection_path, point_id)
        return None

    def _resolve_authoritative_path_index(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> PathIndex:
        """Bug #1575 Part A: return an authoritative PathIndex for a
        SHARDED_JSON collection.

        Trusts the in-memory ``self._path_indexes`` entry ONLY when
        ``collection_name in self._indexing_session_changes`` proves an
        ACTIVE indexing session for THIS collection is currently populating
        and mutating it (``begin_indexing()`` sets this entry;
        ``end_indexing()`` clears it) -- that object is provably fresh
        because every ``upsert_points()`` call this session has kept it in
        sync via ``add_point``/``remove_point``, at zero extra I/O. Both
        the session-membership check and the cache lookup are performed
        together inside ONE ``_path_index_lock`` critical section so they
        are atomic with each other from this method's perspective.

        A bare "is this cache key present in self._path_indexes" check is
        NOT sufficient: other call sites (e.g.
        ``get_existing_content_hashes``) also lazily populate the SAME
        cache from a plain, unvalidated disk load outside any active
        session, which is exactly the "path_index.bin is a cache, not an
        authority" staleness risk this method exists to avoid trusting
        blindly. Absent a proven active session, this falls back to an
        authoritative streaming rebuild from disk
        (``_rebuild_path_index_from_disk``).
        """
        cached = self._get_live_session_path_index(collection_name, subdirectory)
        if cached is not None:
            return cached
        return self._rebuild_and_repair_path_index(collection_name, subdirectory)

    def _rebuild_and_repair_path_index(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> PathIndex:
        """Bug #1575 Part A Round 3, Fix A: build the authoritative
        PathIndex from disk (``_rebuild_path_index_from_disk`` already
        persists it to ``path_index.bin``) AND repair the LIVE in-memory
        ``self._path_indexes`` entry with that same complete result,
        marking it ``_path_index_loaded_from_file = True``.

        Closes Gap A (Codex, round 3): without this repair, a session that
        starts with no ``path_index.bin`` on disk correctly falls back to
        this same full scan for ITS OWN answer, but its own live
        ``self._path_indexes`` entry is left as whatever THIS session's own
        mutations happened to touch (empty, then only the files this
        session's upserts/deletes touched) -- NOT the complete picture just
        computed here. ``end_indexing()`` then unconditionally persists
        that still-partial live entry to ``path_index.bin``, and a LATER
        session sees the bin now exists and wrongly trusts it via the fast
        path.

        Repairing here means: whenever this (already expensive, O(N))
        fallback runs, its result becomes the new live picture too -- so
        any SAVE that happens afterwards (in this session or a later one)
        persists the complete, correct picture instead of a partial one.
        This mirrors the codebase's established self-healing philosophy
        (Part C's hnsw_sync fail-safe design, the #1583 id_index reactive
        rebuild): use the result of forced authoritative work to fix the
        cache, don't just answer the immediate question and leave the
        cache broken for next time.

        Bug #1575 round 7 (opus dual-review, confirmed real): the repair
        used to unconditionally SWAP ``self._path_indexes[cache_key]`` with
        the freshly-rebuilt-from-disk object. If another thread/process
        held a reference to the OLD live object and added a point to it
        around the same time this method ran (e.g. a concurrent
        out-of-session ``upsert_points()`` for the same collection), that
        addition was silently discarded the instant the swap replaced the
        dict entry -- the disk rescan can never see a point that was only
        ever added in-memory (or whose file write is still in flight), so
        the freshly-rebuilt object never carries it. Fixed by MERGING the
        rebuilt picture INTO the existing live object (when one already
        exists) instead of replacing it -- mirroring the PRE-EXISTING M2
        fix's merge-not-swap approach already applied to
        ``scroll_points()``'s lazy rebuild (that parity claim covers ONLY
        the merge; see that method's own comment for the DISTINCT
        before/after-snapshot prune mirror added there for THIS round-7
        follow-up, and its documented residual gap -- the two mirrors were
        not both true until then):
        ``merge_from`` uses ``add_point`` (set semantics), so re-adding an
        entry the live object already has is a safe no-op, while anything
        the live object gained concurrently survives because the object's
        IDENTITY in ``self._path_indexes`` is never replaced.

        Bug #1575 round 7 follow-up (empirically caught by repeatedly
        running ``test_filesystem_vector_store_1575_round3_gap_c_
        concurrency.py``): a PURE union merge introduces the mirror-image
        defect -- ``_rebuild_path_index_from_disk``'s scan runs WITHOUT
        holding ``_path_index_lock``, so it can observe a STALE,
        pre-deletion snapshot of disk if a concurrent ``delete_points()``
        commits (DB row delete + in-memory ``remove_point``, atomic under
        this same lock per Fix C) while the scan is in flight. Merging
        that stale snapshot in afterwards would silently RESURRECT a point
        this process just correctly deleted, disagreeing with the very
        chunk store this scan just read.

        Fixed by snapshotting the live object TWICE -- once immediately
        before the disk scan starts, once immediately after it completes
        (both under ``_path_index_lock``) -- and treating any point present
        in the "before" snapshot but absent from the "after" snapshot as a
        genuine concurrent deletion that must be pruned back out even if
        the (possibly stale) disk scan still has it. Both snapshots are of
        the SAME live object, and every production mutation of that object
        (``add_point``/``remove_point``) is itself lock-protected, so any
        mutation that ran during the scan window is captured EXACTLY by
        this before/after delta, regardless of what the unlocked disk scan
        happened to observe. A point added during the window is preserved
        by the merge (already reflected in "after"); a point removed
        during the window is pruned by this explicit step; anything
        untouched during the window is answered correctly by the scan
        itself, since a lock-protected mutation cannot straddle across
        either lock acquisition here undetected.
        """
        cache_key = self._id_cache_key(collection_name, subdirectory)
        with self._path_index_lock:
            _pre_scan_live_index = self._path_indexes.get(cache_key)
            before_snapshot = (
                _pre_scan_live_index.snapshot()
                if _pre_scan_live_index is not None
                else None
            )

        rebuilt = self._rebuild_path_index_from_disk(collection_name, subdirectory)

        with self._path_index_lock:
            live_index = self._path_indexes.get(cache_key)
            if live_index is None:
                self._path_indexes[cache_key] = rebuilt
            else:
                self._merge_rebuilt_path_index_with_prune(
                    cache_key, before_snapshot, rebuilt
                )
            self._path_index_loaded_from_file[cache_key] = True
        # Deliberately return the PURE disk-authoritative `rebuilt` object,
        # never the merged/cache-repaired `live_index` -- both existing
        # callers (`_resolve_authoritative_path_index`,
        # `_calculate_and_save_unique_file_count`'s SHARDED_JSON branch)
        # invoke this method SPECIFICALLY because the live cache is not
        # (or cannot be) trusted, and need an answer immune to whatever a
        # stale/corrupted live entry might contain (project-owner decision,
        # 6 dual-review rounds -- see
        # ``test_filesystem_vector_store_1575_sharded_json_shortcut_
        # abandoned.py``). The cache-side merge+prune above is a SEPARATE
        # concern: it keeps ``self._path_indexes[cache_key]`` (consulted by
        # Part B/Story #540's duplicate-prevention and other cache readers)
        # from losing a genuine concurrent mutation to a swap, without
        # letting that same cache leak into this method's own answer.
        return rebuilt

    def _merge_rebuilt_path_index_with_prune(
        self,
        cache_key: str,
        before_snapshot: Optional[Dict[str, Set[str]]],
        rebuilt: "PathIndex",
    ) -> "PathIndex":
        """Bug #1575 round 7 shared merge+prune step, extracted out of
        :meth:`_rebuild_and_repair_path_index` so ``scroll_points()``'s
        lazy-rebuild fast path (Bug #1575 Part 2) can reuse the identical
        mechanism instead of duplicating it (Messi Rule #4).

        Merges ``rebuilt`` (a freshly disk-scanned ``PathIndex``, whose
        UNLOCKED scan may have observed a stale, pre-deletion view of disk)
        into the LIVE ``self._path_indexes[cache_key]`` object, then prunes
        any (path, point_id) pair present in ``before_snapshot`` but absent
        from a freshly-recaptured "after" snapshot -- a point legitimately
        removed by a concurrent ``delete_points()`` while the scan was in
        flight. See :meth:`_rebuild_and_repair_path_index`'s own docstring
        for the full round-7 rationale this mirrors exactly.

        ``before_snapshot`` of ``None`` skips pruning entirely (mirrors
        :meth:`_rebuild_and_repair_path_index`'s own
        ``before_snapshot is not None`` guard -- there is nothing to prune
        against when the caller had no live entry to snapshot before
        starting the scan).

        Must be called while holding ``_path_index_lock``, with
        ``self._path_indexes[cache_key]`` already populated -- every
        current call site guarantees this (this helper does not replicate
        ``_rebuild_and_repair_path_index``'s separate ``live_index is
        None`` branch).

        Returns the same live object (mutated in place), for caller
        convenience.
        """
        live_index = self._path_indexes[cache_key]
        after_snapshot = live_index.snapshot()
        live_index.merge_from(rebuilt)
        if before_snapshot is not None:
            for path, point_ids in before_snapshot.items():
                removed_during_scan = point_ids - after_snapshot.get(path, set())
                for point_id in removed_during_scan:
                    live_index.remove_point(path, point_id)
        return live_index

    def _get_live_session_path_index(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> Optional[PathIndex]:
        """Bug #1575 Part A (Codex follow-up, finding 4): the "trust the
        in-memory PathIndex ONLY under a proven active indexing session"
        check, extracted so :meth:`_resolve_authoritative_path_index` and
        :meth:`distinct_content_paths` share ONE implementation. Returns
        ``None`` when no active session for this collection proves the
        cached entry fresh (see the sibling docstring for the rationale).

        NOT used by :meth:`_calculate_and_save_unique_file_count` -- that
        method's SHARDED_JSON branch abandoned this fast-path trust
        entirely (project-owner final decision, matching CHUNKS_DB's own
        earlier revert) and always calls
        :meth:`_rebuild_and_repair_path_index` unconditionally instead.

        Bug #1575 Finding-1-regression fix (opus, reproduced): an ACTIVE
        session alone is not sufficient. If ``path_index.bin`` did not
        exist on disk when ``begin_indexing()`` ran, the in-memory
        PathIndex was built EMPTY and only ever learns about the files
        THIS session happens to touch -- trusting it as the whole
        collection's picture produced a catastrophic undercount (a 10-file
        collection with a missing path_index.bin reported
        ``unique_file_count == 1`` after a session that touched only one
        file). ``self._path_index_loaded_from_file`` (set unconditionally
        by ``begin_indexing()``) is the "was this ever proven complete"
        signal; absent a True value here, this returns ``None`` exactly
        like the "no active session" case, and every caller already falls
        back to a full, authoritative disk scan.
        """
        cache_key = self._id_cache_key(collection_name, subdirectory)
        with self._path_index_lock:
            has_active_session = collection_name in self._indexing_session_changes
            if not has_active_session:
                return None
            if not self._path_index_loaded_from_file.get(cache_key, False):
                return None
            return self._path_indexes.get(cache_key)

    def _lazy_load_path_index_tracked(
        self, collection_name: str, cache_key: str
    ) -> PathIndex:
        """Bug #1575 round 6 (opus/Codex dual review, CRITICAL item 1):
        lazy-load ``path_index.bin`` for an OUT-OF-SESSION mutation (watch
        mode, ``delete_by_filter()``'s reconcile path, or any other
        ``upsert_points()``/``delete_points()`` call with no active
        ``begin_indexing()`` session for this collection), recording
        whether the bin actually existed on disk -- mirroring
        ``begin_indexing()``'s own provenance bookkeeping into
        ``self._path_index_loaded_from_file``.

        Without this, every out-of-session lazy-load site left
        ``_path_index_loaded_from_file`` unset for its cache_key (that dict
        is otherwise populated ONLY by ``begin_indexing()`` and
        ``_rebuild_and_repair_path_index()``), so Gap D's/Gap B's
        out-of-session persist could not tell a genuinely complete,
        freshly-loaded picture apart from one built EMPTY because
        ``path_index.bin`` was missing -- and blindly persisting the
        latter is the exact mechanism that reintroduced the round-2
        catastrophic-undercount bug (a 25-file collection reduced to a
        1-file ``path_index.bin`` after a single out-of-session upsert).

        Must be called while already holding ``_path_index_lock`` (same
        contract as ``_load_path_index``, which performs no locking of its
        own).
        """
        subdirectory = self._active_subdirectories.get(collection_name)
        collection_path = self._get_collection_path(collection_name, subdirectory)
        bin_existed = (collection_path / "path_index.bin").exists()
        self._path_index_loaded_from_file[cache_key] = bin_existed
        return self._load_path_index(collection_name)

    def _persist_out_of_session_path_index(
        self,
        collection_name: str,
        cache_key: str,
        subdirectory: Optional[str],
    ) -> None:
        """Bug #1575 round 6, item 1: the single shared decision for
        persisting an out-of-session PathIndex mutation (Gap D's
        upsert-side persist and Gap B's delete-side persist, both
        SHARDED_JSON and CHUNKS_DB layouts).

        Only persists the live in-memory PathIndex DIRECTLY when
        ``self._path_index_loaded_from_file`` proves it was actually
        loaded from an existing, presumed-complete bin (or previously
        repaired by ``_rebuild_and_repair_path_index``) -- otherwise the
        picture was never proven complete and forcing an authoritative
        rebuild-and-repair is the only safe option: it streams the TRUE
        on-disk picture (including whatever this call just wrote), persists
        it, and marks this cache_key proven-complete so later out-of-session
        calls in this same process can trust and persist directly without
        repeating the full rescan.

        Bug #1575 round 7 (opus review, confirmed real, distinct from the
        swap-vs-merge defect fixed in ``_rebuild_and_repair_path_index``):
        this method used to accept the live PathIndex as a caller-supplied
        parameter -- every real call site captured it under its OWN
        ``_path_index_lock`` acquisition and then released that lock
        BEFORE calling in here. Between that capture and the
        ``_save_path_index`` call below, a concurrent mutation for the
        SAME ``cache_key`` (another out-of-session call, or a
        ``_rebuild_and_repair_path_index`` repair) could have moved
        ``self._path_indexes[cache_key]`` forward -- persisting the
        caller's now-stale snapshot would silently regress
        ``path_index.bin`` to an older picture. Fixed by re-reading
        ``self._path_indexes[cache_key]`` here, under the SAME lock
        acquisition used to read ``self._path_index_loaded_from_file``,
        immediately before deciding what (if anything) to persist --
        never trusting a reference captured across an already-released
        lock boundary.
        """
        with self._path_index_lock:
            loaded_from_file = self._path_index_loaded_from_file.get(cache_key, False)
            current_path_index = self._path_indexes.get(cache_key)
        if loaded_from_file:
            if current_path_index is not None:
                self._save_path_index(
                    collection_name, current_path_index, subdirectory=subdirectory
                )
        else:
            self._rebuild_and_repair_path_index(collection_name, subdirectory)

    @staticmethod
    def _content_scan_integrity_message(vector_file: Path, reason: str) -> str:
        """Shared ``ScrollDataIntegrityError`` message for
        :meth:`_stream_authoritative_content_paths_from_disk` (Bug #1575
        Part A, Gap 2) -- one place naming the offending file.
        """
        return (
            f"legacy vector file {str(vector_file)!r} {reason} during the "
            f"authoritative content-path scan; refusing to silently drop "
            f"a present record"
        )

    @staticmethod
    def _extract_content_path(data: Dict[str, Any]) -> Optional[str]:
        """Return the stored path for a ``type == "content"`` record, else
        None. Missing/non-dict ``payload``/``metadata`` are tolerated
        defensively (never ``AttributeError``); the type check requires
        an EXPLICIT "content" match (metadata first, else payload), never
        defaulted -- the real writer always stamps ``metadata.type``
        explicitly (``_prepare_vector_data_batch``). Bug #1575 Part A.
        """
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        record_type = metadata.get("type")
        if record_type is None:
            record_type = payload.get("type")
        if record_type != "content":
            return None
        file_path = payload.get("path")
        return file_path if isinstance(file_path, str) and file_path else None

    def _stream_authoritative_content_paths_from_disk(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> Set[str]:
        """Bug #1575 Part A (Codex findings 3+4; Gap 2): memory-bounded,
        ``type == "content"``-filtered streaming scan of a SHARDED_JSON
        collection's on-disk ``vector_*.json`` files. Never persists
        ``path_index.bin``.

        A PRESENT-but-malformed record fails loud via
        ``ScrollDataIntegrityError``, naming the file. A file that
        VANISHES between listing and reading (``FileNotFoundError``, Bug
        #1486 Finding-5) is a different, legitimate race and is skipped.
        """
        if subdirectory is None:
            subdirectory = self._active_subdirectories.get(collection_name)
        collection_path = self._get_collection_path(collection_name, subdirectory)

        content_paths: Set[str] = set()
        for vector_file in collection_path.rglob("vector_*.json"):
            try:
                with open(str(vector_file), "r") as fh:
                    data = json.load(fh)
            except FileNotFoundError:
                continue
            except json.JSONDecodeError as exc:
                raise ScrollDataIntegrityError(
                    self._content_scan_integrity_message(
                        vector_file, f"is not valid JSON ({exc})"
                    )
                ) from exc
            except UnicodeDecodeError as exc:
                raise ScrollDataIntegrityError(
                    self._content_scan_integrity_message(
                        vector_file, f"contains undecodable bytes ({exc})"
                    )
                ) from exc

            if not isinstance(data, dict):
                raise ScrollDataIntegrityError(
                    self._content_scan_integrity_message(
                        vector_file, "has a non-dict JSON root"
                    )
                )

            file_path = self._extract_content_path(data)
            if file_path:
                content_paths.add(file_path)
        return content_paths

    def distinct_content_paths(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> Set[str]:
        """Bug #1575 Part A: authoritative, memory-bounded enumeration of
        distinct content-point stored paths for a collection.

        Replaces materializing every content payload
        (``_fetch_all_content_points``) just to derive the set of distinct
        file paths on record. Retains ONLY the path strings -- never a list
        of payloads.
        """
        collection_path = self._get_collection_path(collection_name, subdirectory)
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            with open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            ) as chunk_store:
                return set(chunk_store.distinct_content_paths())

        # SHARDED_JSON, live in-memory session: the cached PathIndex is
        # authoritative and free (no disk I/O). No per-point type check is
        # needed here (unlike the disk-fallback below): temporal is the
        # sole non-"content" writer in this codebase and it never sets
        # payload.path (uses paths/primary_path instead) AND is exclusively
        # CHUNKS_DB since Bug #1528, so this SHARDED_JSON PathIndex can only
        # ever be populated by type=="content" points (Bug #1575
        # investigation, mirrored in sqlite_chunk_store.py's
        # _ensure_type_column docstring).
        cached = self._get_live_session_path_index(collection_name, subdirectory)
        if cached is not None:
            # The cached index may be the SHARED, session-live object
            # another thread's upsert_points() call is concurrently
            # mutating -- read it under the same lock upsert_points() holds
            # while mutating it, to avoid a torn read.
            with self._path_index_lock:
                return cached.all_paths()

        # Absent an active session, fall back to a dedicated lightweight
        # scan (Codex findings 3+4) instead of the full point-id-tracking,
        # path_index.bin-persisting rebuild _resolve_authoritative_path_index
        # uses for its own (unrelated) targeted-lookup job.
        return self._stream_authoritative_content_paths_from_disk(
            collection_name, subdirectory
        )

    def fetch_points_for_paths(
        self,
        collection_name: str,
        paths: Set[str],
        subdirectory: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Bug #1575 Part A: targeted, payload-only fetch of the points
        stored under ``paths`` -- never a full collection scan.

        Returns ``{"id": ..., "payload": {...}}`` dicts (never vectors,
        matching the memory-bounded ``with_vectors=False`` contract
        ``_fetch_all_content_points`` used).
        """
        if not paths:
            return []
        collection_path = self._get_collection_path(collection_name, subdirectory)
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            with open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            ) as chunk_store:
                # Codex review follow-up (Bug #1575 Part A, finding 6):
                # this caller only ever needs id/payload -- never the
                # vector -- so request the payload_only fetch that skips
                # vector decode entirely.
                records = chunk_store.fetch_points_for_paths(paths, payload_only=True)
            return [
                {"id": record["id"], "payload": record.get("payload", {})}
                for record in records
            ]

        path_index = self._resolve_authoritative_path_index(
            collection_name, subdirectory
        )
        point_ids: Set[str] = set()
        with self._path_index_lock:
            for path in paths:
                point_ids.update(path_index.get_point_ids(path))

        points: List[Dict[str, Any]] = []
        for point_id in point_ids:
            point = self.get_point(point_id, collection_name, subdirectory)
            if point is not None:
                points.append({"id": point["id"], "payload": point.get("payload", {})})
        return points

    def get_existing_content_hashes(
        self, file_path: str, collection_name: str
    ) -> Dict[int, Dict[str, Any]]:
        """Get existing content hashes for a file's chunks.

        Story #470: Enables smart embedding cache by loading existing
        content_hash values for comparison before re-embedding.

        Args:
            file_path: Relative file path (as stored in payload)
            collection_name: Vector collection name

        Returns:
            Dict mapping chunk_index -> {"content_hash": str, "vector": list, "point_id": str}
            Empty dict if no existing vectors or no content_hash.
        """
        result: Dict[int, Dict[str, Any]] = {}

        # Resolved via _active_subdirectories (this method has no
        # subdirectory param) so a nested collection's lookup uses its OWN
        # _path_indexes cache entry and hydrates via the matching get_point
        # cache key, never a bare-name top-level collision.
        _content_hashes_subdirectory = self._active_subdirectories.get(collection_name)
        _content_hashes_cache_key = self._id_cache_key(
            collection_name, _content_hashes_subdirectory
        )

        with self._path_index_lock:
            if _content_hashes_cache_key not in self._path_indexes:
                self._path_indexes[_content_hashes_cache_key] = self._load_path_index(
                    collection_name
                )
            path_index = self._path_indexes[_content_hashes_cache_key]

        point_ids = path_index.get_point_ids(file_path)
        if not point_ids:
            return result

        for point_id in point_ids:
            point_data = self.get_point(
                point_id, collection_name, subdirectory=_content_hashes_subdirectory
            )
            if point_data is None:
                continue
            payload = point_data.get("payload", {})
            content_hash = payload.get("content_hash")
            if not content_hash:
                continue
            chunk_idx = payload.get("chunk_index", 0)
            result[chunk_idx] = {
                "content_hash": content_hash,
                "vector": point_data["vector"],
                "point_id": point_data["id"],
            }

        return result

    def _parse_filter(self, filter_conditions: Optional[Dict[str, Any]]) -> Any:
        """Parse filter to callable that evaluates payload.

        Supports TWO filter formats:

        1. Nested filters (CLI format):
           {"must": [{"key": "language", "match": {"value": "python"}}]}
           {"should": [{"key": "type", "match": {"value": "test"}}]}
           {"must_not": [{"key": "git_available", "match": {"value": False}}]}

        2. Flat dict filters:
           {"language": "python", "type": "test"}

        Args:
            filter_conditions: Filter dictionary in either format

        Returns:
            Callable that takes payload dict and returns True if matches filter
        """
        if not filter_conditions:
            return lambda payload: True

        # Detect filter format: nested has "must"/"should"/"must_not" keys
        is_nested_style = any(
            key in filter_conditions for key in ["must", "should", "must_not"]
        )

        if is_nested_style:
            # Nested filter
            def evaluate_condition(
                condition: Dict[str, Any], payload: Dict[str, Any]
            ) -> bool:
                """Evaluate a single condition against payload.

                Supports both simple conditions and nested filters:
                - Simple: {"key": "language", "match": {"value": "python"}}
                - Nested: {"should": [{"key": "language", "match": {"value": "py"}}, ...]}
                """
                # Check if this is a nested filter (has must/should/must_not)
                is_nested = any(
                    key in condition for key in ["must", "should", "must_not"]
                )

                if is_nested:
                    # Recursively evaluate nested filter
                    # Handle "must" conditions (AND)
                    if "must" in condition:
                        for nested_condition in condition["must"]:
                            if not evaluate_condition(nested_condition, payload):
                                return False

                    # Handle "should" conditions (OR) - at least one must match
                    if "should" in condition:
                        if not any(
                            evaluate_condition(nested_condition, payload)
                            for nested_condition in condition["should"]
                        ):
                            return False

                    # Handle "must_not" conditions (NOT)
                    if "must_not" in condition:
                        for nested_condition in condition["must_not"]:
                            if evaluate_condition(nested_condition, payload):
                                return False

                    return True
                else:
                    # Simple key-match condition
                    key = condition.get("key")
                    if not key or not isinstance(key, str):
                        return False

                    # Handle nested payload keys (e.g., "metadata.language")
                    current: Any = payload
                    for key_part in key.split("."):
                        if isinstance(current, dict):
                            current = current.get(key_part)
                        else:
                            return False

                    # TEMPORAL COLLECTION FIX: If 'path' field is None and key is "path",
                    # fall back to 'file_path' field (temporal collection format)
                    # This enables path filters to work with both collection formats:
                    # - Main collection: uses 'path' field
                    # - Temporal collection: uses 'file_path' field
                    if current is None and key == "path" and "file_path" in payload:
                        current = payload["file_path"]

                    # Check for range specification (NEW: temporal filter support)
                    range_spec = condition.get("range")
                    if range_spec:
                        # Range filtering for numeric fields (timestamps, etc.)
                        if not isinstance(current, (int, float)):
                            return False

                        # Apply range constraints
                        if "gte" in range_spec and current < range_spec["gte"]:
                            return False
                        if "gt" in range_spec and current <= range_spec["gt"]:
                            return False
                        if "lte" in range_spec and current > range_spec["lte"]:
                            return False
                        if "lt" in range_spec and current >= range_spec["lt"]:
                            return False

                        return True

                    # Check for match specification (existing logic)
                    match_spec = condition.get("match", {})

                    # Support "any" (set membership - NEW: temporal filter support)
                    if "any" in match_spec:
                        allowed_values = match_spec["any"]
                        return current in allowed_values

                    # Support "contains" (substring match - NEW: temporal filter support)
                    if "contains" in match_spec:
                        if not isinstance(current, str):
                            return False
                        substring = match_spec["contains"]
                        return substring.lower() in current.lower()

                    # Support both "value" (exact match) and "text" (pattern match)
                    if "value" in match_spec:
                        # Exact match
                        expected_value = match_spec["value"]
                        return bool(current == expected_value)
                    elif "text" in match_spec:
                        # Pattern match (glob-style wildcards)
                        # Use PathPatternMatcher for cross-platform consistency
                        from code_indexer.services.path_pattern_matcher import (
                            PathPatternMatcher,
                        )

                        pattern = match_spec["text"]
                        if not isinstance(current, str):
                            return False

                        matcher = PathPatternMatcher()
                        return bool(matcher.matches_pattern(current, pattern))
                    else:
                        # No match or range specification found
                        return False

            def evaluate_filter(payload: Dict[str, Any]) -> bool:
                """Evaluate full filter against payload."""
                # Handle "must" conditions (AND)
                if "must" in filter_conditions:
                    for condition in filter_conditions["must"]:
                        if not evaluate_condition(condition, payload):
                            return False

                # Handle "should" conditions (OR) - at least one must match
                if "should" in filter_conditions:
                    if not any(
                        evaluate_condition(condition, payload)
                        for condition in filter_conditions["should"]
                    ):
                        return False

                # Handle "must_not" conditions (NOT)
                if "must_not" in filter_conditions:
                    for condition in filter_conditions["must_not"]:
                        if evaluate_condition(condition, payload):
                            return False

                return True

            return evaluate_filter
        else:
            # Flat dict filter (legacy format)
            def evaluate_flat_filter(payload: Dict[str, Any]) -> bool:
                """Evaluate flat dict filter against payload."""
                for key, expected_value in filter_conditions.items():
                    # Handle nested payload keys (e.g., "metadata.language")
                    current: Any = payload
                    for key_part in key.split("."):
                        if isinstance(current, dict):
                            current = current.get(key_part)
                        else:
                            return False

                    if current != expected_value:
                        return False

                return True

            return evaluate_flat_filter

    def _extract_path_filter(
        self, filter_conditions: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """Extract the target file path from a must-clause path equality filter.

        Scans filter_conditions["must"] for a clause of the form:
            {"key": "path", "match": {"value": <file_path>}}

        Args:
            filter_conditions: Filter conditions dict (may be None)

        Returns:
            The file path string if a path equality clause is found AND the
            filter has no keys other than "must", else None.

        Note:
            The fast path is only safe when filter_conditions has EXACTLY the
            key {"must"}.  If "should" or "must_not" (or any other key) is
            present those clauses would be silently discarded by the fast path,
            producing incorrect results.  Return None in that case to fall
            through to the rglob path which evaluates the full filter.
        """
        if not filter_conditions:
            return None
        # M1 fix: fast path only when the ONLY top-level key is "must".
        if set(filter_conditions.keys()) != {"must"}:
            return None
        must_clauses = filter_conditions.get("must")
        if not isinstance(must_clauses, list):
            return None
        for clause in must_clauses:
            if (
                isinstance(clause, dict)
                and clause.get("key") == "path"
                and isinstance(clause.get("match"), dict)
                and "value" in clause["match"]
            ):
                return str(clause["match"]["value"])
        return None

    def _rebuild_path_index_from_disk(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> PathIndex:
        """Walk the collection directory and rebuild a PathIndex from on-disk JSON files.

        Called lazily when scroll_points detects that no path_index.bin exists for
        a collection that already has vector files (i.e. a legacy or incomplete index).
        After rebuilding, persists path_index.bin so subsequent calls use the fast path.

        Args:
            collection_name: Name of the collection to rebuild for
            subdirectory: Optional explicit subdirectory (e.g.
                "multimodal_index"). When provided, wins over the
                active-indexing-session fallback below -- required for
                ``scroll_points`` to rebuild a nested collection's path
                index correctly OUTSIDE an active indexing session, where
                ``_active_subdirectories`` is empty (Codex-16 Finding 3).
                When None, falls back to the active-indexing subdirectory
                recorded for this collection, byte-identical to every
                existing caller that omits this argument.

        Returns:
            Freshly built PathIndex populated from all existing vector JSON files
        """
        if subdirectory is None:
            subdirectory = self._active_subdirectories.get(collection_name)
        collection_path = self._get_collection_path(collection_name, subdirectory)
        path_index = PathIndex()

        # Story #1456 AC3: CHUNKS_DB collections stream from chunks.db
        # instead of rglob-scanning vector_*.json files.
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                for record in chunk_store.stream_all():
                    point_id = record.get("id", "")
                    file_path = record.get("payload", {}).get("path", "")
                    if point_id and file_path:
                        path_index.add_point(file_path, point_id)
            finally:
                chunk_store.close()

            self._save_path_index(
                collection_name, path_index, subdirectory=subdirectory
            )
            return path_index

        for vector_file in collection_path.rglob("*.json"):
            if "collection_meta" in vector_file.name:
                continue
            if vector_file.name == HNSW_SYNC_STATE_FILENAME:
                # Bug #1619: dedicated hnsw_sync bookkeeping sidecar, never
                # a vector record.
                continue
            try:
                with open(str(vector_file), "r") as fh:
                    data: Dict[str, Any] = json.load(fh)
                point_id = data.get("id", "")
                file_path = data.get("payload", {}).get("path", "")
                if point_id and file_path:
                    path_index.add_point(file_path, point_id)
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                self.logger.warning(
                    "Skipping malformed vector file during path index rebuild: %s. Error: %s",
                    vector_file,
                    exc,
                )
                continue
        # Persist so next call hits the fast path
        self._save_path_index(collection_name, path_index, subdirectory=subdirectory)
        return path_index

    @staticmethod
    def _encode_scroll_cursor(point_id: str) -> str:
        """Bug #1488: mint the next-page cursor from a REAL point-id.

        Both scroll layouts iterate the SAME ``sorted(real point-id)`` order and
        emit this self-describing cursor, so a cursor issued under one layout
        resumes correctly after a concurrent flip to the other.
        """
        return _SCROLL_CURSOR_PREFIX + point_id

    @staticmethod
    def _resolve_legacy_scroll_token(token: str, ordered_ids: List[str]) -> str:
        """Bug #1488 (Codex Finding B): resolve a pre-#1488 ``vector_<token>.json``
        path-format cursor's ``<token>`` to the REAL stored point-id it names.

        The sharded filename token is NOT always the point-id: temporal
        collections name files ``vector_<sha256(point_id)[:16]>.json`` while
        non-temporal collections name them ``vector_<point_id-with-slashes-as-_>``.
        Resolution matches the token against the actual stored ids by BOTH
        schemes (so it is correct regardless of collection type, without needing
        to know which). Fails LOUD (Messi #13) on zero matches or ambiguity --
        never silently restarts pagination at offset 0.
        """
        from code_indexer.storage.temporal_metadata_store import (
            generate_hash_prefix,
        )

        matches = {
            pid
            for pid in ordered_ids
            if token == pid.replace("/", "_") or token == generate_hash_prefix(pid)
        }
        if len(matches) == 0:
            raise ValueError(
                f"legacy scroll cursor token {token!r} matches no stored "
                f"point-id; refusing to silently restart pagination at offset 0"
            )
        if len(matches) > 1:
            raise ValueError(
                f"legacy scroll cursor token {token!r} is ambiguous: matches "
                f"{sorted(matches)!r}; refusing to guess a resume position"
            )
        return next(iter(matches))

    @staticmethod
    def _resolve_scroll_cursor(
        offset: Optional[str], ordered_ids: List[str]
    ) -> Optional[str]:
        """Bug #1488 (Codex Finding B): resolve a pagination cursor to the stable,
        LAYOUT-INDEPENDENT real point-id to resume strictly AFTER (via
        ``bisect_right``), so a cursor issued under one chunk layout is honored
        after a concurrent flip to the other -- and a garbage cursor is refused
        loudly rather than silently mis-bisecting to page 1.

        - ``None`` -> ``None`` (a legitimate "start from the beginning").
        - A self-describing ``_SCROLL_CURSOR_PREFIX`` cursor -> its embedded real
          point-id, returned verbatim. Honored even if that point was deleted
          between pages: ``bisect_right`` then lands on the first greater id
          (correct continuation, no dup, no gap). This is the ONLY path that
          tolerates an absent id, and it is safe precisely because the prefix
          proves we minted it -- a "deleted-but-valid id" resumes, distinct from
          a garbage cursor.
        - A legacy ``vector_<token>.json`` path-format cursor -> resolved to the
          stored point-id it names (see ``_resolve_legacy_scroll_token``); a
          token matching no current id fails LOUD (a hex token carries no
          ordering information, so a since-deleted legacy cursor cannot be safely
          continued).
        - Anything else -> ``ValueError`` (fail loud, Messi #13). NEVER silently
          returns ``None`` / restarts at offset 0 for an unrecognized cursor.
        """
        if offset is None:
            return None
        if offset.startswith(_SCROLL_CURSOR_PREFIX):
            embedded = offset[len(_SCROLL_CURSOR_PREFIX) :]
            if not embedded:
                # Codex MEDIUM (Messi #13): a prefix-ONLY cursor strips to an
                # empty embedded point-id. bisect_right(ordered_ids, "") returns
                # 0 -> a silent restart at page 1 (duplicating already-consumed
                # results). Fail loud instead of ever bisecting on "".
                raise ValueError(
                    f"scroll cursor {offset!r} has an empty embedded point-id "
                    f"after the {_SCROLL_CURSOR_PREFIX!r} prefix; refusing to "
                    f"silently restart pagination at offset 0"
                )
            return embedded
        name = Path(offset).name
        if name.startswith("vector_") and name.endswith(".json"):
            token = name[len("vector_") : -len(".json")]
            return FilesystemVectorStore._resolve_legacy_scroll_token(
                token, ordered_ids
            )
        raise ValueError(
            f"unrecognized scroll cursor {offset!r}: not a self-describing "
            f"{_SCROLL_CURSOR_PREFIX!r} cursor and not a legacy "
            f"vector_<token>.json path cursor; refusing to silently restart "
            f"pagination at offset 0"
        )

    @staticmethod
    def _validate_scroll_vector(
        vector: Any, expected_dim: int, vector_file: str
    ) -> None:
        """Bug #1488 (Codex ITEM 1 tail, Messi #13): fail LOUD when a legacy
        SHARDED_JSON record's ``vector`` field is PRESENT but MALFORMED during
        ``scroll_points(with_vectors=True)`` hydration -- never return a silently
        wrong value (a ``None``, a string, an object, a NaN/Inf, a wrong-
        dimension vector). The prior round only rejected a MISSING ``vector``;
        a present-but-garbage vector was still returned verbatim.

        Mirrors ``ChunkStore._encode_vector``'s validation exactly (non-empty
        list -> numeric dtype -> finite -> expected dimension) so BOTH storage
        layouts reject the same malformed shapes identically, rather than
        introducing a divergent ad-hoc validator. Pure/in-memory (numpy only, no
        file I/O), so it never masks the Bug #1486 mid-hydration
        ``FileNotFoundError`` re-dispatch. Only ever called when
        ``with_vectors=True`` -- the vector is otherwise never returned, so never
        validated, and the ``with_vectors=False`` path is byte-identical to
        before (no new cost, no new raises).
        """
        if not isinstance(vector, list):
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has a malformed 'vector' "
                f"field during scroll hydration: expected a non-empty list, got "
                f"{type(vector).__name__}; refusing to return a silently wrong "
                f"value"
            )
        if not vector:
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has an empty 'vector' field "
                f"during scroll hydration; refusing to return a silently wrong "
                f"value"
            )
        try:
            arr = np.asarray(vector)
        except (ValueError, TypeError) as exc:
            # A ragged nested list (e.g. [[0.1, 0.2], [0.3]] or [0.1, [0.2]])
            # makes np.asarray itself raise -- translate into the contextual
            # integrity error instead of leaking a raw ValueError/TypeError.
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has a malformed (ragged) "
                f"'vector' field during scroll hydration ({exc}); refusing to "
                f"return a silently wrong value"
            ) from exc
        if arr.ndim != 1:
            # A 2-D nested vector (e.g. [[0.1, 0.2]] * expected_dim -> shape
            # (expected_dim, 2)) is numeric, finite, and passes a shape[0]-only
            # check -- reject it here before any dimension comparison.
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has a non-1-dimensional "
                f"'vector' field during scroll hydration (ndim={arr.ndim}, "
                f"shape={arr.shape}); refusing to return a silently wrong value"
            )
        if arr.dtype.kind not in ("i", "u", "f"):
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has a non-numeric 'vector' "
                f"field during scroll hydration (dtype={arr.dtype}); refusing to "
                f"return a silently wrong value"
            )
        if not np.isfinite(arr).all():
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has a non-finite 'vector' "
                f"field during scroll hydration (NaN or inf); refusing to return "
                f"a silently wrong value"
            )
        if arr.shape[0] != expected_dim:
            raise ScrollDataIntegrityError(
                f"legacy vector file {vector_file!r} has 'vector' dimension "
                f"{arr.shape[0]} during scroll hydration, expected "
                f"{expected_dim}; refusing to return a silently wrong value"
            )

    def _scroll_points_chunks_db(
        self,
        collection_name: str,
        collection_path: Path,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
        offset: Optional[str],
        filter_conditions: Optional[Dict[str, Any]],
        subdirectory: Optional[str] = None,
    ) -> tuple:
        """Story #1456 AC3: paginate a CHUNKS_DB collection over chunks.db,
        using sorted point_ids as the opaque offset cursor (never rglob).

        Extracted from ``scroll_points()`` so both its primary
        ``_is_chunks_db_collection`` gate and its Bug #1486 re-resolve gate
        share one implementation. Opened on the calling/main thread (sqlite3
        connections are not shared across threads).

        Bug #1488 (Codex Medium, Messi #13): this CHUNKS_DB branch is now
        EXHAUSTIVELY fail-loud, at parity with the SHARDED_JSON hydration path.
        (1) An id returned by ``point_ids_after()`` whose row cannot be
        hydrated (``read`` -> ``None``) is a chunks.db primary-key/row
        INCONSISTENCY (corruption) -- both queries run against the SAME
        main-thread connection, so this is never a normal concurrent-delete
        state (and CHUNKS_DB is terminal -- there is no flip AWAY from it, so
        no legitimate migration-vanish story exists here) -- it RAISES rather
        than silently omitting the id (which would drop a row AND can emit a
        terminal ``None`` cursor falsely presenting a complete traversal).
        (2) Under ``with_vectors=True`` a structurally-valid row whose stored
        vector decodes to a wrong-dimension, 1-element, empty, or non-finite
        array is validated through the SAME ``_validate_scroll_vector`` the
        SHARDED_JSON path uses (never a divergent validator) and RAISES naming
        the point-id + ``vector``. The ``with_vectors=False`` path is
        byte-identical to before -- no ``expected_dim`` lookup, no vector
        access, no new raises.

        Bug #1575 Part B: this method used to call
        ``sorted(chunk_store.all_point_ids())`` on EVERY page -- for N points
        and page size L that is ~(N/L) full O(N log N) Python sorts of N ids
        across one scroll. It now resolves the cursor
        (``_resolve_chunks_db_scroll_cursor``) and fetches candidate ids in
        bounded batches (``_scan_chunks_db_scroll_page``) via
        ``ChunkStore.point_ids_after()``'s keyset query, so the cost of
        retrieving ONE page is bounded by the page size, never by the
        collection's total row count.
        """
        from code_indexer.storage.sqlite_chunk_store import (
            open_chunk_store_for_path,
        )

        filter_func_cdb = None
        if filter_conditions:
            filter_func_cdb = self._parse_filter(filter_conditions)

        # Bug #1488: the expected vector dimension, read ONLY when vectors are
        # actually returned so the ``with_vectors=False`` path stays
        # byte-identical (no new I/O). ``_get_vector_size`` is cached and is the
        # SAME dimension the write path validated the stored vector against.
        expected_dim_cdb: Optional[int] = (
            self._get_vector_size(collection_name, subdirectory)
            if with_vectors
            else None
        )

        chunk_store = open_chunk_store_for_path(
            collection_path / "chunks.db", str(collection_path)
        )
        try:
            cursor = self._resolve_chunks_db_scroll_cursor(offset, chunk_store)

            cdb_points, last_examined_id_cdb = self._scan_chunks_db_scroll_page(
                chunk_store,
                cursor,
                limit,
                filter_func_cdb,
                collection_path,
                with_payload,
                with_vectors,
                expected_dim_cdb,
            )

            # Codex-15 MEDIUM: cursor on the LAST EXAMINED id; only emitted
            # when a full page of matches was collected AND at least one more
            # id exists beyond it. Bug #1575 Part B: "is there more" is now a
            # single bounded existence-check keyset query (limit=1) instead
            # of a Python ``len(all_ids)`` comparison against a pre-sorted
            # full list.
            next_offset_cdb = None
            if len(cdb_points) == limit and last_examined_id_cdb is not None:
                trailing_cdb = chunk_store.point_ids_after(last_examined_id_cdb, 1)
                if trailing_cdb:
                    next_offset_cdb = self._encode_scroll_cursor(last_examined_id_cdb)

            return cdb_points, next_offset_cdb
        finally:
            chunk_store.close()

    def _resolve_chunks_db_scroll_cursor(
        self, offset: Optional[str], chunk_store: Any
    ) -> Optional[str]:
        """Bug #1575 Part B: resolve a scroll cursor WITHOUT a full O(N)
        enumeration when possible.

        A self-describing cursor (the format this method itself always
        mints going forward) resolves to its embedded point-id with ZERO
        query -- ``_resolve_scroll_cursor``'s prefix branch never touches its
        ``ordered_ids`` argument. Only the rare legacy
        ``vector_<token>.json`` path-format cursor (pre-#1488, no longer
        emitted anywhere in this codebase) needs the full id set to
        disambiguate a hash-prefix/slash-token match -- that ONE case still
        pays the ``sorted(all_point_ids())`` cost this fix otherwise
        eliminates, since it cannot be resolved any other way.
        """
        if offset is None:
            return None
        if offset.startswith(_SCROLL_CURSOR_PREFIX):
            return self._resolve_scroll_cursor(offset, [])
        return self._resolve_scroll_cursor(offset, sorted(chunk_store.all_point_ids()))

    def _hydrate_chunks_db_scroll_point(
        self,
        chunk_store: Any,
        point_id: str,
        collection_path: Path,
        with_payload: bool,
        with_vectors: bool,
        expected_dim: Optional[int],
    ) -> tuple:
        """Read + validate ONE chunks.db row for scroll_points hydration.

        Returns ``(point, raw_payload)`` -- ``raw_payload`` is the REAL
        hydrated payload regardless of ``with_payload`` (the filter must see
        it even when the caller does not want it returned). Raises
        ``ScrollDataIntegrityError`` on a missing/corrupt row or a
        structurally malformed vector -- never silently drops a row.
        """
        record = chunk_store.read(point_id)
        if record is None:
            raise ScrollDataIntegrityError(
                f"chunks.db enumerated point-id {point_id!r} has no "
                f"hydratable row (read returned None) in collection "
                f"{str(collection_path)!r}; refusing to silently drop an "
                f"enumerated primary key from a paginated scroll"
            )

        point: Dict[str, Any] = {"id": record["id"]}
        if with_payload:
            point["payload"] = record.get("payload", {})
        if with_vectors:
            vector_value = record.get("vector", [])
            if hasattr(vector_value, "tolist"):
                vector_value = vector_value.tolist()
            assert expected_dim is not None
            self._validate_scroll_vector(
                vector_value, expected_dim, f"{point_id} (chunks.db)"
            )
            point["vector"] = vector_value

        return point, record.get("payload", {})

    def _scan_chunks_db_scroll_page(
        self,
        chunk_store: Any,
        cursor: Optional[str],
        limit: int,
        filter_func: Optional[Any],
        collection_path: Path,
        with_payload: bool,
        with_vectors: bool,
        expected_dim: Optional[int],
    ) -> tuple:
        """Bug #1575 Part B: scan forward from ``cursor`` in BOUNDED batches
        (via ``point_ids_after()``) until either ``limit`` filter-matching
        rows are collected or ids are exhausted -- NEVER a single upfront
        full-table fetch. Returns ``(points, last_examined_id)``.
        """
        cdb_points: List[Dict[str, Any]] = []
        last_examined_id: Optional[str] = None
        current_cursor = cursor
        exhausted = False
        while len(cdb_points) < limit and not exhausted:
            batch_ids = chunk_store.point_ids_after(current_cursor, limit)
            if not batch_ids:
                break
            for point_id in batch_ids:
                if len(cdb_points) >= limit:
                    break
                last_examined_id = point_id
                point, raw_payload = self._hydrate_chunks_db_scroll_point(
                    chunk_store,
                    point_id,
                    collection_path,
                    with_payload,
                    with_vectors,
                    expected_dim,
                )
                if filter_func is not None and not filter_func(raw_payload):
                    continue
                cdb_points.append(point)
            current_cursor = last_examined_id
            if len(batch_ids) < limit:
                # Fewer ids than requested means no more rows exist after
                # the last one in this batch -- exhausted.
                exhausted = True
        return cdb_points, last_examined_id

    def _parse_sharded_json_scroll_record_id(self, f: Path) -> str:
        """Read + validate ONE legacy vector file during enumeration,
        returning its stored point-id. Extracted from the pre-#1575 inline
        enumeration loop to keep ``_build_sharded_json_scroll_index``
        within this file's method-size limits.

        A mid-scan vanish (``FileNotFoundError``, a subclass of
        ``OSError``) is deliberately NOT caught here -- it must propagate
        to the Bug #1486 Finding-5 re-dispatch. A file that is genuinely
        PRESENT but unreadable as a record (bad JSON, non-dict root,
        missing/invalid ``id``) is a data-integrity fault and fails LOUD
        (Messi #13) -- never silently skipped.
        """
        try:
            with open(str(f), "r") as _fh:
                _data: Dict[str, Any] = json.load(_fh)
        except json.JSONDecodeError as exc:
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(f)!r} is not valid JSON "
                f"({exc}); refusing to silently drop it from a "
                f"paginated scroll"
            ) from exc
        if not isinstance(_data, dict):
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(f)!r} has a non-dict JSON root "
                f"({type(_data).__name__}); refusing to silently drop it "
                f"from a paginated scroll"
            )
        if "id" not in _data:
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(f)!r} has no 'id' field; "
                f"refusing to silently drop it from a paginated scroll"
            )
        _pid = _data["id"]
        if not isinstance(_pid, str) or not _pid:
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(f)!r} has an invalid 'id' "
                f"({_pid!r}); expected a non-empty string point-id"
            )
        return _pid

    def _build_sharded_json_scroll_index(
        self, collection_path: Path
    ) -> Dict[str, Path]:
        """Bug #1575 Part B: the O(N) legacy-scroll enumeration, extracted
        UNCHANGED (behaviorally) from the pre-#1575 inline loop -- rglob +
        parse EVERY ``vector_*.json`` file to build the id -> file map
        keyed by each file's STORED point-id (the real point-id in BOTH
        layouts, per Bug #1488 Finding B).

        This is the exact cost Bug #1575 Part B eliminates from repeating
        on every scroll page: called via ``_get_sharded_json_scroll_index``
        ONCE per scroll session (on the first page, or on a cache miss),
        and once more (bypassing the cache) on the bounded 1-retry
        stale-cache self-heal path in ``scroll_points``. Propagates
        ``FileNotFoundError`` (Bug #1486 Finding 5) and
        ``ScrollDataIntegrityError`` (Messi #13) from
        ``_parse_sharded_json_scroll_record_id`` unchanged.
        """
        id_to_file: Dict[str, Path] = {}
        for f in collection_path.rglob("*.json"):
            if "collection_meta" in f.name:
                continue
            fname = f.name
            if not (fname.startswith("vector_") and fname.endswith(".json")):
                continue
            pid = self._parse_sharded_json_scroll_record_id(f)
            if pid in id_to_file:
                raise ScrollDataIntegrityError(
                    f"duplicate stored point-id {pid!r} across legacy "
                    f"vector files {str(id_to_file[pid])!r} and "
                    f"{str(f)!r}; refusing to silently collapse them"
                )
            id_to_file[pid] = f
        return id_to_file

    def _get_sharded_json_scroll_index(
        self,
        collection_name: str,
        collection_path: Path,
        subdirectory: Optional[str],
        *,
        read_cache: bool,
    ) -> Tuple[Dict[str, Path], bool]:
        """Bug #1575 Part B: serve the SHARDED_JSON legacy-scroll id_to_file
        enumeration from a per-collection session cache when ``read_cache``
        is True and an entry already exists; otherwise rebuild it fresh via
        ``_build_sharded_json_scroll_index`` (which may raise
        ``FileNotFoundError``/``ScrollDataIntegrityError`` -- propagated
        UNCHANGED) and WRITE-THROUGH the fresh result into the cache so a
        later continuation page of the SAME scroll session can reuse it.

        A fresh scroll (``scroll_points(offset=None)``) always passes
        ``read_cache=False``, guaranteeing every NEW scroll observes the
        CURRENT on-disk state rather than a stale view left over from an
        earlier, unrelated scroll session on this collection.

        Returns ``(id_to_file, was_cache_hit)`` -- ``was_cache_hit`` tells
        the caller whether a subsequent hydration ``FileNotFoundError``
        against this map might be due to STALE cached data (warranting
        exactly one rebuild-and-retry) or a genuine fresh-scan failure
        (never retried).
        """
        cache_key = self._id_cache_key(collection_name, subdirectory)
        if read_cache:
            with self._scroll_sharded_json_index_cache_lock:
                cached = self._scroll_sharded_json_index_cache.get(cache_key)
            if cached is not None:
                return cached, True

        fresh = self._build_sharded_json_scroll_index(collection_path)
        with self._scroll_sharded_json_index_cache_lock:
            self._scroll_sharded_json_index_cache[cache_key] = fresh
        return fresh, False

    def _invalidate_scroll_sharded_json_cache(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> None:
        """Codex review Finding 1 (Bug #1575 Part B): evict this
        collection's cached SHARDED_JSON scroll id_to_file enumeration
        whenever a mutation (``upsert_points``/``delete_points``) changes
        which ``vector_*.json`` files exist for it, so a scroll session
        already in progress observes writes that happen BETWEEN its pages
        -- preserving the pre-Part-B "every page rebuilds fresh" guarantee
        instead of silently freezing a page-1 snapshot for the rest of the
        session.

        Mirrors the existing HNSW/id_index cache invalidation convention
        (``rebuild_hnsw_filtered``): compose the SAME key used at populate
        time (``_id_cache_key``, identical to the key
        ``_get_sharded_json_scroll_index`` reads/writes) and evict
        immediately after the mutation that invalidated it. A miss (no
        scroll session currently has this collection cached) is a
        harmless no-op -- ``dict.pop(..., None)`` never raises.
        """
        cache_key = self._id_cache_key(collection_name, subdirectory)
        with self._scroll_sharded_json_index_cache_lock:
            self._scroll_sharded_json_index_cache.pop(cache_key, None)

    def _load_sharded_json_scroll_record(self, vector_file: Path) -> Dict[str, Any]:
        """Read + parse ONE legacy vector_*.json file for scroll hydration,
        extracted UNCHANGED (behaviorally) from the pre-#1575 inline "Load
        points" loop.

        A mid-hydration vanish (``FileNotFoundError``, a subclass of
        ``OSError``) is deliberately NOT caught here -- it signals the Bug
        #1486 Finding-5 concurrent flip+delete and must propagate to the
        caller. A file that is genuinely PRESENT but malformed (bad JSON,
        non-dict root, missing ``id``) is a data-integrity fault and fails
        LOUD (Messi #13) -- never silently skipped.
        """
        try:
            with open(str(vector_file), "r") as file_handle:
                data: Dict[str, Any] = json.load(file_handle)
        except json.JSONDecodeError as exc:
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(vector_file)!r} is not valid "
                f"JSON during scroll hydration ({exc}); refusing to "
                f"silently drop it from a paginated scroll"
            ) from exc

        if not isinstance(data, dict):
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(vector_file)!r} has a non-dict "
                f"JSON root ({type(data).__name__}) during scroll "
                f"hydration; refusing to silently drop it from a "
                f"paginated scroll"
            )

        if "id" not in data:
            raise ScrollDataIntegrityError(
                f"legacy vector file {str(vector_file)!r} has no 'id' "
                f"field during scroll hydration; refusing to silently "
                f"drop it from a paginated scroll"
            )
        return data

    def _build_sharded_json_scroll_point(
        self,
        vector_file: Path,
        data: Dict[str, Any],
        with_payload: bool,
        with_vectors: bool,
        expected_dim: Optional[int],
    ) -> Dict[str, Any]:
        """Build ONE scroll-result point dict from an already-loaded legacy
        record, extracted UNCHANGED (behaviorally) from the pre-#1575
        inline loop.
        """
        point: Dict[str, Any] = {"id": data["id"]}

        if with_payload:
            # Payload should always exist in new format
            point["payload"] = data.get("payload", {})

        if with_vectors:
            if "vector" not in data:
                raise ScrollDataIntegrityError(
                    f"legacy vector file {str(vector_file)!r} is missing "
                    f"the required 'vector' field during scroll hydration "
                    f"(with_vectors=True); refusing to silently drop it "
                    f"from a paginated scroll"
                )
            # Bug #1488 (Codex ITEM 1 tail): the field is PRESENT, but a
            # present-but-malformed vector (null/string/object/empty,
            # non-numeric element, NaN/Inf, or wrong dimension) must fail
            # LOUD too -- never be returned as a silently wrong value.
            assert expected_dim is not None  # set whenever with_vectors
            self._validate_scroll_vector(data["vector"], expected_dim, str(vector_file))
            point["vector"] = data["vector"]

        return point

    def _hydrate_sharded_json_scroll_page(
        self,
        id_to_file: Dict[str, Path],
        sorted_ids: List[str],
        start_idx: int,
        limit: int,
        filter_func: Optional[Any],
        with_payload: bool,
        with_vectors: bool,
        expected_dim: Optional[int],
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Bug #1575 Part B: hydrate up to ``limit`` filter-matching legacy
        vector files starting at ``start_idx`` in ``sorted_ids``.

        Codex-15 MEDIUM (regression, preserved): iterates candidates in
        sorted-id order until either ``limit`` MATCHING rows are collected
        OR candidates are exhausted -- NEVER slices before filtering (which
        previously returned an empty page with a non-null cursor when a
        page's sliced candidates all failed the filter while later
        candidates still matched).

        A mid-hydration ``FileNotFoundError`` (Bug #1486 Finding 5) is
        propagated UNCHANGED -- the caller uses it to detect a possibly
        stale cached ``id_to_file`` map. Returns
        ``(points, last_examined_idx)``.
        """
        points: List[Dict[str, Any]] = []
        last_examined_idx = -1
        _scan_idx = start_idx
        while _scan_idx < len(sorted_ids) and len(points) < limit:
            vector_file = id_to_file[sorted_ids[_scan_idx]]
            last_examined_idx = _scan_idx
            _scan_idx += 1

            data = self._load_sharded_json_scroll_record(vector_file)
            point = self._build_sharded_json_scroll_point(
                vector_file, data, with_payload, with_vectors, expected_dim
            )

            # Apply pre-parsed filter (compiled once by the caller). Bug
            # #1488 (Codex Medium, Messi #13): evaluate against the REAL
            # hydrated payload (``data``), never ``point``'s payload which is
            # OMITTED ({}) when with_payload=False -- with_payload controls
            # only what is RETURNED, never what the filter sees.
            if filter_func is not None:
                if not filter_func(data.get("payload", {})):
                    continue

            points.append(point)

        return points, last_examined_idx

    def scroll_points(
        self,
        collection_name: str,
        limit: int = 100,
        with_payload: bool = True,
        with_vectors: bool = False,
        offset: Optional[str] = None,
        filter_conditions: Optional[Dict[str, Any]] = None,
        subdirectory: Optional[str] = None,
    ) -> tuple:
        """Scroll through points in collection with pagination.

        Args:
            collection_name: Name of the collection
            limit: Maximum number of points to return
            with_payload: Include payload in results
            with_vectors: Include vectors in results
            subdirectory: Optional subdirectory path within base_path (e.g.
                    "multimodal_index"). When None, falls back to the
                    active-indexing subdirectory recorded for this collection
                    (``_active_subdirectories``) so an in-flight indexing
                    session resolves the same nested path. Threaded through the
                    existence check, PathIndex lookup, metadata read, and BOTH
                    layout branches so a nested ``<subdirectory>/<collection>``
                    collection is scrollable (Codex-15 MEDIUM).
            offset: Pagination cursor from a previous page's next_offset. Bug
                    #1488: this is a stable, layout-INDEPENDENT point-id (both
                    the SHARDED_JSON and CHUNKS_DB branches iterate the same
                    sorted point-id order), so a cursor issued under one layout
                    resumes correctly after a concurrent flip to the other. A
                    legacy path-format cursor is translated, never silently
                    reset to offset 0. Used by the rglob/chunks.db fallback path
                    only; the path-index fast path always returns all matching
                    points without pagination since per-file point counts are
                    small, typically 1-10.
            filter_conditions: Optional filter conditions

        Returns:
            Tuple of (points_list, next_offset)

        Performance note:
            When filter_conditions contains a path equality clause
            ({"key": "path", "match": {"value": X}}), the method uses the
            persistent PathIndex to resolve matching point IDs in O(1) instead
            of walking the entire collection tree via rglob.  Other filter
            shapes fall through to the original rglob path (safety valve).
        """
        # Bug #1488 (Codex Low, Messi #13): a non-positive limit has no valid
        # continuation math (an empty page would index page_ids[-1]) and no
        # existing caller/test relies on limit<=0 -- reject it loudly rather
        # than silently returning a malformed/empty page.
        if limit <= 0:
            raise ValueError(
                f"scroll_points limit must be a positive integer, got {limit!r}"
            )

        # Codex-15 MEDIUM: resolve the subdirectory ONCE. An explicit caller arg
        # wins (mirrors search()'s explicit ``subdirectory`` param, the only
        # convention that survives ``end_indexing`` clearing the active-session
        # map); otherwise fall back to the active-indexing subdirectory so an
        # in-flight session resolves the same nested path. Threaded through the
        # existence check, PathIndex lookup, metadata read, and BOTH layout
        # branches so a nested ``<subdirectory>/<collection>`` collection (e.g.
        # ``multimodal_index/<coll>``) is scrollable instead of returning
        # ``([], None)`` from a top-level-only existence check.
        if subdirectory is None:
            subdirectory = self._active_subdirectories.get(collection_name)

        if not self.collection_exists(collection_name, subdirectory):
            return [], None

        # --- Fast path: path equality filter via PathIndex ---
        target_path = self._extract_path_filter(filter_conditions)
        if target_path is not None:
            # Keyed via _id_cache_key using the already-resolved subdirectory
            # so a nested-subdirectory scroll never reuses/collides with a
            # bare-name top-level collection's cached PathIndex (Codex NEW
            # Finding 2).
            _scroll_path_cache_key = self._id_cache_key(collection_name, subdirectory)
            # Ensure path index is loaded (or lazily rebuilt if absent)
            with self._path_index_lock:
                if _scroll_path_cache_key not in self._path_indexes:
                    loaded = PathIndex.load(
                        self._get_collection_path(
                            collection_name,
                            subdirectory,
                        )
                        / "path_index.bin"
                    )
                    self._path_indexes[_scroll_path_cache_key] = loaded
                path_index = self._path_indexes[_scroll_path_cache_key]
                # Detect legacy collection: PathIndex empty but collection may have files
                needs_rebuild = not path_index._path_index
                # Bug #1575 round 7 structural mirror of
                # _rebuild_and_repair_path_index()'s before/after-snapshot
                # prune fix: snapshot the live object NOW, under the SAME
                # lock acquisition as the needs_rebuild check, so a point
                # removed by a concurrent delete_points() while the
                # (unlocked) disk scan below runs can be pruned back out
                # even if the scan's possibly-stale view still has it.
                #
                # KNOWN RESIDUAL GAP (deliberately not closed by this
                # mirror, empirically verified -- see
                # test_filesystem_vector_store_1575_scroll_points_rebuild_
                # merge_prune.py): needs_rebuild is True ONLY when
                # path_index._path_index is EMPTY at this exact moment, so
                # before_snapshot below is ALWAYS {} for every real firing
                # of this branch -- there is nothing in it for the prune
                # step to ever find. A point that is added to the live
                # object AND removed again entirely within the scan
                # window -- starting from this genuinely-empty cache --
                # can therefore still be resurrected by a stale disk read;
                # a before/after delta rooted at an empty T0 cannot detect
                # a removal of something it never saw exist. This mirror
                # is still worth having: it is correct in general (closes
                # the race for a live object that is non-empty when the
                # scan starts) and makes real if this gate's precondition
                # ever changes.
                before_snapshot = path_index.snapshot() if needs_rebuild else None

            # Release lock before any I/O; rebuild walks disk only on first call
            if needs_rebuild:
                rebuilt = self._rebuild_path_index_from_disk(
                    collection_name, subdirectory
                )
                # M2 fix (merge, not swap) + round-7 prune mirror -- shared
                # with _rebuild_and_repair_path_index() via
                # _merge_rebuilt_path_index_with_prune(). Any upsert_points
                # calls that ran concurrently during the rglob walk added to
                # the live PathIndex; a swap would discard those additions.
                # merge_from uses add_point (set semantics) so re-adding
                # existing entries is a no-op.
                with self._path_index_lock:
                    path_index = self._merge_rebuilt_path_index_with_prune(
                        _scroll_path_cache_key, before_snapshot, rebuilt
                    )

            # Get point IDs for the requested path (copy under lock for safety)
            with self._path_index_lock:
                target_ids = path_index.get_point_ids(target_path)

            # MEDIUM fix (Codex, Messi #13): the PathIndex is used ONLY to
            # obtain candidate ids. Evaluate the COMPLETE ORIGINAL filter (every
            # clause, INCLUDING the path clauses) against each candidate's REAL
            # hydrated payload -- the SAME predicate the general rglob path uses
            # (``_parse_filter``). A reduced, path-STRIPPED filter produced two
            # wrong-row bugs: (a) a CONTRADICTORY {"must":[path==a.py,
            # path==b.py]} filter matched nothing yet the stripped variant
            # returned the a.py candidate; (b) a STALE PathIndex entry (indexed
            # path no longer equals the file's real on-disk path) returned a row
            # whose real path differs from the requested path. Evaluating the
            # full filter against the real payload drops both and self-heals
            # stale PathIndex entries (a candidate whose hydrated path no longer
            # matches is correctly excluded). This changes ONLY which filter is
            # evaluated, never the O(1) PathIndex candidate-discovery mechanism.
            full_filter = self._parse_filter(filter_conditions)

            # Bug #1488 (Codex, Messi #13): the fast path must obey the SAME
            # contract as the general paginated path -- paginate with the stable
            # sorted-id continuation cursor, FAIL LOUD on an enumerated-but-
            # unhydratable id, and validate returned vectors. The prior
            # ``result_points[:limit], None`` truncation emitted NO continuation
            # cursor, so with more matching ids than ``limit`` the remainder was
            # PERMANENTLY unreachable. The id-based cursor is layout-independent
            # (same sorted real-id order as both general branches), so a cursor
            # issued here resumes correctly even after a concurrent layout flip.
            import bisect

            sorted_ids = sorted(target_ids)  # deterministic, layout-independent
            cursor = self._resolve_scroll_cursor(offset, sorted_ids)
            start_idx = 0 if cursor is None else bisect.bisect_right(sorted_ids, cursor)

            # Read the expected dimension ONLY when vectors are returned so the
            # with_vectors=False path stays byte-identical (no new I/O). Pass
            # the already-resolved ``subdirectory`` explicitly (Codex-16
            # Finding 4) so a nested collection's dimension resolves
            # correctly outside an active indexing session.
            expected_dim_fp: Optional[int] = (
                self._get_vector_size(collection_name, subdirectory)
                if with_vectors
                else None
            )

            # Codex-15 MEDIUM (regression): iterate candidates in sorted order
            # until either ``limit`` MATCHING rows are collected OR candidates
            # are exhausted -- NEVER slice ``candidates[start:start+limit]``
            # before filtering. Slicing-then-filtering returned an EMPTY page
            # with a NON-null cursor whenever a page's sliced candidates all
            # failed the filter while later candidates still matched; real
            # callers treat an empty page as TERMINAL, so those later matches
            # were permanently DROPPED (Messi #13). The continuation cursor is
            # based on the LAST EXAMINED candidate, so a page is empty ONLY when
            # no further matches exist (then the cursor is None / terminal).
            result_points: List[Dict[str, Any]] = []
            last_examined_idx_fp = -1
            idx_fp = start_idx
            while idx_fp < len(sorted_ids) and len(result_points) < limit:
                pid = sorted_ids[idx_fp]
                last_examined_idx_fp = idx_fp
                idx_fp += 1
                point_data = self.get_point(
                    pid, collection_name, subdirectory=subdirectory
                )
                if point_data is None:
                    # The id was enumerated by the PathIndex but cannot be
                    # hydrated from either layout (get_point already re-resolves
                    # a concurrent CHUNKS_DB flip on a legacy miss). Silently
                    # dropping it would lose a row AND could emit a terminal
                    # None cursor falsely presenting a complete traversal --
                    # fail LOUD instead (Messi #13).
                    raise ScrollDataIntegrityError(
                        f"path-index enumerated point-id {pid!r} has no "
                        f"hydratable row in collection {collection_name!r}; "
                        f"refusing to silently drop an enumerated id from a "
                        f"paginated scroll"
                    )
                # Evaluate the COMPLETE original filter against the REAL
                # hydrated payload, independent of with_payload (which controls
                # only what is RETURNED, never what the filter sees).
                if not full_filter(point_data.get("payload", {})):
                    continue
                point: Dict[str, Any] = {"id": point_data["id"]}
                if with_payload:
                    point["payload"] = point_data.get("payload", {})
                if with_vectors:
                    vector_value = point_data.get("vector", [])
                    if hasattr(vector_value, "tolist"):
                        vector_value = vector_value.tolist()
                    # Validate with the SAME gate both general branches use so
                    # every scroll path rejects an identical malformed shape.
                    assert expected_dim_fp is not None
                    self._validate_scroll_vector(
                        vector_value,
                        expected_dim_fp,
                        f"{pid} (path-index fast path)",
                    )
                    point["vector"] = vector_value
                result_points.append(point)

            # Continuation cursor on the LAST EXAMINED candidate: only emit one
            # when a full page of matches was collected AND unexamined candidates
            # remain. When candidates are exhausted the cursor is None (terminal)
            # even if fewer than ``limit`` rows matched -- a genuinely-empty page
            # therefore always carries a terminal None cursor.
            next_offset_fp: Optional[str] = None
            if len(result_points) == limit and last_examined_idx_fp + 1 < len(
                sorted_ids
            ):
                next_offset_fp = self._encode_scroll_cursor(
                    sorted_ids[last_examined_idx_fp]
                )
            return result_points, next_offset_fp

        # --- Safety valve: fall through to original rglob path ---
        # Codex-15 MEDIUM: resolve via _get_collection_path with the resolved
        # subdirectory (not a hardcoded base_path/collection_name) so a nested
        # multimodal collection is scanned at its real ``<subdirectory>/<coll>``
        # location instead of a non-existent top-level path.
        collection_path = self._get_collection_path(collection_name, subdirectory)

        from code_indexer.storage.shared.chunk_layout import (
            ChunkLayout,
            resolve_chunk_layout,
        )

        # Story #1456 AC3: CHUNKS_DB collections paginate over chunks.db
        # (sorted point_ids as the opaque offset cursor) instead of
        # rglob-scanning vector_*.json files.
        if self._is_chunks_db_collection(collection_name, collection_path):
            return self._scroll_points_chunks_db(
                collection_name,
                collection_path,
                limit,
                with_payload,
                with_vectors,
                offset,
                filter_conditions,
                subdirectory=subdirectory,
            )

        # Bug #1486 (Codex Finding 4): re-resolve the committed discriminator
        # HERE, before the legacy rglob scan. A concurrent server-mode fleet
        # migration may have flipped the discriminator to CHUNKS_DB and deleted
        # the legacy vector_*.json files in the window between the gate above
        # and this scan -- which would otherwise walk an empty tree and return
        # []. The flip is the atomic swap point (committed durably BEFORE legacy
        # deletion), so re-resolving here observes a consistent CHUNKS_DB
        # collection. A permanently-SHARDED_JSON collection re-resolves to
        # SHARDED_JSON (one cheap top-level JSON key read) and falls through to
        # the unchanged rglob path below.
        if resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB:
            return self._scroll_points_chunks_db(
                collection_name,
                collection_path,
                limit,
                with_payload,
                with_vectors,
                offset,
                filter_conditions,
                subdirectory=subdirectory,
            )

        # Legacy SHARDED_JSON scan. Bug #1486 Finding 5 (TOCTOU): the pre-scan
        # re-resolve above read SHARDED_JSON, but a concurrent server-mode
        # migration may flip the discriminator and delete the legacy
        # vector_*.json files DURING the rglob walk / per-file open below. A
        # per-file open() (or the rglob walk itself) then raises
        # FileNotFoundError, and even without one the rglob may find zero
        # (already-deleted) files and produce an empty/partial page. Both are
        # absorbed: on a FileNotFoundError, or when the discriminator now reads
        # CHUNKS_DB after the scan, re-resolve and dispatch to the chunk-store
        # scroll so the page is never silently empty/partial. A genuinely
        # missing file on a still-SHARDED_JSON collection is a real error and is
        # re-raised (fail loud, Messi #13).
        points: List[Dict[str, Any]] = []
        next_offset: Optional[str] = None
        try:
            import bisect

            # Bug #1575 Part B: a NEW scroll (offset=None) always rebuilds
            # the id_to_file enumeration fresh (byte-identical cost to
            # before -- one page, one full scan) and write-through
            # populates the per-collection session cache; a CONTINUATION
            # call (offset given) reuses that cache instead of repeating
            # the O(N) rglob+parse-every-file rebuild on every page. A
            # cache-derived map that has gone stale (a point deleted, or
            # the collection migrated, since it was built) is detected as a
            # FileNotFoundError while hydrating a page SERVED FROM THE
            # CACHE (``was_cache_hit``) and self-healed by rebuilding fresh
            # and retrying EXACTLY ONCE: the retry's ``was_cache_hit`` is
            # always False (a fresh build, never itself a cache hit), so a
            # second failure always propagates to the unchanged Finding-5
            # handling below -- this loop can never iterate more than twice.
            read_cache = offset is not None
            # Bug #1579: a pre-existing on-disk shifted/duplicate point_id
            # (two vector_*.json files sharing the same stored id, e.g. from
            # a collection built before the upsert_points write-path fix)
            # makes _build_sharded_json_scroll_index raise
            # ScrollDataIntegrityError. Rather than permanently failing the
            # scroll (which propagates through smart_indexer.py's fail-fast
            # reconcile and kills `cidx index --reconcile` for the repo
            # forever), attempt ONE self-heal via
            # repair_duplicate_and_shifted_points before giving up -- bounded
            # exactly like the FileNotFoundError self-heal above (the retry
            # always rebuilds fresh, so a second failure always propagates).
            dedup_repair_attempted = False
            while True:
                try:
                    id_to_file, was_cache_hit = self._get_sharded_json_scroll_index(
                        collection_name,
                        collection_path,
                        subdirectory,
                        read_cache=read_cache,
                    )
                except ScrollDataIntegrityError:
                    if dedup_repair_attempted:
                        raise
                    from code_indexer.storage.shared.collection_dedup_repair import (
                        repair_duplicate_and_shifted_points,
                    )

                    self.logger.warning(
                        "Bug #1579: scroll_points hit a duplicate/malformed "
                        "point_id while enumerating collection %r -- "
                        "attempting a one-shot dedup repair before failing "
                        "the scroll.",
                        collection_name,
                    )
                    # DedupRepairAmbiguousError (e.g. a malformed record the
                    # repair refuses to touch) is deliberately NOT caught
                    # here -- it is a more specific, more actionable error
                    # than the ScrollDataIntegrityError it would otherwise
                    # mask, and must propagate to the caller unchanged.
                    repair_duplicate_and_shifted_points(collection_path)
                    dedup_repair_attempted = True
                    read_cache = False
                    continue
                sorted_ids = sorted(id_to_file)

                # Resume strictly AFTER the resolved real-id cursor via bisect
                # (a legacy path/token cursor is translated first, garbage
                # fails loud, and a self-describing cursor whose point was
                # deleted resolves to the first id greater than it -- correct
                # continuation, no dup, no gap).
                cursor = self._resolve_scroll_cursor(offset, sorted_ids)
                start_idx = (
                    0 if cursor is None else bisect.bisect_right(sorted_ids, cursor)
                )

                # Fix C: Parse filter ONCE before the loop (was inside
                # per-file loop). Avoids O(N) repeated filter compilation
                # for collections with thousands of files.
                filter_func = None
                if filter_conditions:
                    filter_func = self._parse_filter(filter_conditions)

                # Bug #1488 (Codex ITEM 1 tail): the expected vector
                # dimension for this collection, read ONLY when vectors are
                # actually returned so the ``with_vectors=False`` path stays
                # byte-identical (no new I/O). ``_get_vector_size`` is cached
                # and is the SAME dimension the write path (upsert_points)
                # validated the stored vector against. The already-resolved
                # ``subdirectory`` is passed explicitly (Codex-16 Finding 4)
                # so a nested collection's dimension resolves correctly
                # outside an active indexing session.
                expected_dim: Optional[int] = (
                    self._get_vector_size(collection_name, subdirectory)
                    if with_vectors
                    else None
                )

                try:
                    (
                        points,
                        last_examined_idx,
                    ) = self._hydrate_sharded_json_scroll_page(
                        id_to_file,
                        sorted_ids,
                        start_idx,
                        limit,
                        filter_func,
                        with_payload,
                        with_vectors,
                        expected_dim,
                    )
                except FileNotFoundError:
                    if was_cache_hit:
                        read_cache = False
                        continue
                    raise

                # Calculate next offset: a self-describing REAL point-id
                # cursor (Bug #1488), never a filesystem path or filename
                # token, so the next page resumes correctly even if the
                # collection flips to CHUNKS_DB before it is requested.
                # Codex-15 MEDIUM: the cursor is based on the LAST EXAMINED
                # candidate (not a pre-filter slice end) and is only emitted
                # when a full page of matches was collected AND unexamined
                # candidates remain -- so a genuinely-empty page always
                # carries a terminal None cursor.
                if len(points) == limit and last_examined_idx + 1 < len(sorted_ids):
                    next_offset = self._encode_scroll_cursor(
                        sorted_ids[last_examined_idx]
                    )
                break
        except FileNotFoundError:
            # A legacy vector_*.json (or a shard subdir walked by rglob) vanished
            # mid-scan. Re-resolve: if the flip has landed, dispatch to the chunk
            # store; otherwise it is a genuine missing file -> fail loud.
            if resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB:
                return self._scroll_points_chunks_db(
                    collection_name,
                    collection_path,
                    limit,
                    with_payload,
                    with_vectors,
                    offset,
                    filter_conditions,
                    subdirectory=subdirectory,
                )
            raise
        else:
            # Scan completed without a vanish, but the flip may have landed just
            # after the pre-scan resolve, so rglob found zero (deleted) legacy
            # files -> an empty/partial page. Re-check the discriminator; on a
            # detected flip, dispatch to the chunk store instead of returning it.
            if resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB:
                return self._scroll_points_chunks_db(
                    collection_name,
                    collection_path,
                    limit,
                    with_payload,
                    with_vectors,
                    offset,
                    filter_conditions,
                    subdirectory=subdirectory,
                )

        return points, next_offset

    def search(
        self,
        query: str,
        embedding_provider: Any,
        collection_name: str = "",
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter_conditions: Optional[Dict[str, Any]] = None,
        return_timing: bool = False,
        lazy_load: bool = False,
        prefetch_limit: Optional[int] = None,
        ef: int = 50,
        subdirectory: Optional[str] = None,
        parallel_executor: Optional["Executor"] = None,
        no_embedding_cache_shortcut: bool = False,
        precomputed_query_vector: Optional[List[float]] = None,
        temporal_chunk_type: Optional[str] = None,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
        """Search for similar vectors using parallel execution of index loading and embedding generation.

        This method ALWAYS executes in parallel mode:
        - Thread 1: Load HNSW index + ID mapping
        - Thread 2: Generate query embedding (skipped when precomputed_query_vector is supplied)
        - Wait for both, then perform search

        Parallel execution reduces query latency by 350-467ms by overlapping I/O-bound
        index loading with CPU-bound embedding generation.

        Args:
            query: Query text for embedding generation (REQUIRED)
            embedding_provider: Provider with get_embedding() method (REQUIRED)
            collection_name: Name of the collection
            limit: Maximum number of results
            score_threshold: Minimum similarity score (0-1)
            filter_conditions: Optional filter conditions for payload
            return_timing: If True, return tuple of (results, timing_dict)
            lazy_load: If True, load payloads on-demand with early exit (optimization for restrictive filters)
            prefetch_limit: How many candidate IDs to fetch from HNSW (default: limit * 2 or limit * 15 for lazy_load)
            subdirectory: Optional subdirectory path (e.g., "multimodal_index")
            parallel_executor: Optional SHARED, long-lived ThreadPoolExecutor for the
                index-load || embedding fan-out. SERVER PATH: the server injects its
                app-level query executor here so concurrent requests reuse threads
                instead of each creating/destroying a per-request pool (which serialized
                on CPython's process-wide _global_shutdown_lock). CLI/SOLO/DAEMON PATH:
                leave None — a per-call ThreadPoolExecutor(max_workers=2) is created and
                cleaned up exactly as before (single-user, no concurrency/churn problem).
            precomputed_query_vector: Optional pre-computed embedding vector.
                When supplied (omni per-repo reuse path, Bug #1148), the
                generate_embedding() step is skipped entirely: coalesced_query_embedding
                is NOT called, no cache metric event fires, and no get_provider_name()
                call is made on the embedding_provider.  The supplied vector is used
                directly for the HNSW nearest-neighbour search.

        Returns:
            List of results with id, score, payload (including content), and staleness
            If return_timing=True: Tuple of (results, timing_dict)

        Raises:
            ValueError: If query or embedding_provider not provided
            RuntimeError: If index loading or embedding generation fails
        """
        import time
        from concurrent.futures import ThreadPoolExecutor

        # Story #1493 AC2: validate early -- fail loud on a bad caller
        # value rather than silently misclassifying every candidate as
        # "opposite chunk type" later in the hydration loop.
        if temporal_chunk_type is not None and temporal_chunk_type not in (
            "commit_message",
            "commit_diff",
        ):
            raise ValueError(
                f"temporal_chunk_type must be 'commit_message' or "
                f"'commit_diff', got {temporal_chunk_type!r}"
            )

        timing: Dict[str, Any] = {}

        collection_path = self._get_collection_path(collection_name, subdirectory)

        if not self.collection_exists(collection_name, subdirectory):
            return ([], timing) if return_timing else []

        # Load metadata to get vector size.
        # Story #1492 AC1 (Finding C1, SEVERE): fetched via the SAME shared
        # mtime-keyed CollectionMetaCache collection_exists() just consulted
        # a moment ago -- in the common case (file unchanged) this is a
        # cache HIT (zero additional read/parse), and every downstream
        # consumer below (is_stale(), the first resolve_chunk_layout() call)
        # reuses this SAME dict instead of independently re-reading the
        # file.
        #
        # A missing collection_meta.json (TOCTOU race, half-written clone,
        # NFS hiccup) is a LOCAL storage failure — not a provider failure —
        # so a None result here (this call re-stats fresh; it is NOT
        # reusing a stale snapshot) is re-raised as LocalIndexNotFoundError
        # so the parallel-dispatch handler in semantic_query_manager.py
        # skips sin-binning the embedding provider, identically to the
        # pre-#1492 FileNotFoundError branch. Deliberate, LOUD (never
        # silent) reclassification: the narrower TOCTOU sub-case where the
        # file instead becomes malformed/corrupt in that same window (which
        # used to propagate an uncaught json.JSONDecodeError) is now ALSO
        # None from the cache (fail-closed) and takes this SAME well-typed
        # LocalIndexNotFoundError path, rather than an ad hoc unhandled
        # parse exception.
        cached_meta = self._collection_meta_cache.get(collection_path)
        if cached_meta is None:
            raise LocalIndexNotFoundError(
                f"collection_meta.json missing for collection '{collection_name}'. "
                f"Run: cidx index --rebuild-index"
            )
        metadata = cached_meta

        # === CHECK HNSW STALENESS ===
        # Bug #668: NEVER rebuild HNSW during a query. Rebuilding is the indexer's
        # responsibility (cidx index / cidx watch). Queries must use whatever index
        # exists on disk. If stale and bin missing → return empty. If stale and
        # bin exists → use it as-is with a warning.
        from .hnsw_index_manager import HNSWIndexManager

        vector_size = metadata.get("vector_size", 1536)
        hnsw_manager = HNSWIndexManager(vector_dim=vector_size, space="cosine")

        if hnsw_manager.is_stale(collection_path, cached_meta=metadata):
            if not hnsw_manager.index_exists(collection_path):
                log_hnsw_stale(
                    self.logger,
                    collection_path=collection_path,
                    collection_name=collection_name,
                    alias=None,  # Alias not threaded through this call path; collection_path is sufficient for operator triage.
                )
                return ([], timing) if return_timing else []
            self.logger.warning(
                f"HNSW index is stale for '{collection_name}'. "
                "Querying existing index as-is. Run 'cidx index' to rebuild."
            )

        # === PARALLEL EXECUTION (always) ===

        # Story #1456 AC7 (critical, binding design decision): resolved HERE
        # on the MAIN/calling thread, BEFORE the worker closure is defined,
        # so the worker never performs its own layout resolution or any
        # id-index/chunk-store touching for CHUNKS_DB collections.
        from code_indexer.storage.shared.chunk_layout import (
            ChunkLayout,
            resolve_chunk_layout,
        )

        # Story #1492 AC1: pass the SAME already-fetched metadata dict so
        # this first resolve does not re-read the file (the SECOND
        # resolve_chunk_layout() call below, after the parallel section,
        # deliberately re-fetches via the cache instead -- see its comment
        # for why that one MUST re-stat).
        _search_chunk_layout = resolve_chunk_layout(
            collection_path, cached_meta=metadata
        )

        def load_index():
            """Load HNSW and ID indexes in parallel thread.

            Story #526: If hnsw_index_cache is configured, use cached HNSW index
            for 1800x performance improvement (~277ms → <1ms).
            """
            # Load HNSW index (with caching if available)
            t_hnsw = time.time()

            # Story #526: Use cache if available
            if self.hnsw_index_cache is not None:
                # Cache key is collection_path (unique per repository), plus
                # Story #1458 AC11's chunks_db layout-discriminator token
                # (so a post-consolidation read at the same path is a
                # structural miss) and activation_id token (so a deactivate-
                # then-reactivate clone at the same path is a structural
                # miss too).
                cache_key = self._activation_scoped_cache_key(
                    str(collection_path.resolve()),
                    chunk_layout_token=_search_chunk_layout.value,
                )

                def hnsw_loader():
                    """Loader function for cache miss.

                    Bug #1236 GAP A: a corrupt .bin raises RuntimeError from hnswlib.
                    Reclassify as LocalIndexNotFoundError so the parallel-dispatch
                    handler in semantic_query_manager.py does NOT sin-bin the provider.
                    """
                    from .hnsw_index_manager import _is_corrupt_index_error as _cic

                    try:
                        index = hnsw_manager.load_index(
                            collection_path, max_elements=100000
                        )
                    except RuntimeError as _exc:
                        if _cic(_exc):
                            raise LocalIndexNotFoundError(
                                f"HNSW index is corrupt for collection "
                                f"'{collection_name}'. "
                                f"Run: cidx index --rebuild-index"
                            ) from _exc
                        raise
                    # Load ID mapping from metadata for cache entry
                    id_mapping = hnsw_manager._load_id_mapping(collection_path)
                    return index, id_mapping

                # Get or load from cache.
                # EVO-64244 Facet 2: pass the concrete hnsw_index.bin path so a
                # rebuilt index (atomic replace on re-index) invalidates the
                # stale in-RAM cache entry instead of being served for the TTL.
                hnsw_index, _cached_id_mapping = self.hnsw_index_cache.get_or_load(
                    cache_key,
                    hnsw_loader,
                    index_file=collection_path / hnsw_manager.INDEX_FILENAME,
                )
            else:
                # No cache - load directly (original behavior).
                # Bug #1236 GAP A: a corrupt .bin raises RuntimeError from hnswlib.
                # Reclassify as LocalIndexNotFoundError so the parallel-dispatch handler
                # in semantic_query_manager.py does NOT sin-bin the embedding provider.
                from .hnsw_index_manager import _is_corrupt_index_error as _cic

                try:
                    hnsw_index = hnsw_manager.load_index(
                        collection_path, max_elements=100000
                    )
                except RuntimeError as _exc:
                    if _cic(_exc):
                        raise LocalIndexNotFoundError(
                            f"HNSW index is corrupt for collection '{collection_name}'. "
                            f"Run: cidx index --rebuild-index"
                        ) from _exc
                    raise

            hnsw_load_ms = (time.time() - t_hnsw) * 1000

            # Load ID index in same thread (parallel with embedding generation).
            # Story #1456 AC7 (critical, binding): CHUNKS_DB collections skip
            # this ENTIRELY -- no _load_id_index() call, no id-index/chunk-store
            # path resolution in the worker thread. Point-id resolution for
            # CHUNKS_DB happens exclusively via the chunk store, opened AFTER
            # this worker's .result() returns to the main/calling thread.
            t_id = time.time()
            if _search_chunk_layout == ChunkLayout.CHUNKS_DB:
                id_index = None
            elif self.id_index_cache is not None:
                # Bug #1078: use shared cross-query cache (server mode).
                # Story #1458 AC11: same chunks_db-layout-token + activation_id
                # -scoped key as the HNSW cache above.
                id_index = self.id_index_cache.get_or_load(
                    self._activation_scoped_cache_key(
                        str(collection_path.resolve()),
                        chunk_layout_token=_search_chunk_layout.value,
                    ),
                    lambda: self._load_id_index(collection_name, subdirectory),
                )
            else:
                with self._id_index_lock:
                    cache_key = self._id_cache_key(collection_name, subdirectory)
                    if cache_key not in self._id_index:
                        self._id_index[cache_key] = self._load_id_index(
                            collection_name, subdirectory
                        )
                    id_index = self._id_index[cache_key]
            id_load_ms = (time.time() - t_id) * 1000

            return hnsw_index, id_index, hnsw_load_ms, id_load_ms

        def generate_embedding():
            """Generate query embedding in parallel thread.

            Bug #1078: the HTTP call is gated through the concurrency governor so
            at most K concurrent serving-path embedding requests reach VoyageAI/Cohere.
            The HNSW-load worker (load_index) runs freely — it makes no provider calls.

            Story #1110 (S6 Chunk B): allocate _audit_ctx dict and thread it into
            coalesced_query_embedding.  On a sampled cache hit the function populates
            the dict in-place; the 3-tuple return carries it back to search().
            """
            _audit_ctx: Dict[str, Any] = {}
            t0 = time.time()
            embedding, _embed_meta = coalesced_query_embedding(
                embedding_provider,
                query,
                no_embedding_cache_shortcut=no_embedding_cache_shortcut,
                audit_ctx=_audit_ctx,
            )
            embedding_time_ms = (time.time() - t0) * 1000
            # Story #1159: return _embed_meta so the MAIN THREAD can write to
            # _search_event_ctx.  ContextVar is not visible inside worker threads
            # (Python 3.9 ThreadPoolExecutor does not propagate context), so the
            # write must happen in the calling thread after the future resolves.
            return embedding, embedding_time_ms, _audit_ctx, _embed_meta

        # Execute both operations in parallel.
        #
        # Anti-fallback explicit branch (Messi #2): the SERVER passes a shared,
        # long-lived executor; the CLI/solo/daemon does NOT. This is intentionally
        # two readable paths, never a silent global.
        #
        # SERVER PATH (parallel_executor injected): submit to the shared pool and
        # gather — DO NOT shut it down (it is owned by the app lifespan). This
        # eliminates the per-request ThreadPoolExecutor create/destroy churn that
        # serialized concurrent queries on CPython's _global_shutdown_lock.
        #
        # CLI PATH (parallel_executor is None): single-user, not concurrent — keep
        # the original per-call ThreadPoolExecutor(max_workers=2) context manager,
        # so CLI behaviour and the CLI startup import budget are unchanged.
        #
        # PRECOMPUTED PATH (Bug #1148 omni per-repo reuse): when precomputed_query_vector
        # is supplied, generate_embedding() is skipped entirely.  Only load_index() runs
        # (in the thread pool on the server path, or inline on the CLI path).
        # coalesced_query_embedding is never called — no get_provider_name(), no second
        # cache metric event, no AttributeError.
        parallel_start = time.time()
        if precomputed_query_vector is not None:
            # Precomputed path: skip generate_embedding(), use supplied vector directly.
            if parallel_executor is not None:
                hnsw_index, id_index, hnsw_load_ms, id_load_ms = (
                    parallel_executor.submit(load_index).result()
                )
            else:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    hnsw_index, id_index, hnsw_load_ms, id_load_ms = executor.submit(
                        load_index
                    ).result()
            query_vector: List[float] = precomputed_query_vector
            embedding_ms: float = 0.0
            audit_ctx: Dict[str, Any] = {}
        elif parallel_executor is not None:
            index_future = parallel_executor.submit(load_index)
            embedding_future = parallel_executor.submit(generate_embedding)
            # .result() re-raises any sub-task exception in the caller's thread —
            # identical exception propagation to the `with` form below.
            hnsw_index, id_index, hnsw_load_ms, id_load_ms = index_future.result()
            try:
                query_vector, embedding_ms, audit_ctx, _embed_meta = (
                    embedding_future.result()
                )
            except Exception:
                # Story #1293 S1b [A6]: a failed LIVE embedding attempt (e.g.
                # the failover primary provider) is recorded as a durable
                # outcome=error event BEFORE the exception propagates.
                if emit_embed_error_event is not None:
                    emit_embed_error_event(embedding_provider.get_provider_name())
                raise
            # Story #1159: write embed metadata in main thread — ContextVar is not
            # propagated into ThreadPoolExecutor workers (Python 3.9).
            _write_embed_meta_to_event_ctx(
                _embed_meta, embedding_provider.get_provider_name()
            )
        else:
            with ThreadPoolExecutor(max_workers=2) as executor:
                # Submit both tasks
                index_future = executor.submit(load_index)
                embedding_future = executor.submit(generate_embedding)

                # Wait for both to complete and gather results
                hnsw_index, id_index, hnsw_load_ms, id_load_ms = index_future.result()
                try:
                    query_vector, embedding_ms, audit_ctx, _embed_meta = (
                        embedding_future.result()
                    )
                except Exception:
                    if emit_embed_error_event is not None:
                        emit_embed_error_event(embedding_provider.get_provider_name())
                    raise
            # Story #1159: write embed metadata in main thread.
            _write_embed_meta_to_event_ctx(
                _embed_meta, embedding_provider.get_provider_name()
            )

        # Bug #1486 (Codex Finding 4): re-resolve the committed chunk-layout
        # discriminator HERE, on the MAIN/calling thread, AFTER the parallel
        # .result() returns and BEFORE hydration branches. The entry snapshot
        # (_search_chunk_layout, taken before the parallel section) can go
        # stale: a concurrent server-mode fleet migration may have flipped the
        # discriminator to CHUNKS_DB and deleted the legacy vector_*.json /
        # id_index.bin files in the window between that snapshot and this point.
        # The flip is the atomic swap point and is committed durably BEFORE the
        # legacy files are deleted (collection_migration.py), so re-resolving
        # here observes a consistent, fully-valid CHUNKS_DB collection and
        # avoids returning empty/partial results down the stale SHARDED_JSON
        # branch. resolve_chunk_layout() is fail-closed and cheap (one top-level
        # JSON key read); a permanently-SHARDED_JSON collection re-resolves to
        # SHARDED_JSON with zero extra chunks.db probing.
        #
        # Claude Finding 3 (perf gate): the ONLY dangerous transition is
        # SHARDED_JSON -> CHUNKS_DB (migration only ADDS chunks.db and deletes
        # legacy AFTER the flip). A collection already CHUNKS_DB at the entry
        # snapshot can therefore NEVER transition back, so re-resolving it is
        # pure waste -- an extra open()+json.load() on the server query hot path.
        # Gate the re-resolve on the entry snapshot: reuse CHUNKS_DB directly
        # when it was already CHUNKS_DB at entry; only pay the re-resolve for a
        # SHARDED_JSON entry snapshot, which is the exact case the race fix must
        # still cover. Story #1456 AC7 is preserved: this re-resolve and the
        # ChunkStore open both run on the calling thread, never inside
        # load_index()'s worker closure.
        if _search_chunk_layout == ChunkLayout.CHUNKS_DB:
            _hydration_chunk_layout = ChunkLayout.CHUNKS_DB
        else:
            # Story #1492 AC1: routed through the SAME mtime-keyed cache.
            # This re-stats the file's CURRENT mtime on every call, so a
            # real concurrent flip (the exact race this re-resolve exists
            # to catch) is still a fresh reparse; an unchanged file is now
            # a cache HIT instead of an unconditional reparse.
            _hydration_chunk_layout = resolve_chunk_layout(
                collection_path,
                cached_meta=self._collection_meta_cache.get(collection_path),
            )

        # Story #1456 AC4/AC7: open the chunk store for hydration ONLY here,
        # on the MAIN thread -- NEVER inside load_index()'s worker closure.
        # sqlite3 connections are not safely shared across threads, which is
        # exactly why AC7 mandates this be resolved post-.result().
        # Story #1492 AC3: routed through the per-THREAD ChunkStoreThreadCache
        # instead of an unconditional fresh open() -- a repeat query against
        # the same unchanged mutable collection, served by the same worker
        # thread, reuses the already-open connection (no schema DDL /
        # dim-load / codec re-construction). Never shared across threads
        # (threading.local semantics) -- see chunk_store_cache.py.
        chunk_store_for_hydration: Optional[Any] = None
        if _hydration_chunk_layout == ChunkLayout.CHUNKS_DB:
            chunk_store_for_hydration = self._chunk_store_cache.get_or_open(
                collection_path / "chunks.db", str(collection_path)
            )

        # Calculate actual parallel execution time (wall clock)
        parallel_load_ms = (time.time() - parallel_start) * 1000

        # Record timing metrics
        timing["parallel_load_ms"] = parallel_load_ms  # Actual clock time
        timing["embedding_ms"] = embedding_ms  # For breakdown display
        timing["index_load_ms"] = hnsw_load_ms  # HNSW index load time
        timing["id_index_load_ms"] = id_load_ms  # ID index load time
        timing["parallel_execution"] = True

        # Calculate threading overhead
        # Max concurrent work = max(embedding, index_loads_combined)
        index_work_ms = hnsw_load_ms + id_load_ms
        max_concurrent_work_ms = max(embedding_ms, index_work_ms)
        overhead_ms = parallel_load_ms - max_concurrent_work_ms
        timing["parallel_overhead_ms"] = overhead_ms

        # Validate results
        if hnsw_index is None:
            raise LocalIndexNotFoundError(
                f"HNSW index not found for collection '{collection_name}'. "
                f"Run: cidx index --rebuild-index"
            )

        # === SEARCH LOGIC ===

        query_vec = np.array(query_vector)
        query_norm = np.linalg.norm(query_vec)

        if query_norm == 0:
            return ([], timing) if return_timing else []

        # Mark search path for timing metrics
        timing["search_path"] = "hnsw_index"

        # Determine how many candidates to fetch from HNSW
        # Use prefetch_limit if provided (for over-fetching with filters), otherwise limit * 2
        hnsw_k = prefetch_limit if prefetch_limit is not None else limit * 2

        # Query HNSW index
        t0 = time.time()
        candidate_ids, distances = hnsw_manager.query(
            index=hnsw_index,
            query_vector=query_vec,
            collection_path=collection_path,
            k=hnsw_k,  # Use prefetch_limit when provided for filter headroom
            ef=ef,  # HNSW query parameter - passed from search method
        )
        timing["hnsw_search_ms"] = (time.time() - t0) * 1000

        # Story #1110 (S6 Chunk B): deep-fidelity audit hook (fail-open).
        # Fires only when the coalesced embedding path sampled this request.
        # _run_deep_fidelity_audit is already fail-open internally; we also
        # guard externally so a bug in the import or the call never breaks search.
        if audit_ctx.get("sampled") and _run_deep_fidelity_audit is not None:
            try:
                _run_deep_fidelity_audit(
                    audit_ctx=audit_ctx,
                    hnsw_index=hnsw_index,
                    hnsw_manager=hnsw_manager,
                    collection_path=collection_path,
                    ef=ef,
                    primary_candidate_ids=candidate_ids,
                    embedding_provider=embedding_provider,
                    query=query,
                    embed_key=_embed_meta.embed_key,
                )
            except Exception:  # noqa: BLE001
                pass  # fail-open: audit never breaks primary search

        # ID index already loaded in parallel section
        # Re-acquire lock for thread-safe reference assignment
        with self._id_index_lock:
            existing_id_index = id_index

        # Convert HNSW distances to similarities: hnswlib cosine space returns
        # distance = 1.0 - cosine_similarity, so similarity = 1.0 - distance
        # distances is a plain Python list of floats returned by hnsw_manager.query()
        candidate_similarities = [1.0 - d for d in distances]

        t0 = time.time()

        def _hydrate_from_chunk_store(chunk_store: Any) -> List[Dict[str, Any]]:
            """CHUNKS_DB Case-A/Case-B hydration, on the MAIN thread. Reused by
            the normal CHUNKS_DB path AND Bug #1486 Finding 5's re-hydrate retry
            (a concurrent migration that deleted the legacy JSON mid-hydration),
            so the read logic exists once and always returns a FRESH list."""
            _res: List[Dict[str, Any]] = []
            if not filter_conditions:
                # Case A (CHUNKS_DB): Story #1456 AC4 -- apply score_threshold on
                # HNSW similarities BEFORE any reads, take the top `limit`
                # candidates FIRST (no existence pre-check across the full
                # candidate set), then hydrate ONLY those.
                candidates = [
                    (point_id, float(sim))
                    for point_id, sim in zip(candidate_ids, candidate_similarities)
                    if score_threshold is None or sim >= score_threshold
                ]
                for point_id, similarity in candidates[:limit]:
                    record = chunk_store.read(point_id)
                    if record is None:
                        continue
                    _res.append(
                        {
                            "id": record["id"],
                            "score": similarity,
                            "payload": record.get("payload", {}),
                            "_vector_data": record,
                        }
                    )
            else:
                # Case B (CHUNKS_DB): payload filter per HNSW candidate via a
                # single indexed point_id lookup. Early-exit is CONDITIONAL on
                # lazy_load -- identical semantics to the legacy path below.
                filter_func = self._parse_filter(filter_conditions)
                for point_id, similarity in zip(candidate_ids, candidate_similarities):
                    if score_threshold is not None and similarity < score_threshold:
                        continue
                    # Story #1493 AC2 (report Finding C2): when the caller
                    # (temporal query path) wants chunk_type="commit_message",
                    # derive is_head PURELY from point_id (zero I/O, zero
                    # decode -- temporal_point_builder.py's is_head_chunk_id)
                    # and skip the full zstd+json decode entirely for any
                    # candidate that is DEFINITELY a non-head chunk -- the
                    # caller's own is_head post-filter would discard it
                    # anyway. None (point_id doesn't parse as the unified
                    # temporal scheme) is NEVER treated as a non-match --
                    # falls through to a normal full decode, never silently
                    # dropped. "commit_diff" has NO is_head filtering at all
                    # (real semantics per temporal_search_service.py's
                    # _filter_by_time_range: it keeps every chunk, head or
                    # not) so only "commit_message" has anything to skip
                    # here. Lazily imported so a non-temporal caller (every
                    # semantic/FTS query, temporal_chunk_type=None) pays
                    # zero extra import cost.
                    if temporal_chunk_type == "commit_message":
                        from code_indexer.services.temporal.temporal_point_builder import (
                            is_head_chunk_id,
                        )

                        _is_head = is_head_chunk_id(point_id)
                        if _is_head is False:
                            continue
                    record = chunk_store.read(point_id)
                    if record is None:
                        continue
                    payload = record.get("payload", {})
                    if not filter_func(payload):
                        continue
                    _res.append(
                        {
                            "id": record["id"],
                            "score": float(similarity),
                            "payload": payload,
                            "_vector_data": record,
                        }
                    )
                    if lazy_load and len(_res) >= limit:
                        break
            return _res

        results: List[Dict[str, Any]] = []

        # Bug #1486 Codex Finding 5 (STILL-OPEN): set True when a legacy
        # hydration branch SKIPS a candidate because its vector_*.json file did
        # not exist (Path.exists() -> False). A concurrent server-mode migration
        # that flips the discriminator to CHUNKS_DB AND deletes the legacy files
        # in the window between the hydration re-resolve above and the exists()
        # filter raises NO exception -- the file is silently skipped -- so the
        # existing ``except FileNotFoundError`` handler never fires. This flag is
        # the precise, perf-conservative trigger for the post-hydration
        # re-resolve in the ``else`` clause below: a masked flip can ONLY surface
        # as a skipped legacy file, so a happy-path legacy read (no skips) pays
        # no extra resolve.
        _legacy_file_skipped = False

        try:
            if _hydration_chunk_layout == ChunkLayout.CHUNKS_DB:
                # Invariant: chunk_store_for_hydration is always opened above
                # when the layout resolves to CHUNKS_DB. Asserted for mypy's
                # benefit (Optional[Any] narrowing) -- not a runtime guard.
                assert chunk_store_for_hydration is not None
                results = _hydrate_from_chunk_store(chunk_store_for_hydration)

            elif not filter_conditions:
                # Case A (legacy sharded-JSON): No filter_conditions - maximum
                # optimization path. Apply score_threshold on HNSW similarities
                # before any JSON reads, then read JSON only for the top `limit`
                # results.
                #
                # Result set is byte-identical to the prior comprehension; the
                # only addition is Bug #1486 Finding 5's skip tracking. Evaluation
                # order is preserved exactly (in-index -> exists() -> threshold),
                # so the exists() call pattern is unchanged; when exists() reports
                # a legacy file absent, record the skip so the ``else`` clause can
                # detect an exists()-masked concurrent flip to CHUNKS_DB.
                candidates = []
                for point_id, sim in zip(candidate_ids, candidate_similarities):
                    if point_id not in existing_id_index:
                        continue
                    if not existing_id_index[point_id].exists():
                        _legacy_file_skipped = True
                        continue
                    if score_threshold is not None and sim < score_threshold:
                        continue
                    candidates.append((point_id, float(sim)))

                # HNSW already returns candidates sorted by distance (closest first),
                # so candidates are already in descending similarity order.
                # Take top `limit` candidates before reading any JSON.
                top_candidates = candidates[:limit]

                # Read JSON only for the top results to get payload/content
                for point_id, similarity in top_candidates:
                    vector_file = existing_id_index[point_id]
                    try:
                        with open(vector_file) as f:
                            data = json.load(f)

                        results.append(
                            {
                                "id": data["id"],
                                "score": similarity,
                                "payload": data.get("payload", {}),
                                "_vector_data": data,
                            }
                        )
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue

            else:
                # Case B (legacy sharded-JSON, UNCHANGED): filter_conditions
                # present - must read JSON for filter evaluation. Use
                # HNSW-derived similarities (not recalculated) but read JSON to
                # apply filter conditions. Stop when we have `limit` results
                # (early exit).
                filter_func = self._parse_filter(filter_conditions)

                for point_id, similarity in zip(candidate_ids, candidate_similarities):
                    if point_id not in existing_id_index:
                        continue

                    vector_file = existing_id_index[point_id]
                    if not vector_file.exists():
                        # Bug #1486 Finding 5: a legacy file skipped here (rather
                        # than raising FileNotFoundError at open()) is the silent
                        # signal of an exists()-masked concurrent flip+delete.
                        _legacy_file_skipped = True
                        continue

                    # Apply score threshold before reading JSON when possible
                    if score_threshold is not None and similarity < score_threshold:
                        continue

                    try:
                        with open(vector_file) as f:
                            data = json.load(f)

                        # Apply filter conditions on payload
                        payload = data.get("payload", {})
                        if not filter_func(payload):
                            continue

                        results.append(
                            {
                                "id": data["id"],
                                "score": float(similarity),
                                "payload": payload,
                                "_vector_data": data,
                            }
                        )

                        # EARLY EXIT: If lazy loading enabled, stop when we have enough results
                        if lazy_load and len(results) >= limit:
                            break

                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except FileNotFoundError:
            # Bug #1486 Finding 5 (TOCTOU): a legacy vector_*.json vanished
            # between the hydration resolve above and its open() -- a concurrent
            # server-mode migration deleted it AFTER flipping the discriminator.
            # Re-resolve on the MAIN thread; if the flip has landed, open the
            # ChunkStore here (Story #1456 AC7 -- main-thread only) and re-hydrate
            # from chunks.db into a FRESH results list (any partially-built
            # legacy result is discarded). A still-SHARDED_JSON collection is a
            # genuine missing file -> fail loud (re-raise the ACTIVE
            # FileNotFoundError this except clause is handling).
            if resolve_chunk_layout(collection_path) != ChunkLayout.CHUNKS_DB:
                raise
            if chunk_store_for_hydration is None:
                # Story #1492 AC3: routed through the per-thread cache
                # (see the entry-point comment above).
                chunk_store_for_hydration = self._chunk_store_cache.get_or_open(
                    collection_path / "chunks.db", str(collection_path)
                )
            results = _hydrate_from_chunk_store(chunk_store_for_hydration)
        else:
            # Bug #1486 Codex Finding 5 (STILL-OPEN): the legacy hydration
            # branches gate reads on Path.exists(). If a concurrent server-mode
            # migration flips the discriminator to CHUNKS_DB AND deletes the
            # legacy vector_*.json files in the window between the hydration
            # re-resolve above and the exists() filter, exists() returns False --
            # the file is SILENTLY SKIPPED, so NO FileNotFoundError is raised and
            # the ``except`` handler above never runs. search() would then return
            # empty/partial results despite a fully-valid chunks.db (Codex repro:
            # SEARCH_FALSE_EXISTS layout chunks_db result_count 0).
            #
            # Path.exists() can NEVER be the race discriminator: a skipped file
            # is indistinguishable from a genuinely-absent one. The only
            # meaningful transition is SHARDED_JSON -> CHUNKS_DB (migration only
            # ADDS chunks.db and deletes legacy AFTER the durable flip), so
            # detect it by RE-RESOLVING the committed discriminator once a legacy
            # file was actually skipped. ``_legacy_file_skipped`` is True only
            # inside a legacy branch (the CHUNKS_DB entry path never sets it), so
            # a happy-path legacy read with no skips pays no extra resolve --
            # preserving the Finding 3 perf gate. If the flip has landed, discard
            # the stale legacy result and re-hydrate from chunks.db into a FRESH
            # list, byte-identical to the permanently-CHUNKS_DB path.
            if _legacy_file_skipped and (
                resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
            ):
                if chunk_store_for_hydration is None:
                    # Story #1456 AC7: ChunkStore open on the MAIN/calling
                    # thread only. Story #1492 AC3: routed through the
                    # per-thread cache instead of a fresh unconditional
                    # open -- connection lifecycle now belongs to
                    # ChunkStoreThreadCache, never closed at the end of
                    # every search() call (see the removed `finally`
                    # block below).
                    chunk_store_for_hydration = self._chunk_store_cache.get_or_open(
                        collection_path / "chunks.db", str(collection_path)
                    )
                results = _hydrate_from_chunk_store(chunk_store_for_hydration)

        timing["candidate_load_ms"] = (time.time() - t0) * 1000

        # Sort by score and limit
        results.sort(key=lambda x: x["score"], reverse=True)
        limited_results = results[:limit]

        # Enhance with content and staleness
        t0 = time.time()
        enhanced_results = []
        for result in limited_results:
            vector_data = result.pop("_vector_data")
            content, staleness = self._get_chunk_content_with_staleness(vector_data)
            result["payload"]["content"] = content
            result["staleness"] = staleness
            # Return chunk_text at root level for optimization contract
            if "chunk_text" in vector_data:
                result["chunk_text"] = vector_data["chunk_text"]
            enhanced_results.append(result)

        timing["staleness_detection_ms"] = (time.time() - t0) * 1000

        return (enhanced_results, timing) if return_timing else enhanced_results

    def _get_chunk_content_with_staleness(self, vector_data: Dict[str, Any]) -> tuple:
        """Retrieve chunk content with staleness detection.

        Strategy:
        - Non-git repos: Return chunk_text from JSON (never stale)
        - Git repos (clean): Try current file → git blob → error
        - Git repos (dirty): Return chunk_text from JSON (never stale)

        Args:
            vector_data: Vector data dictionary from JSON

        Returns:
            Tuple of (content, staleness_info)

        Staleness info structure:
            {
                'is_stale': bool,
                'staleness_indicator': '⚠️ Modified' | '🗑️ Deleted' | '❌ Error' | None,
                'staleness_reason': str | None,
                'hash_mismatch': bool (git repos only)
            }
        """
        # Get payload structure
        payload = vector_data.get("payload", {})

        # Non-git repos: content stored in payload, never stale
        if "chunk_text" in vector_data:
            return vector_data["chunk_text"], {
                "is_stale": False,
                "staleness_indicator": None,
                "staleness_reason": None,
            }

        # Check for content in payload (new format)
        if "content" in payload and payload.get("git_available", False):
            # Git repos with payload format - continue to git blob retrieval logic below
            # (Don't return early - let staleness detection happen)
            pass
        elif "content" in payload:
            # Non-git repos with payload format
            return payload["content"], {
                "is_stale": False,
                "staleness_indicator": None,
                "staleness_reason": None,
            }

        # Git repos: 3-tier fallback with staleness detection
        if "git_blob_hash" in vector_data:
            # Get file info from payload (always use payload for consistency)
            file_path = payload.get("path", "")
            start_line = payload.get("line_start", 0)
            end_line = payload.get("line_end", 0)
            stored_hash = vector_data.get("git_blob_hash", "")

            # Tier 1: Try reading from current file
            full_path = self.project_root / file_path

            if full_path.exists():
                try:
                    # Read chunk from current file
                    # Note: line_start/line_end are 1-based, convert to 0-based for Python slicing
                    with open(full_path) as f:
                        lines = f.readlines()
                        chunk_content = "".join(lines[(start_line - 1) : end_line])

                    # Bug #1181 Perf Fix #3: for immutable versioned snapshots the file
                    # cannot have changed since indexing, so skip the second whole-file
                    # read + SHA-1 (_compute_file_hash) and return fresh immediately.
                    if self.skip_staleness_check:
                        return chunk_content, {
                            "is_stale": False,
                            "staleness_indicator": None,
                            "staleness_reason": None,
                            "hash_mismatch": False,
                        }

                    # Compute current file hash
                    current_hash = self._compute_file_hash(full_path)

                    # Check for staleness via hash comparison
                    if current_hash == stored_hash:
                        # File unchanged - content is current
                        return chunk_content, {
                            "is_stale": False,
                            "staleness_indicator": None,
                            "staleness_reason": None,
                            "hash_mismatch": False,
                        }
                    else:
                        # File modified - fall back to git blob
                        blob_content = self._retrieve_from_git_blob(
                            stored_hash, start_line, end_line
                        )

                        return blob_content, {
                            "is_stale": True,
                            "staleness_indicator": "⚠️ Modified",
                            "staleness_reason": "file_modified_after_indexing",
                            "hash_mismatch": True,
                        }

                except Exception as e:
                    # Tier 3: Error reading file - try git blob
                    try:
                        blob_content = self._retrieve_from_git_blob(
                            stored_hash, start_line, end_line
                        )

                        return blob_content, {
                            "is_stale": True,
                            "staleness_indicator": "❌ Error",
                            "staleness_reason": "retrieval_failed",
                            "hash_mismatch": False,
                        }
                    except Exception:
                        # Complete failure
                        return f"[Error retrieving content: {str(e)}]", {
                            "is_stale": True,
                            "staleness_indicator": "❌ Error",
                            "staleness_reason": "retrieval_failed",
                            "hash_mismatch": False,
                        }
            else:
                # File deleted - retrieve from git blob
                try:
                    blob_content = self._retrieve_from_git_blob(
                        stored_hash, start_line, end_line
                    )

                    return blob_content, {
                        "is_stale": True,
                        "staleness_indicator": "🗑️ Deleted",
                        "staleness_reason": "file_deleted",
                        "hash_mismatch": False,
                    }
                except Exception as e:
                    return f"[File deleted, cannot retrieve: {str(e)}]", {
                        "is_stale": True,
                        "staleness_indicator": "🗑️ Deleted",
                        "staleness_reason": "file_deleted",
                        "hash_mismatch": False,
                    }

        # Fallback: no content available
        return "[Content not available]", {
            "is_stale": False,
            "staleness_indicator": None,
            "staleness_reason": None,
        }

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute git blob hash for a file.

        Uses same algorithm as git for compatibility.

        Args:
            file_path: Path to file

        Returns:
            Git blob hash (40-char hex string)
        """
        try:
            with open(file_path, "rb") as f:
                content = f.read()

            # Git blob format: "blob <size>\0<content>"
            blob_data = f"blob {len(content)}\0".encode() + content

            return hashlib.sha1(blob_data).hexdigest()
        except Exception:
            return ""

    def _retrieve_from_git_blob(
        self, blob_hash: str, start_line: int, end_line: int
    ) -> str:
        """Retrieve chunk content from git blob.

        Args:
            blob_hash: Git blob hash
            start_line: Start line of chunk
            end_line: End line of chunk

        Returns:
            Chunk content from git blob

        Raises:
            RuntimeError: If git operation fails
        """
        try:
            # Use git cat-file to retrieve blob content
            result = subprocess.run(
                ["git", "cat-file", "blob", blob_hash],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                raise RuntimeError(f"Git cat-file failed: {result.stderr}")

            # Extract chunk lines
            # Note: line_start/line_end are 1-based, convert to 0-based for Python slicing
            # line_end is exclusive (Python slicing convention)
            lines = result.stdout.splitlines(keepends=True)
            chunk_content = "".join(lines[(start_line - 1) : end_line])

            return chunk_content

        except subprocess.TimeoutExpired:
            raise RuntimeError("Git cat-file timeout")
        except Exception as e:
            raise RuntimeError(f"Failed to retrieve git blob: {str(e)}")

    def resolve_collection_name(self, config: Any, embedding_provider: Any) -> str:
        """Generate collection name based on current provider and model.

        Uses model name as collection name.
        """
        model_name: str = embedding_provider.get_current_model()
        # Replace special characters to make it filesystem-safe
        safe_name: str = model_name.replace("/", "_").replace(":", "_")
        return safe_name

    def ensure_provider_aware_collection(
        self,
        config,
        embedding_provider,
        quiet: bool = False,
        skip_migration: bool = False,
    ) -> str:
        """Create/validate collection with provider-aware naming.

        Args:
            config: Main configuration object (unused for filesystem)
            embedding_provider: Current embedding provider instance
            quiet: Suppress output (unused for filesystem)
            skip_migration: Skip migration checks (unused for filesystem)

        Returns:
            Collection name that was created/validated
        """
        collection_name = self.resolve_collection_name(config, embedding_provider)
        vector_size = embedding_provider.get_model_info()["dimensions"]

        if not self.collection_exists(collection_name):
            self.create_collection(collection_name, vector_size)

        return collection_name

    def clear_collection(
        self, collection_name: str, remove_projection_matrix: bool = False
    ) -> bool:
        """Clear vectors from collection while optionally preserving projection matrix.

        Removes all indexed vectors from a collection. By default, preserves the
        projection matrix to allow faster re-indexing. The collection metadata
        (quantization_range) is recreated on next index operation.

        The preserved ``collection_meta.json`` has its ``chunks_db`` layout
        discriminator stripped, if present, before being restored -- the
        ``chunks.db`` file it points at was just deleted by this same clear,
        so keeping the discriminator would falsely claim CHUNKS_DB layout for
        a store that no longer exists (see
        ``chunk_layout.clear_chunks_db_discriminator``).

        Bug #1644 Finding 1: a semantic (non-temporal) collection has no
        pre-flight step that re-commits the discriminator after a clear
        (unlike temporal's ``consolidate_legacy_temporal_shards()``), so
        merely stripping it here would leave THIS store instance's next
        write silently downgrading to the legacy SHARDED_JSON layout. The
        pre-clear on-disk layout is therefore captured before the rmtree
        and, if it was CHUNKS_DB, recorded as this session's build intent
        in ``self._chunks_db_mode`` so the next write on this same instance
        still builds ``chunks.db``.

        Args:
            collection_name: Name of the collection to clear
            remove_projection_matrix: If True, also remove projection matrix (default: False)

        Returns:
            True if cleared successfully
        """
        collection_path = self.base_path / collection_name

        if not self.collection_exists(collection_name):
            return False

        try:
            import shutil

            from code_indexer.storage.shared.chunk_layout import (
                ChunkLayout,
                clear_chunks_db_discriminator,
                resolve_chunk_layout,
            )

            was_chunks_db = (
                resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
            )

            # Save projection matrix and metadata if we need to preserve them
            matrix_file = collection_path / "projection_matrix.npy"
            metadata_file = collection_path / "collection_meta.json"
            matrix_data = None
            metadata_data = None

            if not remove_projection_matrix:
                if matrix_file.exists():
                    matrix_data = matrix_file.read_bytes()
                if metadata_file.exists():
                    metadata_data = metadata_file.read_bytes()
                    metadata_data = clear_chunks_db_discriminator(metadata_data)

            # Remove entire collection directory
            shutil.rmtree(collection_path)

            # Clear ID index for this collection
            with self._id_index_lock:
                if collection_name in self._id_index:
                    del self._id_index[collection_name]
                # Bug #1583: a cleared-and-recreated collection must be
                # eligible for the reactive stale-index rebuild again.
                self._id_index_reactive_rebuild_done.discard(collection_name)

            # Restore projection matrix and metadata if they were preserved
            if matrix_data is not None or metadata_data is not None:
                collection_path.mkdir(parents=True, exist_ok=True)
                if matrix_data is not None:
                    matrix_file.write_bytes(matrix_data)
                if metadata_data is not None:
                    metadata_file.write_bytes(metadata_data)

            # Bug #1644 Finding 1: preserve this session's CHUNKS_DB write
            # intent so the next index operation on this same store
            # instance keeps building chunks.db instead of silently
            # downgrading to legacy vector_*.json files.
            if was_chunks_db:
                self._chunks_db_mode[collection_name] = True

            return True

        except Exception:
            return False

    def delete_collection(self, collection_name: str) -> bool:
        """Delete entire collection including structure and metadata.

        Args:
            collection_name: Name of the collection to delete

        Returns:
            True if deleted successfully
        """
        collection_path = self.base_path / collection_name

        if not self.collection_exists(collection_name):
            return False

        try:
            # Remove entire collection directory
            import shutil

            shutil.rmtree(collection_path)

            # Clear ID index and file path cache for this collection
            with self._id_index_lock:
                if collection_name in self._id_index:
                    del self._id_index[collection_name]
                if collection_name in self._file_path_cache:
                    del self._file_path_cache[collection_name]
                # Bug #1583: a deleted-and-recreated collection must be
                # eligible for the reactive stale-index rebuild again.
                self._id_index_reactive_rebuild_done.discard(collection_name)

            # Bug #1644 Finding 1 follow-up: a deleted collection must not
            # leave a stale in-session CHUNKS_DB intent behind for a
            # future collection of the same name to inherit.
            self._chunks_db_mode.pop(collection_name, None)

            return True

        except Exception:
            return False

    def create_point(
        self,
        vector: List[float],
        payload: Dict[str, Any],
        point_id: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a point object for batch operations.

        Args:
            vector: Vector data
            payload: Point payload
            point_id: Optional point ID
            embedding_model: Optional embedding model (added to payload)

        Returns:
            Point dictionary ready for upsert
        """
        point_payload = payload.copy()

        if embedding_model:
            point_payload["embedding_model"] = embedding_model

        point = {"vector": vector, "payload": point_payload}

        if point_id:
            point["id"] = point_id

        return point

    def delete_by_filter(
        self, collection_name: str, filter_conditions: Dict[str, Any]
    ) -> bool:
        """Delete vectors matching filter conditions.

        Args:
            collection_name: Name of the collection
            filter_conditions: Filter conditions

        Returns:
            True if deletion successful
        """
        try:
            # Scroll through vectors with filter applied
            points, _ = self.scroll_points(
                collection_name=collection_name,
                limit=10000,
                with_payload=True,
                with_vectors=False,
                filter_conditions=filter_conditions,
            )

            # All returned points match the filter, so delete them all
            points_to_delete: List[str] = [point["id"] for point in points]

            # Delete matching points
            if points_to_delete:
                result: Dict[str, Any] = self.delete_points(
                    collection_name, points_to_delete
                )
                return bool(result["status"] == "ok")

            return True

        except Exception as e:
            self.logger.error(
                "delete_by_filter failed for collection '%s' with filter %r",
                collection_name,
                filter_conditions,
                exc_info=e,
            )
            return False

    def get_collection_info(self, collection_name: str) -> Dict[str, Any]:
        """Get collection metadata.

        Args:
            collection_name: Name of the collection

        Returns:
            Collection metadata dictionary

        Raises:
            RuntimeError: If collection doesn't exist
        """
        collection_path = self.base_path / collection_name
        metadata_path = collection_path / "collection_meta.json"

        if not metadata_path.exists():
            raise RuntimeError(f"Collection '{collection_name}' does not exist")

        try:
            with open(str(metadata_path), "r") as f:
                metadata: Dict[str, Any] = json.load(f)
                return metadata
        except (json.JSONDecodeError, IOError) as e:
            raise RuntimeError(f"Failed to read collection metadata: {e}")

    def health_check(self) -> bool:
        """Check if filesystem backend is accessible.

        Returns:
            True if filesystem is readable and writable
        """
        return self.base_path.exists() and os.access(self.base_path, os.W_OK)

    def _batch_update_points(
        self, points: List[Dict[str, Any]], collection_name: str, batch_size: int = 100
    ) -> bool:
        """Update multiple points with new payload data.

        Args:
            points: List of point updates with structure {"id": point_id, "payload": {...}}
            collection_name: Name of the collection
            batch_size: Batch size (unused for filesystem, kept for compatibility)

        Returns:
            True if all updates succeeded
        """
        try:
            for point in points:
                point_id = point["id"]
                new_payload = point["payload"]

                # Get existing point
                existing = self.get_point(point_id, collection_name)
                if not existing:
                    continue

                # MERGE payload (not replace) - preserve existing metadata
                existing_payload = existing.get("payload", {})
                merged_payload = {**existing_payload, **new_payload}

                updated_point = {
                    "id": point_id,
                    "vector": existing["vector"],
                    "payload": merged_payload,
                }

                # Preserve chunk_text if it exists in the original
                if "chunk_text" in existing:
                    updated_point["chunk_text"] = existing["chunk_text"]

                # Upsert updated point
                self.upsert_points(collection_name, [updated_point])

            return True

        except Exception:
            return False

    def _batch_update_payload_only(
        self, points: List[Dict[str, Any]], collection_name: str
    ) -> bool:
        """Update payload fields only, bypassing the full upsert pipeline.

        Fix B (Story #339): Lightweight alternative to _batch_update_points for
        visibility-only changes (e.g. hidden_branches). Bypasses:
        - Projection matrix loading
        - Git blob hash lookups
        - Path index updates
        - Vector quantization

        Uses id_index for O(1) file resolution, then does direct JSON
        read -> payload merge -> JSON write. Vector data and chunk_text
        are preserved exactly as stored.

        Args:
            points: List of updates with structure {"id": point_id, "payload": {...}}
                    Only the specified payload fields are merged; all others preserved.
            collection_name: Name of the collection

        Returns:
            True on success (including empty list or all-skip scenarios)
        """
        if not points:
            return True

        # Story #1456 AC7: CHUNKS_DB collections update via the chunk store
        # directly -- id_index.bin is never read or written for this method.
        # Uses the combined _is_chunks_db_collection authority (not the bare
        # resolver) so this is correct even mid-build, before end_indexing()
        # commits the discriminator.
        collection_path = self._get_collection_path(collection_name)

        # Bug #1575 Part C: dirty-before-write -- durably mark the hnsw_sync
        # epoch dirty BEFORE any of this call's storage mutations happen.
        self._mark_hnsw_dirty_before_mutation(collection_path, collection_name)

        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            try:
                chunk_store = open_chunk_store_for_path(
                    collection_path / "chunks.db", str(collection_path)
                )
                try:
                    updates = [(point["id"], point["payload"]) for point in points]
                    # Bug #1575 Part C (AC12/AC43): the diff-returning
                    # variant lets us register EXACTLY which points had a
                    # real visibility-relevant change (path or
                    # hidden_branches), instead of only a row count.
                    changes = chunk_store.update_payload_fields_batch_with_diff(updates)
                finally:
                    chunk_store.close()

                if self._hnsw_sync_epoch_enabled:
                    session = self._get_or_create_hnsw_sync_session(
                        collection_path, collection_name
                    )
                    for change in changes:
                        if (
                            change.old_path != change.new_path
                            or change.old_hidden_branches != change.new_hidden_branches
                        ):
                            session.visibility_changed.add(change.point_id)

                return True
            except Exception as e:
                self.logger.warning(
                    "Failed to batch update payload (CHUNKS_DB) for collection %s: %s",
                    collection_name,
                    e,
                    exc_info=True,
                )
                if self._hnsw_sync_epoch_enabled:
                    session = self._get_or_create_hnsw_sync_session(
                        collection_path, collection_name
                    )
                    session.complete_change_tracking = False
                return False

        try:
            # Ensure id_index is loaded for O(1) file resolution. Resolved
            # via _active_subdirectories (this method has no subdirectory
            # param) so a nested collection's batch update targets its OWN
            # cache entry, never a bare-name top-level collision.
            _batch_update_subdirectory = self._active_subdirectories.get(
                collection_name
            )
            _batch_update_cache_key = self._id_cache_key(
                collection_name, _batch_update_subdirectory
            )
            with self._id_index_lock:
                if _batch_update_cache_key not in self._id_index:
                    self._id_index[_batch_update_cache_key] = self._load_id_index(
                        collection_name, _batch_update_subdirectory
                    )
                index = self._id_index[_batch_update_cache_key]

            for point in points:
                point_id = point["id"]
                new_payload_fields = point["payload"]

                # O(1) lookup - no directory scan
                vector_file = index.get(point_id)
                if vector_file is None:
                    # Point not in id_index - skip gracefully
                    continue

                if not vector_file.exists():
                    # File was deleted externally - skip gracefully
                    continue

                # Direct JSON read
                with open(vector_file) as f:
                    data = json.load(f)

                # Merge only the specified payload fields (preserve all others)
                existing_payload = data.get("payload", {})
                # Bug #1575 Part C (AC12): capture old path/hidden_branches
                # BEFORE merging so a real visibility-relevant change can be
                # detected afterward.
                old_path = existing_payload.get("path")
                old_hidden_branches = tuple(existing_payload.get("hidden_branches", []))
                for key, value in new_payload_fields.items():
                    existing_payload[key] = value
                data["payload"] = existing_payload

                new_path = existing_payload.get("path")
                new_hidden_branches = tuple(existing_payload.get("hidden_branches", []))
                if self._hnsw_sync_epoch_enabled and (
                    old_path != new_path or old_hidden_branches != new_hidden_branches
                ):
                    session = self._get_or_create_hnsw_sync_session(
                        collection_path, collection_name
                    )
                    session.visibility_changed.add(point_id)

                # Direct JSON write (atomic via _atomic_write_json)
                self._atomic_write_json(vector_file, data)

            return True

        except Exception as e:
            self.logger.warning(
                "Failed to batch update payload for collection %s: %s",
                collection_name,
                e,
                exc_info=True,
            )
            if self._hnsw_sync_epoch_enabled:
                session = self._get_or_create_hnsw_sync_session(
                    collection_path, collection_name
                )
                session.complete_change_tracking = False
            return False

    def rebuild_payload_indexes(self, collection_name: str) -> bool:
        """Rebuild payload indexes (no-op for filesystem backend).

        Filesystem backend doesn't use payload indexes.
        Returns True for compatibility.
        """
        return True

    def ensure_payload_indexes(self, collection_name: str, context: str = "") -> None:
        """Ensure payload indexes exist (no-op for filesystem backend).

        Filesystem backend doesn't use payload indexes.
        No-op for compatibility.
        """
        pass

    def get_all_indexed_files(self, collection_name: str) -> List[str]:
        """Get all unique file paths from indexed vectors.

        Uses lazy loading: ID index is loaded from filenames (fast), file paths
        are only loaded by parsing JSON files when actually needed.

        Args:
            collection_name: Name of the collection

        Returns:
            List of unique file paths
        """
        # Story #1456 AC7: CHUNKS_DB collections derive the file-path set
        # directly from chunks.db -- id_index.bin is never read or written,
        # and no per-file JSON is opened. self._file_path_cache caching
        # behavior is preserved; only the data source changes.
        collection_path = self._get_collection_path(collection_name)
        if self._is_chunks_db_collection(collection_name, collection_path):
            with self._id_index_lock:
                if collection_name not in self._file_path_cache:
                    from code_indexer.storage.sqlite_chunk_store import (
                        open_chunk_store_for_path,
                    )

                    chunk_store = open_chunk_store_for_path(
                        collection_path / "chunks.db", str(collection_path)
                    )
                    try:
                        self._file_path_cache[collection_name] = (
                            chunk_store.distinct_paths()
                        )
                    finally:
                        chunk_store.close()

                file_paths = self._file_path_cache[collection_name]

            return sorted(list(file_paths))

        # Resolved via _active_subdirectories (this method has no
        # subdirectory param) so a nested collection's file listing reads
        # its OWN _id_index cache entry, never a bare-name top-level
        # collision.
        _list_files_subdirectory = self._active_subdirectories.get(collection_name)
        _list_files_cache_key = self._id_cache_key(
            collection_name, _list_files_subdirectory
        )
        with self._id_index_lock:
            # Ensure ID index is loaded (fast - from filenames only)
            if _list_files_cache_key not in self._id_index:
                self._id_index[_list_files_cache_key] = self._load_id_index(
                    collection_name, _list_files_subdirectory
                )

            # Lazily load file paths if not cached
            if collection_name not in self._file_path_cache:
                id_index = self._id_index[_list_files_cache_key]
                self._file_path_cache[collection_name] = self._load_file_paths(
                    collection_name, id_index
                )

            file_paths = self._file_path_cache[collection_name]

        return sorted(list(file_paths))

    def get_indexed_file_count_fast(self, collection_name: str) -> int:
        """Get count of indexed files from metadata (FAST - single JSON read).

        Returns 100% accurate file count from collection metadata if available,
        otherwise falls back to estimation. Use this for status/monitoring.

        Args:
            collection_name: Name of the collection

        Returns:
            Number of unique files indexed (accurate if metadata has it, estimated otherwise)

        Note:
            After indexing completes, unique_file_count is stored in metadata for instant lookup.
            Old indexes without this field will fall back to estimation (~99.8% accurate).
        """
        collection_path = self.base_path / collection_name
        meta_file = collection_path / "collection_meta.json"

        # Try reading from metadata first (FAST - single small JSON read)
        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    metadata = json.load(f)

                # Return accurate count from metadata if available
                if "unique_file_count" in metadata:
                    return int(metadata["unique_file_count"])

            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to read collection metadata: {e}")

        # Story #1456 AC3/AC7: CHUNKS_DB collections never fall through to
        # the estimate below -- id_index stays empty for this layout (no
        # id_index.bin), so the estimate would be meaningless. chunks.db's
        # indexed path column gives an EXACT (not estimated) count cheaply.
        if self._is_chunks_db_collection(collection_name, collection_path):
            with self._id_index_lock:
                if collection_name in self._file_path_cache:
                    return len(self._file_path_cache[collection_name])

            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                return len(chunk_store.distinct_paths())
            finally:
                chunk_store.close()

        # Fallback: estimation for old indexes or if metadata read fails.
        # Resolved via _active_subdirectories (this method has no
        # subdirectory param) so a nested collection's estimate reads its
        # OWN _id_index cache entry, never a bare-name top-level collision.
        _file_count_subdirectory = self._active_subdirectories.get(collection_name)
        _file_count_cache_key = self._id_cache_key(
            collection_name, _file_count_subdirectory
        )
        with self._id_index_lock:
            # If file paths already cached, return count from cache (instant)
            if collection_name in self._file_path_cache:
                return len(self._file_path_cache[collection_name])

            # Otherwise estimate: vectors / average chunks per file (~2)
            # This is fast but approximate - acceptable for status display
            if _file_count_cache_key not in self._id_index:
                self._id_index[_file_count_cache_key] = self._load_id_index(
                    collection_name, _file_count_subdirectory
                )

            vector_count = len(self._id_index[_file_count_cache_key])
            # Estimate: most files have 1-3 chunks, average ~2
            estimated_files = max(1, vector_count // 2)

            return estimated_files

    def _calculate_and_save_unique_file_count(
        self,
        collection_name: str,
        collection_path: Path,
        subdirectory: Optional[str] = None,
    ) -> int:
        """Calculate unique file count from all vectors and save to collection metadata.

        This method is called ONCE after indexing completes to calculate the 100% accurate
        file count. It's thread-safe with daemon operations via file locking.

        The count represents the CURRENT state of indexed files (not cumulative), which
        handles re-indexing correctly - same file indexed twice only counts once.

        Args:
            collection_name: Name of the collection
            collection_path: Path to collection directory
            subdirectory: Optional explicit subdirectory (e.g.
                "multimodal_index"). When None, falls back to the
                active-indexing subdirectory recorded for this collection
                (byte-identical to the pre-fix bare-key behavior for every
                existing caller, which always omits this argument and calls
                while the active-indexing session is still open).

        Returns:
            Number of unique files indexed

        Note:
            Thread-safe: Uses file locking to prevent race conditions with daemon indexing
        """
        if subdirectory is None:
            subdirectory = self._active_subdirectories.get(collection_name)
        import json

        from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

        # fcntl imported at module level for lock flag constants

        # Bug #1575 -- project-owner FINAL architectural decision, after 6
        # consecutive dual-review rounds each found a NEW distinct
        # correctness bug in the live-session PathIndex-cache fast-path
        # shortcut (catastrophic undercount, session leaks, non-atomic
        # writes, TOCTOU races, corrupt-file trust, write-ordering races,
        # logical lost-updates, an object-swap silently discarding
        # concurrent mutations, and a stale-multi-writer gap with no
        # self-healing): the shortcut is ABANDONED ENTIRELY for SHARDED_JSON
        # too, matching the treatment already given to CHUNKS_DB in round
        # 5's Fix 1 below. Neither layout ever consults
        # _get_live_session_path_index()/self._path_indexes to shortcut this
        # computation any more -- both ALWAYS compute the authoritative,
        # from-storage answer.
        #
        # For CHUNKS_DB: a CHUNKS_DB collection's direct query -- one
        # `SELECT DISTINCT path FROM chunks` via chunk_store.distinct_paths()
        # -- was measured at ~4.5ms even on a 24,000-row collection, so the
        # shortcut never bought anything meaningful for this layout while
        # introducing a real regression: a killed/crashed indexing session
        # leaves self._path_indexes' in-memory entry (and/or an on-disk
        # path_index.bin written by an earlier, unrelated call)
        # present-but-stale, and the shortcut would trust it forever for
        # this layout. Always computing the direct, authoritative chunks.db
        # query removes that staleness risk entirely, at negligible cost.
        collection_path_str = str(collection_path)
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", collection_path_str
            )
            try:
                unique_files: Set[str] = chunk_store.distinct_paths()
            finally:
                chunk_store.close()
        else:
            # For SHARDED_JSON: ALWAYS force the authoritative, from-disk
            # rebuild via _rebuild_and_repair_path_index() -- never consult
            # _get_live_session_path_index()'s "trust the live session"
            # shortcut, regardless of whether an active session exists or
            # whether path_index.bin was proven complete at begin_indexing()
            # time. This accepts the O(N) rescan cost on every
            # end_indexing() call as the deliberate, accepted trade-off of
            # this decision -- correctness over speed, after 6 rounds of
            # confirmed bugs in the machinery that tried to avoid it.
            #
            # The rebuild-and-repair call (rather than a bare, side-effect-
            # free disk scan) is deliberately retained: it is ALSO the
            # mechanism that repairs self._path_indexes[cache_key] in place
            # (Bug #1575 Part A Round 3, Fix A) so end_indexing()'s own
            # subsequent path_index.bin save immediately after this call
            # persists the complete, correct picture -- not a partial,
            # session-own one. That repair is Part B's (Story #540)
            # duplicate-prevention persistence correctness, orthogonal to
            # (and NOT part of) the fast-path optimization being abandoned
            # here; removing it would reintroduce the separate,
            # already-fixed "Gap A" catastrophic-undercount regression.
            authoritative_path_index = self._rebuild_and_repair_path_index(
                collection_name, subdirectory
            )
            with self._path_index_lock:
                unique_files = authoritative_path_index.all_paths()

        unique_file_count = len(unique_files)

        # Update collection metadata with file locking (daemon-safe)
        meta_file = collection_path / "collection_meta.json"
        lock_file = collection_path / ".metadata.lock"
        lock_file.touch(exist_ok=True)

        with open(lock_file, "r+") as lock_f:
            # Acquire exclusive lock (blocks if daemon is writing) — NFS-safe
            _used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)

            try:
                # Read current metadata
                with open(meta_file) as f:
                    metadata = json.load(f)

                # Update unique_file_count
                metadata["unique_file_count"] = unique_file_count

                # Save metadata atomically (Bug #1223: use atomic write helper)
                self._atomic_write_json(meta_file, metadata, fsync=True)

                self.logger.debug(
                    f"Updated collection metadata: {unique_file_count} unique files"
                )

            finally:
                # Release lock
                nfs_safe_funlock(lock_f.fileno(), _used_lockf)

        return unique_file_count

    def get_file_index_timestamps(self, collection_name: str) -> Dict[str, datetime]:
        """Get indexed_at timestamps for all files.

        For files with multiple chunks, returns the latest timestamp.

        Args:
            collection_name: Name of the collection

        Returns:
            Dictionary mapping file paths to their latest index timestamps
        """
        collection_path = self.base_path / collection_name

        if not self.collection_exists(collection_name):
            return {}

        # Story #1456 AC3: CHUNKS_DB collections derive the file-path set
        # from chunks.db, mapped to the SINGLE chunks.db file's mtime for
        # every path (there is no per-record file mtime once records are
        # consolidated into one file -- a documented, reasonable
        # approximation for this debug/introspection utility).
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            db_path = collection_path / "chunks.db"
            chunk_store = open_chunk_store_for_path(db_path, str(collection_path))
            try:
                paths = chunk_store.distinct_paths()
            finally:
                chunk_store.close()

            shared_timestamp = datetime.fromtimestamp(db_path.stat().st_mtime)
            return {path: shared_timestamp for path in paths}

        file_timestamps: Dict[str, datetime] = {}

        # Scan all vector JSON files
        for json_file in collection_path.rglob("*.json"):
            # Skip collection metadata
            if "collection_meta" in json_file.name:
                continue
            # Bug #1619: dedicated hnsw_sync bookkeeping sidecar, never a
            # vector record.
            if json_file.name == HNSW_SYNC_STATE_FILENAME:
                continue

            try:
                with open(json_file) as f:
                    data = json.load(f)

                # Extract file path from payload only
                file_path = data.get("payload", {}).get("path", "")

                if not file_path:
                    continue

                # Get file modification time as timestamp
                file_mtime = json_file.stat().st_mtime
                timestamp = datetime.fromtimestamp(file_mtime)

                # Keep latest timestamp for each file
                if (
                    file_path not in file_timestamps
                    or timestamp > file_timestamps[file_path]
                ):
                    file_timestamps[file_path] = timestamp

            except (json.JSONDecodeError, KeyError, OSError):
                # Skip corrupted or inaccessible files
                continue

        return file_timestamps

    def sample_vectors(self, collection_name: str, sample_size: int = 5) -> List[Dict]:
        """Get random sample of vectors for debugging.

        Args:
            collection_name: Name of the collection
            sample_size: Number of vectors to sample (default: 5)

        Returns:
            List of sampled vector data dictionaries
        """
        collection_path = self.base_path / collection_name

        if not self.collection_exists(collection_name):
            return []

        # Story #1456 AC3: CHUNKS_DB collections sample random point_ids
        # from chunks.db instead of rglob-scanning vector_*.json files.
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                point_ids = list(chunk_store.all_point_ids())
                if not point_ids:
                    return []

                sample_count = min(sample_size, len(point_ids))
                sampled_ids = random.sample(point_ids, sample_count)

                sampled: List[Dict] = []
                for point_id in sampled_ids:
                    record = chunk_store.read(point_id)
                    if record is None:
                        continue
                    payload = record.get("payload", {})
                    sampled.append(
                        {
                            "id": record["id"],
                            "vector": record["vector"].tolist(),
                            "file_path": payload.get("path", ""),
                            "metadata": record.get("metadata", {}),
                        }
                    )
                return sampled
            finally:
                chunk_store.close()

        # Collect all vector files
        all_vector_files = [
            f
            for f in collection_path.rglob("*.json")
            if "collection_meta" not in f.name
            # Bug #1619: dedicated hnsw_sync bookkeeping sidecar, never a
            # vector record -- exclude it from the sampling population too.
            and f.name != HNSW_SYNC_STATE_FILENAME
        ]

        if not all_vector_files:
            return []

        # Sample random files
        sample_count = min(sample_size, len(all_vector_files))
        sampled_files = random.sample(all_vector_files, sample_count)

        sampled_vectors = []

        for vector_file in sampled_files:
            try:
                with open(vector_file) as f:
                    data = json.load(f)

                # Get file_path from payload for consistency
                payload = data.get("payload", {})
                sampled_vectors.append(
                    {
                        "id": data["id"],
                        "vector": data["vector"],
                        "file_path": payload.get("path", ""),
                        "metadata": data.get("metadata", {}),
                    }
                )

            except (json.JSONDecodeError, KeyError):
                # Skip corrupted files
                continue

        return sampled_vectors

    def validate_embedding_dimensions(
        self, collection_name: str, expected_dims: int
    ) -> bool:
        """Verify all vectors have expected dimensions.

        Optimized to sample from cached ID index instead of scanning entire directory tree.
        Performance: O(1) index lookup + O(20) JSON reads (sampled files only).

        Checks a sample of vectors for performance. Empty collections return True.

        Args:
            collection_name: Name of the collection
            expected_dims: Expected vector dimensions

        Returns:
            True if all sampled vectors have expected dimensions, False otherwise
        """
        # Story #1456 AC3: CHUNKS_DB collections sample from chunks.db
        # instead of the retired id_index.bin/vector_*.json path.
        collection_path = self._get_collection_path(collection_name)
        if self._is_chunks_db_collection(collection_name, collection_path):
            from code_indexer.storage.sqlite_chunk_store import (
                open_chunk_store_for_path,
            )

            chunk_store = open_chunk_store_for_path(
                collection_path / "chunks.db", str(collection_path)
            )
            try:
                point_ids = list(chunk_store.all_point_ids())
                if not point_ids:
                    return True  # Empty collection is vacuously valid

                sample_count = min(20, len(point_ids))
                sampled_ids = random.sample(point_ids, sample_count)

                for point_id in sampled_ids:
                    record = chunk_store.read(point_id)
                    if record is None:
                        continue
                    vector = record.get("vector", [])
                    if len(vector) != expected_dims:
                        return False

                return True
            finally:
                chunk_store.close()

        # Resolved via _active_subdirectories (this method has no
        # subdirectory param) so validating a nested collection samples its
        # OWN _id_index cache entry, never a bare-name top-level collision.
        _validate_dims_subdirectory = self._active_subdirectories.get(collection_name)
        _validate_dims_cache_key = self._id_cache_key(
            collection_name, _validate_dims_subdirectory
        )
        with self._id_index_lock:
            # Ensure ID index is loaded (cached after first call)
            if _validate_dims_cache_key not in self._id_index:
                self._id_index[_validate_dims_cache_key] = self._load_id_index(
                    collection_name, _validate_dims_subdirectory
                )

            index = self._id_index[_validate_dims_cache_key]

            if not index:
                return True  # Empty collection is vacuously valid

            # Sample from cached index - no directory scan needed
            sample_count = min(20, len(index))
            sampled_files = random.sample(list(index.values()), sample_count)

        # Validate sampled files
        for vector_file in sampled_files:
            try:
                with open(vector_file) as f:
                    data = json.load(f)

                vector = data.get("vector", [])
                if len(vector) != expected_dims:
                    return False

            except (json.JSONDecodeError, KeyError, FileNotFoundError):
                # Skip corrupted or missing files, continue validation
                continue

        return True

    def get_collection_size(self, collection_name: str) -> int:
        """Get total size of collection in bytes.

        Args:
            collection_name: Name of the collection

        Returns:
            Total size in bytes, or 0 if collection doesn't exist
        """
        collection_path = self.base_path / collection_name

        if not self.collection_exists(collection_name):
            return 0

        total_size = 0
        for file_path in collection_path.rglob("*"):
            if file_path.is_file():
                try:
                    total_size += file_path.stat().st_size
                except OSError:
                    # Skip files we can't access
                    pass

        return total_size

    # === HNSW INCREMENTAL UPDATE HELPER METHODS (HNSW-001 & HNSW-002) ===

    def _update_hnsw_incrementally_realtime(
        self,
        collection_name: str,
        changed_points: List[Dict[str, Any]],
        progress_callback: Optional[Any] = None,
    ) -> None:
        """Update HNSW index incrementally in real-time (watch mode).

        Args:
            collection_name: Name of the collection
            changed_points: List of points that were added/updated
            progress_callback: Optional progress callback

        Note:
            HNSW-001: Real-time incremental updates for watch mode.
            Updates HNSW immediately after each batch of file changes,
            enabling queries without rebuild delays.

            AC2 (Concurrent Query Support): Uses readers-writer lock pattern
            AC3 (Daemon Cache Updates): Detects daemon mode and updates cache in-memory
            AC4 (Standalone Persistence): Falls back to disk persistence when no daemon

        Bug #1575 Part C review fix (Finding 2): this method's own
        metadata write (inside ``HNSWIndexManager.save_incremental_update``)
        used to be guarded ONLY by the independent ``.metadata.lock``,
        providing NO mutual exclusion against a concurrent full/incremental
        Part C rebuild or another dirty-before-write, both of which hold
        ONLY ``.index_rebuild.lock`` -- two different lock files guarding
        the same ``collection_meta.json`` is a genuine lost-update hazard.
        This wrapper acquires ``.index_rebuild.lock`` (the SAME lock every
        other writer of that file in this class uses) for the WHOLE
        real-time update, delegating the actual work (unchanged, moved
        verbatim) to ``_update_hnsw_incrementally_realtime_locked`` below.
        """
        if not changed_points:
            return

        collection_path = self.base_path / collection_name

        from .background_index_rebuilder import BackgroundIndexRebuilder

        rebuilder = BackgroundIndexRebuilder(collection_path)
        with rebuilder.acquire_lock():
            self._update_hnsw_incrementally_realtime_locked(
                collection_name, collection_path, changed_points, progress_callback
            )

    def _update_hnsw_incrementally_realtime_locked(
        self,
        collection_name: str,
        collection_path: Path,
        changed_points: List[Dict[str, Any]],
        progress_callback: Optional[Any] = None,
    ) -> None:
        """The real-time HNSW update's actual implementation (daemon-mode
        and standalone-mode branches, unchanged) -- ALWAYS called with
        ``.index_rebuild.lock`` already held by the caller
        (``_update_hnsw_incrementally_realtime`` above).
        """
        vector_size = self._get_vector_size(collection_name)

        from .hnsw_index_manager import HNSWIndexManager

        hnsw_manager = HNSWIndexManager(vector_dim=vector_size, space="cosine")

        # AC3: Detect daemon mode vs standalone mode
        daemon_mode = hasattr(self, "cache_entry") and self.cache_entry is not None

        if daemon_mode and self.cache_entry is not None:
            # === DAEMON MODE: Update cache in-memory with locking ===
            cache_entry = self.cache_entry

            # AC2: Acquire write lock for exclusive HNSW update
            # ReaderWriterLock provides exclusive access (no concurrent readers or writers)
            cache_entry.rw_lock.acquire_write()
            try:
                # Load from cache or disk if not cached
                if cache_entry.hnsw_index is None:
                    # Cache not loaded - load from disk
                    cache_entry.hnsw_index = hnsw_manager.load_index(
                        collection_path, max_elements=100000
                    )

                    cache_entry.id_mapping = self._load_id_index(collection_name)

                # Use cache references
                index = cache_entry.hnsw_index
                id_mapping = cache_entry.id_mapping

                if index is None:
                    # No existing index - mark as stale for query-time rebuild
                    self.logger.debug(
                        f"No existing HNSW index for watch mode update in '{collection_name}', "
                        f"marking as stale"
                    )
                    hnsw_manager.mark_stale(collection_path)
                    return

                # Build ID-to-label and label-to-ID mappings
                label_to_id = hnsw_manager._load_id_mapping(collection_path)
                id_to_label = {v: k for k, v in label_to_id.items()}
                next_label = max(label_to_id.keys()) + 1 if label_to_id else 0

                # Process each changed point
                processed = 0
                for point in changed_points:
                    point_id = point["id"]
                    vector = np.array(point["vector"], dtype=np.float32)

                    try:
                        # Add or update in HNSW (updates cache index directly)
                        old_count = len(id_to_label)
                        label, id_to_label, label_to_id, next_label = (
                            hnsw_manager.add_or_update_vector(
                                index,
                                point_id,
                                vector,
                                id_to_label,
                                label_to_id,
                                next_label,
                            )
                        )
                        new_count = len(id_to_label)

                        self.logger.debug(
                            f"Daemon watch mode HNSW: added '{point_id}' with label {label}, "
                            f"mappings: {old_count} -> {new_count}, next_label: {next_label}"
                        )

                        processed += 1

                    except Exception as e:
                        self.logger.warning(
                            f"Failed to update HNSW for point '{point_id}': {e}"
                        )
                        continue

                # Save updated index to disk (also updates cache since index is same object)
                total_vectors = len(id_to_label)
                hnsw_manager.save_incremental_update(
                    index, collection_path, id_to_label, label_to_id, total_vectors
                )

                # AC3: Update cache ID mapping (keep cache warm)
                cache_entry.id_mapping = id_mapping

                self.logger.debug(
                    f"Daemon watch mode HNSW update complete for '{collection_name}': "
                    f"{processed} points updated, total vectors: {total_vectors}, "
                    f"cache remains warm"
                )

            finally:
                # AC2: Release write lock
                cache_entry.rw_lock.release_write()

        else:
            # === STANDALONE MODE: Load from disk, update, save to disk ===
            # Load existing index for incremental update
            index, id_to_label, label_to_id, next_label = (
                hnsw_manager.load_for_incremental_update(collection_path)
            )

            if index is None:
                # No existing index - mark as stale for query-time rebuild
                self.logger.debug(
                    f"No existing HNSW index for watch mode update in '{collection_name}', "
                    f"marking as stale"
                )
                hnsw_manager.mark_stale(collection_path)
                return

            # Process each changed point
            processed = 0
            for point in changed_points:
                point_id = point["id"]
                vector = np.array(point["vector"], dtype=np.float32)

                try:
                    # Add or update in HNSW
                    old_count = len(id_to_label)
                    label, id_to_label, label_to_id, next_label = (
                        hnsw_manager.add_or_update_vector(
                            index,
                            point_id,
                            vector,
                            id_to_label,
                            label_to_id,
                            next_label,
                        )
                    )
                    new_count = len(id_to_label)

                    self.logger.debug(
                        f"Standalone watch mode HNSW: added '{point_id}' with label {label}, "
                        f"mappings: {old_count} -> {new_count}, next_label: {next_label}"
                    )

                    processed += 1

                except Exception as e:
                    self.logger.warning(
                        f"Failed to update HNSW for point '{point_id}': {e}"
                    )
                    continue

            # Save updated index to disk
            total_vectors = len(id_to_label)
            hnsw_manager.save_incremental_update(
                index, collection_path, id_to_label, label_to_id, total_vectors
            )

            self.logger.debug(
                f"Standalone watch mode HNSW update complete for '{collection_name}': "
                f"{processed} points updated, total vectors: {total_vectors}"
            )

    def _load_and_validate_incremental_record(
        self,
        point_id: str,
        collection_name: str,
        subdirectory: Optional[str],
        vector_size: int,
    ) -> Optional[Tuple[Dict[str, Any], np.ndarray]]:
        """Bug #1575 Part C: load+validate ONE point for the visibility-aware
        incremental path. Returns ``None`` (never raises) on a missing
        record or a dimension mismatch -- the caller treats ``None`` as
        "abort this incremental attempt", never "skip and continue".
        """
        record = self.get_point(point_id, collection_name, subdirectory)
        if record is None:
            self.logger.info(
                "Visibility-aware incremental update: point '%s' not found "
                "in collection (falling back to full rebuild)",
                point_id,
            )
            return None

        vector = np.array(record["vector"], dtype=np.float32)
        if vector.shape[0] != vector_size:
            self.logger.info(
                "Visibility-aware incremental update: point '%s' has vector "
                "dimension %d, expected %d (falling back to full rebuild)",
                point_id,
                vector.shape[0],
                vector_size,
            )
            return None

        return record, vector

    def _compute_effective_visibility(
        self, payload: Dict[str, Any], session: HNSWSyncSession
    ) -> bool:
        """Bug #1575 Part C: whether a point's stored payload is
        EFFECTIVELY visible under ``session``'s current branch context.

        Unconditionally visible when ``session.branch_context_set`` is
        False (``set_hnsw_branch_context()`` was never called this run --
        no filtering active, matching the legacy unfiltered incremental
        path's semantics). Otherwise: the stored path (normalized
        absolute-vs-relative, Bug #1575 AC6) must be in
        ``session.visible_files`` AND the current branch must NOT be
        listed in the point's ``hidden_branches``.
        """
        from .hnsw_index_manager import _normalize_stored_path_for_visibility

        if not session.branch_context_set:
            return True

        normalized_path = _normalize_stored_path_for_visibility(
            payload.get("path"), self.project_root
        )
        hidden_branches = payload.get("hidden_branches", [])
        hidden = session.current_branch in hidden_branches
        return normalized_path in session.visible_files and not hidden

    def _apply_visibility_aware_incremental_update(
        self,
        collection_name: str,
        collection_path: Path,
        session: HNSWSyncSession,
        progress_callback: Optional[Any] = None,
        clear_stale: bool = True,
        layout_override: Optional[ChunkLayout] = None,
        subdirectory: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Bug #1575 Part C item 7: visibility-aware incremental HNSW
        update, used by the decision engine's "mutation_epoch !=
        published_epoch" branch instead of a full rebuild.

        ANY failure (missing record, dimension mismatch, unexpected
        exception) ABORTS this attempt immediately by returning ``None`` --
        never a ``logger.warning(...); continue``. The caller (the decision
        engine) is responsible for falling back to a fresh full rebuild
        under the SAME lock when this returns ``None``, so a
        partially-applied incremental index is never published.

        Returns ``None`` when there is no existing HNSW index to update
        incrementally (falls back to full rebuild), or a dict with the
        resulting ``vectors`` count on success.
        """
        from .hnsw_index_manager import HNSWIndexManager

        vector_size = self._get_vector_size(collection_name, subdirectory)
        hnsw_manager = HNSWIndexManager(
            vector_dim=vector_size, space="cosine", num_threads=self._hnsw_num_threads
        )

        index, id_to_label, label_to_id, next_label = (
            hnsw_manager.load_for_incremental_update(collection_path)
        )
        if index is None:
            return None

        to_process = (
            session.added | session.updated | session.visibility_changed
        ) - session.deleted
        total_changes = len(to_process) + len(session.deleted)
        processed = 0

        try:
            for point_id in to_process:
                loaded = self._load_and_validate_incremental_record(
                    point_id, collection_name, subdirectory, vector_size
                )
                if loaded is None:
                    return None
                record, vector = loaded

                if self._compute_effective_visibility(
                    record.get("payload", {}), session
                ):
                    _, id_to_label, label_to_id, next_label = (
                        hnsw_manager.add_or_update_vector(
                            index,
                            point_id,
                            vector,
                            id_to_label,
                            label_to_id,
                            next_label,
                        )
                    )
                else:
                    hnsw_manager.remove_vector(
                        index, point_id, id_to_label, label_to_id
                    )

                processed += 1
                if (
                    progress_callback
                    and processed % _INCREMENTAL_PROGRESS_INTERVAL == 0
                ):
                    progress_callback(
                        processed,
                        total_changes,
                        Path(""),
                        info=(
                            f"Visibility-aware incremental HNSW update: "
                            f"{processed}/{total_changes} changes"
                        ),
                    )

            for point_id in session.deleted:
                hnsw_manager.remove_vector(index, point_id, id_to_label, label_to_id)
                processed += 1
        except Exception as exc:
            self.logger.info(
                "Visibility-aware incremental update aborted for '%s': %s "
                "(falling back to full rebuild)",
                collection_name,
                exc,
            )
            return None

        total_vectors = len(id_to_label)
        filtered = session.branch_context_set
        hnsw_manager.save_incremental_update(
            index,
            collection_path,
            id_to_label,
            label_to_id,
            total_vectors,
            clear_stale=clear_stale,
            filtered=filtered,
            current_branch=session.current_branch,
            visible_count=total_vectors if filtered else None,
        )

        if progress_callback:
            progress_callback(
                total_changes,
                total_changes,
                Path(""),
                info=(
                    f"Visibility-aware incremental update complete: "
                    f"{total_changes} changes applied"
                ),
            )

        return {"vectors": total_vectors}
