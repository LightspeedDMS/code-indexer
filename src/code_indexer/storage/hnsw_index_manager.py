"""HNSW-based index manager for fast vector search in filesystem storage.

Provides alternative to binary index using Hierarchical Navigable Small World (HNSW)
algorithm for approximate nearest neighbor search with better query performance.
"""

import fcntl
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from code_indexer.utils.file_locking import (
    nfs_safe_flock,
    nfs_safe_funlock,
    nfs_safe_fsync,
)

import numpy as np

logger = logging.getLogger(__name__)

# Try to import hnswlib, gracefully degrade if not available
try:
    import hnswlib

    HNSWLIB_AVAILABLE = True
except ImportError:
    HNSWLIB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Corruption helpers (Bug #1223 extension)
# ---------------------------------------------------------------------------


HNSW_ORPHAN_REPAIR_MARKER = "HNSW_ORPHAN_REPAIR_EVENT"
"""Bug #1388: machine-readable marker prefix identifying an orphan
detect+repair event, emitted as a plain, unbuffered line on the process's
**stderr** (as opposed to the `logger.info`/`logger.error` calls in
`_detect_and_repair_orphans`, which never surface outside a `cidx`
subprocess -- the CLI's own `setup_logging()` sets the root logger to
WARNING, and the SQLiteLogHandler that would otherwise persist INFO records
to the server's logs.db is only installed during the SERVER's own lifespan
startup, never inherited across a subprocess boundary).

Bug #1388 remediation history: the first attempt threaded this marker
through the `progress_callback`/--progress-json wire protocol as a
`total=0` event. That was proven wrong by two independent, compounding
real-boundary gates: (a) the real --progress-json child callback in
cli.py drops every event where `total <= 0`, so the marker died inside the
child before reaching stdout; (b) even if it had reached stdout, the
parent's `run_with_popen_progress` monotonic high-water `_emit` guard would
have suppressed it too, since HNSW finalize happens at the end of the
semantic phase when the high-water mark is already near the phase's
`range_end`. stderr is immune to both: it is not logging (so the WARNING
filter never applies) and not the JSON progress wire (so neither gate
applies). The parent-side scraping half of this fix lives in
`progress_subprocess_runner.run_with_popen_progress`, which reads the
child's captured stderr for lines with this prefix and forwards them to a
dedicated `orphan_event_callback` -- never through the monotonic
percentage channel. Callers on the server side (e.g.
`golden_repo_manager.py`, `refresh_scheduler.py`) build that callback to
re-log a forwarded marker line through the SERVER's own logger, which DOES
reach logs.db. Import this constant rather than hardcoding the literal
string (Messi Rule #4 anti-duplication)."""


def _emit_hnsw_orphan_repair_stderr_event(
    *, context: str, orphan_count: int, repaired: bool, remaining: Optional[int]
) -> None:
    """Bug #1388: print the orphan-repair marker as a plain, unbuffered
    stderr line -- never through progress_callback/emit_progress_json (see
    HNSW_ORPHAN_REPAIR_MARKER docstring for why that channel is unusable).
    """
    parts = [
        HNSW_ORPHAN_REPAIR_MARKER + ":",
        f"context={context}",
        f"orphan_count={orphan_count}",
        f"repaired={'true' if repaired else 'false'}",
    ]
    if remaining is not None:
        parts.append(f"remaining={remaining}")
    print(" ".join(parts), file=sys.stderr, flush=True)


class HNSWIntegrityRepairError(RuntimeError):
    """Raised when finalize-time orphan repair fails to reach zero orphans.

    Story #1359 (Epic #1333, S2): every build/finalize path runs
    check_integrity() -> repair_orphans() -> re-verify before the index is
    considered done. S1's repair_orphans() is deterministic and proven to
    drive orphan_count to exactly 0 for both measured regimes (near-tie,
    exact-tie), so this should never fire in practice -- if it ever does,
    that is itself the signal something is wrong (a genuine repair-pipeline
    regression), and it must surface loudly rather than publish a silent
    partial index (Messi Rule 2: fail fast, no fallback).
    """


# Bug #1392: informational only -- the actual pin source of truth is
# pyproject.toml's `hnswlib @ git+...@<commit>` dependency line. This
# constant exists purely to make capability-check messages actionable (name
# the expected commit); it does not enforce anything and must be kept in
# sync with pyproject.toml manually. Also imported by
# `server/services/hnswlib_capability_check.py` (Bug #1392's non-fatal
# server startup probe, unchanged/reused by Bug #1415).
EXPECTED_HNSWLIB_FORK_COMMIT = "878cfbe585395a8bdd95f593d071f778d2fac457"


class HNSWRebuildAllInvalidError(RuntimeError):
    """Bug #1407 Amendment 5: raised by rebuild_from_vectors(clear_stale=False)

    when vector files EXIST on disk but ALL fail to parse/validate (JSON
    decode error, missing/malformed 'id' or 'vector', or dimension
    mismatch). This is NOT a legitimate empty shard -- it is a failure --
    so staleness must be retained rather than a stale index silently
    getting blessed as fresh. Only raised on the clear_stale=False
    (force_full_rebuild) path; the default clear_stale=True path preserves
    today's lenient bare-return-0 behavior unchanged for the whole
    non-temporal fleet.
    """


def _fsync_directory(path: Path) -> None:
    """Fsync a directory so entries created/replaced within it survive a
    crash/power-loss (precedent: id_index_manager.py's save_index()).
    """
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def count_orphan_errors(integrity_result: Dict[str, Any]) -> int:
    """Count orphan-node entries in a check_integrity() errors list.

    check_integrity() does not expose a structured orphan_count field --
    orphans are reported as entries in the `errors` list containing the
    substring "orphan" (verified against the real fork; see S1's own
    `_orphan_ids` test helper pattern).
    """
    return sum(1 for e in integrity_result.get("errors", []) if "orphan" in e)


def _is_corrupt_index_error(exc: BaseException) -> bool:
    """Return True if *exc* is hnswlib's corrupt-index RuntimeError.

    hnswlib raises RuntimeError("Index seems to be corrupted or unsupported")
    for both truncated and garbage binary files.  Match is case-insensitive and
    substring-based so minor hnswlib version wording differences are tolerated.

    Returns False for any non-RuntimeError or for unrelated RuntimeErrors such
    as the "contiguous 2D array" query error.
    """
    if not isinstance(exc, RuntimeError):
        return False
    return "corrupted or unsupported" in str(exc).lower()


def discard_corrupt_index(collection_path: Path) -> None:
    """Remove a corrupt hnsw_index.bin and any stale .tmp_hnsw_*.tmp files.

    This is the INDEX-TIME recovery helper.  It MUST NOT be called from the
    query path — queries have no data to rebuild from and must raise on
    corruption so the operator is alerted.

    Removes:
      - ``collection_path / hnsw_index.bin``  (if it exists)
      - All ``collection_path / .tmp_hnsw_*.tmp`` files (orphaned from a
        crashed ``save_index`` temp+rename sequence)

    Does NOT remove vector JSON files, collection_meta.json, or any other
    collection data.  Safe to call when .bin is absent (no-op).
    """
    index_file = collection_path / HNSWIndexManager.INDEX_FILENAME
    if index_file.exists():
        try:
            index_file.unlink()
        except OSError as e:
            logger.warning(
                "discard_corrupt_index: could not remove corrupt index %s: %s",
                index_file,
                e,
            )

    for stale_tmp in collection_path.glob(".tmp_hnsw_*.tmp"):
        try:
            stale_tmp.unlink()
        except OSError as e:
            logger.warning(
                "discard_corrupt_index: could not remove stale temp file %s: %s",
                stale_tmp,
                e,
            )


class HNSWIndexManager:
    """Manages HNSW index for fast approximate nearest neighbor search.

    Provides:
    - Build HNSW index from vectors (full rebuild only)
    - Save/load index to/from single binary file
    - Fast k-NN queries with configurable accuracy
    - Metadata tracking in collection_meta.json
    """

    INDEX_FILENAME = "hnsw_index.bin"
    VALID_SPACES = {"cosine", "l2", "ip"}  # inner product

    def __init__(self, vector_dim: int = 1536, space: str = "cosine"):
        """Initialize HNSW index manager.

        Args:
            vector_dim: Dimension of vectors (default 1536 for voyage-code-3)
            space: Distance metric ('cosine', 'l2', or 'ip')

        Raises:
            ImportError: If hnswlib is not installed
            ValueError: If space metric is invalid
        """
        if not HNSWLIB_AVAILABLE:
            raise ImportError(
                "hnswlib is not installed. Install with: pip install hnswlib"
            )

        if space not in self.VALID_SPACES:
            raise ValueError(
                f"Invalid space metric: {space}. Must be one of {self.VALID_SPACES}"
            )

        self.vector_dim = vector_dim
        self.space = space

    def _hnswlib_has_fork_capability(self) -> bool:
        """Return True iff hnswlib.Index has the custom fork's
        check_integrity()/repair_orphans() methods.

        Bug #1415: non-raising predicate, replacing Bug #1392's
        `_ensure_hnswlib_capability()` (which raised `HNSWCapabilityError`
        as the very first statement of every build/finalize entry point).
        That fail-fast design still aborted the ENTIRE indexing operation on
        a drifted (stock PyPI) hnswlib install -- a fleet-wide production
        outage (~12 golden repos, plus an activated-repo branch-delta
        reindex blocked by Bug #1203's correctness-first design). The
        replacement design never hard-gates build/finalize entry points;
        only `_detect_and_repair_orphans()` consults this predicate, and
        degrades (WARNING + skip) rather than raising.
        """
        return hasattr(hnswlib.Index, "check_integrity") and hasattr(
            hnswlib.Index, "repair_orphans"
        )

    def _save_hnsw_index(self, index: Any, path: str) -> None:
        # Any: hnswlib.Index is a C extension with no Python type stubs
        index.save_index(path)

    def _detect_and_repair_orphans(self, index: Any, context: str) -> None:
        """Detect + repair HNSW orphans on an in-memory index before it is
        persisted (Story #1359 AC1/AC2).

        Runs check_integrity() and logs orphan_count (AC1 -- count only, no
        ratio: there is no threshold that consumes it, per the AC4
        zero-tolerance KISS design). If orphan_count > 0, runs S1's
        repair_orphans() and re-verifies via check_integrity() BEFORE the
        caller's atomic swap publishes the index (AC2). A repair that fails
        to reach zero orphans fails LOUD -- never a silent partial index.

        Near-tie detect+repair is the EXPECTED happy path for temporal
        rebuilds (not an anomaly), so the "detected, repairing" line logs at
        INFO -- WARNING is reserved for the non-convergence failure case, so
        this does not trip the Story #1122 post-E2E log-audit gate on
        ordinary, successful rebuilds.

        Bug #1388: the logger.info/logger.error calls above never surface
        outside a `cidx` subprocess (see HNSW_ORPHAN_REPAIR_MARKER docstring
        for why), so when orphan_count > 0 -- the one case actually worth
        surfacing -- this also emits a single HNSW_ORPHAN_REPAIR_MARKER
        line on stderr via `_emit_hnsw_orphan_repair_stderr_event`, printed
        unconditionally (no caller-supplied callback required). The routine
        orphan_count == 0 case does NOT use this channel -- proportionate:
        only the interesting event is emitted.

        Args:
            index: hnswlib.Index instance with vectors already added, not
                yet saved to disk.
            context: short label identifying the call site (e.g.
                "build_index", "rebuild_from_vectors",
                "incremental_update") for log correlation.

        Raises:
            HNSWIntegrityRepairError: repair_orphans() ran but orphans
                remain afterward.

        Bug #1415: if the installed hnswlib lacks check_integrity()/
        repair_orphans() (stock PyPI hnswlib, not the custom LightspeedDMS
        fork), this logs ONE WARNING and returns immediately -- the orphan
        hardening pass is skipped, but the caller's build/save proceeds and
        still produces a valid, correct index (orphan repair is a hardening
        layer, not correctness of the vectors themselves). Guarded via
        hasattr (never a try/except AttributeError around the calls below)
        so a genuine AttributeError raised from INSIDE a present
        check_integrity()/repair_orphans() implementation is never
        mis-classified as a missing-capability case.
        """
        if not self._hnswlib_has_fork_capability():
            logger.warning(
                "HNSW finalize (%s): installed hnswlib lacks check_integrity()/"
                "repair_orphans() -- this Python environment does not have "
                "the custom hnswlib fork (expected commit %s) installed. "
                "Skipping orphan detect+repair for this finalize; indexing "
                "will still complete (only the orphan hardening pass is "
                "degraded). See docs/hnswlib-custom-build.md for the "
                "rebuild procedure.",
                context,
                EXPECTED_HNSWLIB_FORK_COMMIT,
            )
            return

        integrity = index.check_integrity()
        orphan_count = count_orphan_errors(integrity)
        logger.info(
            "HNSW finalize integrity check (%s): orphan_count=%d",
            context,
            orphan_count,
        )

        if orphan_count == 0:
            return

        logger.info(
            "HNSW finalize (%s): %d orphan(s) detected, running repair_orphans()",
            context,
            orphan_count,
        )
        index.repair_orphans()

        post_repair = index.check_integrity()
        post_orphan_count = count_orphan_errors(post_repair)

        if post_orphan_count > 0:
            logger.error(
                "HNSW finalize (%s): repair_orphans() failed to eliminate all "
                "orphans (%d remain after repair)",
                context,
                post_orphan_count,
            )
            _emit_hnsw_orphan_repair_stderr_event(
                context=context,
                orphan_count=orphan_count,
                repaired=False,
                remaining=post_orphan_count,
            )
            raise HNSWIntegrityRepairError(
                f"HNSW orphan repair failed for {context}: "
                f"{post_orphan_count} orphan(s) remain after repair_orphans()"
            )

        logger.info(
            "HNSW finalize (%s): repair_orphans() succeeded, orphan_count now 0",
            context,
        )
        _emit_hnsw_orphan_repair_stderr_event(
            context=context,
            orphan_count=orphan_count,
            repaired=True,
            remaining=None,
        )

    def build_index(
        self,
        collection_path: Path,
        vectors: np.ndarray,
        ids: List[str],
        M: int = 16,
        ef_construction: int = 200,
        progress_callback: Optional[Any] = None,
    ) -> None:
        """Build HNSW index from vectors and save to disk.

        Args:
            collection_path: Path to collection directory
            vectors: Numpy array of shape (N, vector_dim)
            ids: List of vector IDs (same length as vectors)
            M: HNSW parameter - number of connections per layer
               (higher = more accurate, larger index)
            ef_construction: HNSW parameter - size of dynamic candidate list
                           (higher = better quality, slower build)
            progress_callback: Optional callback(current, total, file_path, info) for progress tracking

        Raises:
            ValueError: If vector dimensions don't match or IDs length doesn't match
        """
        # Validate inputs
        if vectors.shape[1] != self.vector_dim:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.vector_dim}, "
                f"got {vectors.shape[1]}"
            )

        if len(ids) != len(vectors):
            raise ValueError(
                f"IDs length ({len(ids)}) doesn't match vectors length ({len(vectors)})"
            )

        num_vectors = len(vectors)

        # Create HNSW index
        index = hnswlib.Index(space=self.space, dim=self.vector_dim)
        index.init_index(
            max_elements=num_vectors,
            M=M,
            ef_construction=ef_construction,
            allow_replace_deleted=True,
        )

        # Add vectors to index with labels (use indices as labels)
        # We'll store the ID mapping separately in metadata
        labels = np.arange(num_vectors)

        # Report info message at start
        if progress_callback:
            progress_callback(0, 0, Path(""), info="🔧 Building HNSW index...")
            # DEBUG: Mark full build for manual testing
            progress_callback(
                0,
                0,
                Path(""),
                info=f"🔨 FULL HNSW INDEX BUILD: Creating index from scratch with {num_vectors} vectors",
            )

        index.add_items(vectors, labels)

        # Story #1359 AC1/AC2: detect + repair orphans BEFORE the index is
        # persisted, so a freshly-built index never finalizes with orphans.
        self._detect_and_repair_orphans(
            index,
            context=f"build_index:{collection_path}",
        )

        # Report info message at completion
        if progress_callback:
            progress_callback(0, 0, Path(""), info="🔧 HNSW index built ✓")

        # Save index to disk atomically — temp file + rename prevents corruption on crash
        index_file = collection_path / self.INDEX_FILENAME
        tmp_hnsw_fd, tmp_hnsw_path = tempfile.mkstemp(
            dir=str(collection_path),
            prefix=".tmp_hnsw_",
            suffix=".tmp",
        )
        os.close(tmp_hnsw_fd)  # hnswlib opens by path, not fd
        try:
            index.save_index(tmp_hnsw_path)
            os.replace(tmp_hnsw_path, str(index_file))
        except Exception:
            try:
                os.unlink(tmp_hnsw_path)
            except OSError as cleanup_err:
                # Best-effort cleanup — temp file may already be gone or unlink may
                # fail on a read-only filesystem. Log and discard so the original
                # exception propagates unmodified.
                logger.warning(
                    "Failed to clean up temp HNSW file %s after write error: %s",
                    tmp_hnsw_path,
                    cleanup_err,
                )
            raise

        # Update metadata
        self._update_metadata(
            collection_path=collection_path,
            vector_count=num_vectors,
            M=M,
            ef_construction=ef_construction,
            ids=ids,
            index_file_size=index_file.stat().st_size,
        )

    def load_index(
        self, collection_path: Path, max_elements: int = 1000000
    ) -> Optional[Any]:
        """Load HNSW index from disk.

        Args:
            collection_path: Path to collection directory
            max_elements: Maximum number of elements (for index initialization)

        Returns:
            hnswlib.Index instance or None if index doesn't exist
        """
        index_file = collection_path / self.INDEX_FILENAME

        if not index_file.exists():
            return None

        # Create index instance
        index = hnswlib.Index(space=self.space, dim=self.vector_dim)

        # Load from disk
        index.load_index(str(index_file), max_elements=max_elements)

        return index

    def query(
        self,
        index: Any,
        query_vector: np.ndarray,
        collection_path: Path,
        k: int = 10,
        ef: int = 50,
    ) -> Tuple[List[str], List[float]]:
        """Query HNSW index for k nearest neighbors.

        Args:
            index: hnswlib.Index instance from load_index()
            query_vector: Query vector (1D array of shape (vector_dim,))
            collection_path: Path to collection directory (for loading ID mapping)
            k: Number of nearest neighbors to return
            ef: HNSW query parameter - size of dynamic candidate list
                (higher = more accurate, slower)

        Returns:
            Tuple of (ids, distances) where ids are vector IDs and
            distances are similarity scores

        Raises:
            ValueError: If query vector dimension doesn't match
        """
        # Validate query vector dimension
        if len(query_vector) != self.vector_dim:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.vector_dim}, "
                f"got {len(query_vector)}"
            )

        # Load ID mapping from metadata (reflects actual non-deleted vectors)
        id_mapping = self._load_id_mapping(collection_path)

        # Get actual queryable vector count (excludes soft-deleted)
        # Note: get_current_count() includes soft-deleted vectors, causing errors
        queryable_count = len(id_mapping) if id_mapping else index.get_current_count()

        # Limit k to available queryable vectors.
        # Also cap at index.get_current_count() to prevent hnswlib crash when
        # id_mapping metadata diverges from the binary (e.g. transient mismatch
        # during index refresh). hnswlib throws if k > getCurrentCount().
        k_actual = min(k, queryable_count, index.get_current_count())

        # Ensure k_actual is at least 1 if there are any vectors
        if k_actual == 0 and queryable_count > 0:
            k_actual = 1

        # Bug #743: hnswlib requires ef >= k. Auto-adjust ef upward when needed.
        # Without this, small-corpus repos with ef < k_actual raise:
        #   RuntimeError: Cannot return the results in a contiguous 2D array.
        #   Probably ef or M is too small
        ef_actual = max(ef, k_actual)

        # Set ef parameter for query-time accuracy (must be set after k_actual is known)
        index.set_ef(ef_actual)

        # Query index (returns labels and distances).
        # Bug #954/#948: hnswlib can still raise "contiguous 2D array" even after
        # the ef>=k guard above (e.g. index M parameter too small, corrupted index).
        # Retry with progressively smaller k so callers get partial results rather
        # than a hard failure.  A WARNING is emitted when entering the first retry
        # so operators see the degraded-index signal regardless of retry outcome.
        # Unrelated RuntimeErrors propagate immediately without retry.
        first_exc: Optional[RuntimeError] = None
        labels = distances = None
        for attempt_idx, attempt_k in enumerate(
            [k_actual, max(1, k_actual // 2), max(1, k_actual // 4), 1]
        ):
            if attempt_idx == 1 and first_exc is not None:
                # Entering first retry — warn once so operators see degraded index.
                logger.warning(
                    "knn_query failed with contiguous-2D-array error at k=%d; "
                    "retrying with k=%d (degraded index — ef or M may be too small)",
                    k_actual,
                    attempt_k,
                )
            try:
                labels, distances = index.knn_query(query_vector, k=attempt_k)
                break
            except RuntimeError as exc:
                if "contiguous 2D array" not in str(exc):
                    raise
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None and labels is None:
            raise first_exc

        # Convert labels to IDs — labels/distances are non-None here: the raise above
        # exits if labels is still None after all retries.
        assert labels is not None and distances is not None
        result_ids = [id_mapping.get(int(label), f"vec_{label}") for label in labels[0]]
        result_distances = [float(d) for d in distances[0]]

        return result_ids, result_distances

    def index_exists(self, collection_path: Path) -> bool:
        """Check if HNSW index exists.

        Args:
            collection_path: Path to collection directory

        Returns:
            True if index file exists, False otherwise
        """
        index_file = collection_path / self.INDEX_FILENAME
        return index_file.exists()

    def get_index_stats(self, collection_path: Path) -> Optional[Dict[str, Any]]:
        """Get index statistics from metadata.

        Args:
            collection_path: Path to collection directory

        Returns:
            Dictionary with index statistics or None if index doesn't exist
        """
        meta_file = collection_path / "collection_meta.json"

        if not meta_file.exists():
            return None

        try:
            with open(meta_file) as f:
                metadata = json.load(f)

            if "hnsw_index" not in metadata:
                return None

            hnsw_meta: Dict[str, Any] = metadata["hnsw_index"]
            return hnsw_meta

        except (json.JSONDecodeError, KeyError):
            return None

    def rebuild_from_vectors(
        self,
        collection_path: Path,
        progress_callback: Optional[Any] = None,
        visible_files: Optional[Set[str]] = None,
        current_branch: Optional[str] = None,
        clear_stale: bool = True,
    ) -> int:
        """Rebuild HNSW index by scanning all vector JSON files.

        Uses BackgroundIndexRebuilder for atomic file swapping with exclusive
        locking. Queries can continue using old index during rebuild.

        Args:
            collection_path: Path to collection directory
            progress_callback: Optional callback(current, total, file_path, info) for progress tracking
            visible_files: Optional set of file paths (payload.path values) that should be
                          included in the rebuilt index. When provided, vectors whose
                          payload.path is NOT in this set are skipped, enabling ghost
                          vector elimination during branch isolation.
                          When None (default), all vectors are included (backward compatible).
            current_branch: Optional current branch name. When provided and visible_files is None,
                           vectors whose payload.hidden_branches contains current_branch are
                           excluded. This makes all rebuilds branch-aware (Bug #306 fix).
                           Also stored in HNSW metadata when filtered=True for use by
                           query-time rebuilds after CoW snapshot.
            clear_stale: Bug #1407 Amendment 1 -- when True (default, today's
                         unchanged behavior for the whole fleet), publishes
                         is_stale=False. When False (temporal force-rebuild
                         finalize), preserves the PRIOR is_stale/last_marked_stale
                         (defaulting stale=True for a virgin shard) so only the
                         caller's explicit clear_stale() call can mark it fresh.
                         Also activates Amendment 5's empty-shard contract: a
                         genuinely empty shard (zero vector files) durably
                         publishes vector_count=0 rather than a silent no-op,
                         and files-exist-but-all-invalid raises instead of a
                         silent no-op (never bless a stale index as fresh).

        Returns:
            Number of vectors indexed

        Raises:
            FileNotFoundError: If collection metadata is missing
            HNSWRebuildAllInvalidError: clear_stale=False and vector files
                exist on disk but ALL failed to parse/validate (Amendment 5).
        """
        from .background_index_rebuilder import BackgroundIndexRebuilder

        # Load collection metadata to get vector dimension
        meta_file = collection_path / "collection_meta.json"
        if not meta_file.exists():
            raise FileNotFoundError(f"Collection metadata not found at {meta_file}")

        with open(meta_file) as f:
            metadata = json.load(f)
            expected_dim = metadata.get("vector_dim", self.vector_dim)

        # Clean up orphaned .tmp_hnsw_*.tmp files left by a previous crashed
        # save_index (index-time path only — always safe here because we are
        # about to write a fresh index via BackgroundIndexRebuilder anyway).
        # The corrupt .bin (if any) is NOT removed here; BackgroundIndexRebuilder
        # replaces it atomically via temp+os.replace.
        for stale_tmp in collection_path.glob(".tmp_hnsw_*.tmp"):
            try:
                stale_tmp.unlink()
            except OSError as e:
                logger.warning(
                    "rebuild_from_vectors: could not remove stale temp file %s: %s",
                    stale_tmp,
                    e,
                )

        # Scan all vector JSON files
        vector_files = list(collection_path.rglob("vector_*.json"))
        total_files_on_disk = len(vector_files)

        if total_files_on_disk == 0:
            if visible_files is not None:
                # Write filtered metadata even when no vectors exist
                self._update_metadata(
                    collection_path=collection_path,
                    vector_count=0,
                    M=16,
                    ef_construction=200,
                    ids=[],
                    index_file_size=0,
                    filtered=True,
                    visible_count=0,
                    total_on_disk=0,
                    current_branch=current_branch,
                    clear_stale=clear_stale,
                )
            elif not clear_stale:
                # Amendment 5: legitimate empty shard (zero vector files at
                # all) under force-rebuild -- durably publish empty state
                # rather than a silent no-op that would let a stale prior
                # .bin+id_mapping get blessed as fresh later.
                self._publish_empty_rebuild_state(collection_path)
            return 0

        # Report info message at start
        if progress_callback:
            progress_callback(0, 0, Path(""), info="🔧 Rebuilding HNSW index...")

        # Load all vectors and IDs, applying visibility filter if provided
        vectors_list = []
        ids_list = []

        for vector_file in vector_files:
            try:
                with open(vector_file) as f:
                    data = json.load(f)

                vector = np.array(data["vector"], dtype=np.float32)
                point_id = data["id"]

                # Validate dimension
                if len(vector) != expected_dim:
                    continue  # Skip mismatched dimensions

                # Apply visibility filter: skip vectors for hidden files
                if visible_files is not None:
                    file_path = data.get("payload", {}).get("path")
                    if file_path not in visible_files:
                        continue  # Skip vectors not in visible set
                elif current_branch is not None:
                    # Branch-aware filter: skip vectors hidden for current_branch
                    # (Bug #306: makes ALL rebuilds branch-aware via hidden_branches metadata)
                    payload = data.get("payload", {})
                    hidden_branches = payload.get("hidden_branches", [])
                    if current_branch in hidden_branches:
                        continue  # Skip vectors hidden for this branch

                vectors_list.append(vector)
                ids_list.append(point_id)

            except (json.JSONDecodeError, KeyError, ValueError):
                # Skip malformed files
                continue

        if not vectors_list:
            # No vectors pass the filter - return 0 without building index
            if visible_files is not None:
                # Write filtered metadata showing 0 visible vectors
                index_file = collection_path / self.INDEX_FILENAME
                self._update_metadata(
                    collection_path=collection_path,
                    vector_count=0,
                    M=16,
                    ef_construction=200,
                    ids=[],
                    index_file_size=(
                        index_file.stat().st_size if index_file.exists() else 0
                    ),
                    filtered=True,
                    visible_count=0,
                    total_on_disk=total_files_on_disk,
                    current_branch=current_branch,
                    clear_stale=clear_stale,
                )
                return 0
            if not clear_stale:
                # Amendment 5: files EXIST on disk but ALL failed to parse/
                # validate -- NOT a legitimate empty shard. This is a
                # FAILURE: retain staleness, never silently publish/bless.
                raise HNSWRebuildAllInvalidError(
                    f"rebuild_from_vectors: {total_files_on_disk} vector "
                    f"file(s) found in {collection_path} but ALL failed to "
                    f"parse/validate -- refusing to publish an empty index "
                    f"(staleness retained)"
                )
            return 0

        # Convert to numpy array
        vectors = np.array(vectors_list, dtype=np.float32)

        # Use BackgroundIndexRebuilder for atomic swap with locking
        rebuilder = BackgroundIndexRebuilder(collection_path)
        index_file = collection_path / self.INDEX_FILENAME

        def build_hnsw_index_to_temp(temp_file: Path) -> None:
            """Build HNSW index to temp file."""
            # Create HNSW index
            index = hnswlib.Index(space=self.space, dim=self.vector_dim)
            index.init_index(
                max_elements=len(vectors),
                M=16,
                ef_construction=200,
                allow_replace_deleted=True,
            )

            # Add vectors
            labels = np.arange(len(vectors))
            if progress_callback:
                progress_callback(0, 0, Path(""), info="🔧 Building HNSW index...")
            index.add_items(vectors, labels)

            # Story #1359 AC1/AC2: detect + repair orphans BEFORE the index
            # is persisted. ONE shared code path serves regular, temporal,
            # and multimodal rebuilds (all converge on this method).
            self._detect_and_repair_orphans(
                index,
                context=f"rebuild_from_vectors:{collection_path}",
            )

            # Save to temp file
            index.save_index(str(temp_file))

            if progress_callback:
                progress_callback(0, 0, Path(""), info="🔧 HNSW index built ✓")

        # Rebuild with lock (entire rebuild duration)
        rebuilder.rebuild_with_lock(build_hnsw_index_to_temp, index_file)

        # Update metadata AFTER atomic swap
        # When visible_files is provided, write filtered metadata fields
        if visible_files is not None:
            self._update_metadata(
                collection_path=collection_path,
                vector_count=len(vectors),
                M=16,
                ef_construction=200,
                ids=ids_list,
                index_file_size=index_file.stat().st_size,
                filtered=True,
                visible_count=len(vectors),
                total_on_disk=total_files_on_disk,
                current_branch=current_branch,
                clear_stale=clear_stale,
            )
        else:
            self._update_metadata(
                collection_path=collection_path,
                vector_count=len(vectors),
                M=16,
                ef_construction=200,
                ids=ids_list,
                index_file_size=index_file.stat().st_size,
                clear_stale=clear_stale,
            )

        return len(vectors)

    def _publish_empty_rebuild_state(self, collection_path: Path) -> None:
        """Amendment 5 (Bug #1407): durably publish an empty index state for
        a legitimate empty shard (zero vector files on disk) under
        force-rebuild. Removes any stale .bin so it is no longer queryable,
        then writes metadata with vector_count=0 / empty id_mapping while
        PRESERVING (not clearing) staleness -- only the temporal caller's
        explicit clear_stale() call may mark the shard fresh.
        """
        index_file = collection_path / self.INDEX_FILENAME
        if index_file.exists():
            try:
                index_file.unlink()
            except OSError as exc:
                raise RuntimeError(
                    f"rebuild_from_vectors: failed to remove stale "
                    f"{index_file} while publishing empty index state: {exc}"
                ) from exc
        self._update_metadata(
            collection_path=collection_path,
            vector_count=0,
            M=16,
            ef_construction=200,
            ids=[],
            index_file_size=0,
            clear_stale=False,
        )

    @staticmethod
    def _atomic_write_metadata_durable(collection_path: Path, metadata: dict) -> None:
        """Write collection_meta.json atomically AND durably (Bug #1407
        Foundation): flush+fsync the tmp file before os.replace(), then
        fsync the collection directory so the rename itself survives a
        crash/power-loss.
        """
        meta_file = collection_path / "collection_meta.json"
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_path), suffix=".tmp")
        fd_owned = False
        try:
            try:
                tmp_f = os.fdopen(tmp_fd, "w")
                fd_owned = True
                with tmp_f:
                    json.dump(metadata, tmp_f, indent=2)
                    tmp_f.flush()
                    nfs_safe_fsync(tmp_f.fileno())
                os.replace(tmp_path, str(meta_file))
            finally:
                if not fd_owned:
                    try:
                        os.close(tmp_fd)
                    except OSError:
                        pass  # Already closed or invalid — discard
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_err:
                # Best-effort cleanup — log and discard so original exception propagates
                logger.warning(
                    "Failed to clean up temp metadata file %s: %s",
                    tmp_path,
                    cleanup_err,
                )
            raise
        _fsync_directory(collection_path)

    def _write_stale_flag_durably(self, collection_path: Path, is_stale: bool) -> None:
        """Shared durable writer for mark_stale()/clear_stale() (Bug #1407
        Foundation). No-ops (mirrors the pre-existing guard) when
        collection_meta.json or its 'hnsw_index' key is absent -- never
        fabricates staleness state on a meta-less/virgin shard.

        last_marked_stale is refreshed only when marking stale; clearing
        leaves it untouched as an audit trail (non-blocking precision item).
        """
        meta_file = collection_path / "collection_meta.json"
        lock_file = collection_path / ".metadata.lock"
        lock_file.touch(exist_ok=True)

        with open(lock_file, "r+") as lock_f:
            # Acquire exclusive lock (blocks if query is rebuilding) — NFS-safe
            _used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                if not meta_file.exists():
                    return  # No metadata to mark/clear stale

                with open(meta_file) as f:
                    metadata = json.load(f)

                if "hnsw_index" not in metadata:
                    return  # No HNSW index to mark/clear stale

                metadata["hnsw_index"]["is_stale"] = is_stale
                if is_stale:
                    metadata["hnsw_index"]["last_marked_stale"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                self._atomic_write_metadata_durable(collection_path, metadata)
            finally:
                # Release lock
                nfs_safe_funlock(lock_f.fileno(), _used_lockf)

    def mark_stale(self, collection_path: Path) -> None:
        """Mark HNSW index as stale (needs rebuilding), durably.

        Uses file locking for cross-process coordination. Sets is_stale=true
        in collection metadata to indicate index needs rebuilding. The write
        is crash-durable (Bug #1407 Foundation): metadata content and the
        collection directory are both fsynced so the flag cannot be lost by
        a crash immediately after this call returns.

        Args:
            collection_path: Path to collection directory

        Note:
            Called by watch mode to defer HNSW rebuild until query time, and
            by the temporal per-shard finalize barrier (Bug #1407) before
            any shard mutation.
        """
        self._write_stale_flag_durably(collection_path, is_stale=True)

    def clear_stale(self, collection_path: Path) -> None:
        """Clear HNSW index staleness, durably (Bug #1407 Foundation).

        The ONLY writer permitted to flip is_stale True->False on the
        temporal finalize path: callers pass clear_stale=False to
        save_incremental_update()/rebuild_from_vectors() (via
        end_indexing(clear_stale=False)) so the underlying HNSW+metadata
        writes preserve staleness, and invoke THIS method explicitly and
        ONLY after end_indexing() has returned successfully -- a crash at
        any point before that leaves the shard flagged stale for repair on
        the next run (Amendment 2).

        Mirrors mark_stale()'s guards: no-op when collection_meta.json or
        its 'hnsw_index' key is absent -- never fabricates
        ``{"is_stale": False}`` on a meta-less/virgin shard.

        Args:
            collection_path: Path to collection directory
        """
        self._write_stale_flag_durably(collection_path, is_stale=False)

    def is_stale(self, collection_path: Path) -> bool:
        """Check if HNSW index needs rebuilding.

        Returns True if any of the following conditions are met:
        - is_stale flag is set to True in metadata
        - No metadata exists (no index built yet)
        - hnsw_index key is missing from metadata
        - For filtered rebuilds: stored count != visible_count

        Args:
            collection_path: Path to collection directory

        Returns:
            True if HNSW index needs rebuilding, False if fresh

        Note:
            No locking needed - atomic boolean read. Defaults to True if
            is_stale flag missing (backward compatibility with old metadata).
            No filesystem scan (rglob) is performed - the explicit is_stale
            flag is the sole source of truth for non-filtered indexes.
        """
        meta_file = collection_path / "collection_meta.json"

        if not meta_file.exists():
            return True  # No metadata = needs build

        try:
            with open(meta_file) as f:
                metadata = json.load(f)

            if "hnsw_index" not in metadata:
                return True  # No HNSW index = needs build

            hnsw_info = metadata["hnsw_index"]

            # Check is_stale flag (default to True if missing for backward compatibility)
            is_stale_flag = hnsw_info.get("is_stale", True)
            if is_stale_flag:
                return True

            # Fallback detection: Check for vector count mismatch
            # This catches incremental indexing that bypassed mark_stale()
            # Only perform this check if there are actual vector files (not just HNSW index)
            stored_count = hnsw_info.get("vector_count", 0)

            # Branch isolation fix: When this is a filtered rebuild, compare
            # HNSW count against visible_count (not total disk count).
            # This prevents false-positive staleness after filtered rebuilds where
            # disk has MORE vectors than what's in the HNSW index (that's by design).
            if hnsw_info.get("filtered", False):
                visible_count = hnsw_info.get("visible_count", stored_count)
                if stored_count != visible_count:
                    return True  # HNSW count doesn't match what was rebuilt
                return False  # Filtered rebuild is fresh

            return False  # Fresh index

        except (json.JSONDecodeError, KeyError):
            return True  # Corrupted metadata = needs rebuild

    def _update_metadata(
        self,
        collection_path: Path,
        vector_count: int,
        M: int,
        ef_construction: int,
        ids: List[str],
        index_file_size: int,
        filtered: bool = False,
        visible_count: Optional[int] = None,
        total_on_disk: Optional[int] = None,
        current_branch: Optional[str] = None,
        clear_stale: bool = True,
    ) -> None:
        """Update collection metadata with HNSW index information.

        Args:
            collection_path: Path to collection directory
            vector_count: Number of vectors in index
            M: HNSW M parameter
            ef_construction: HNSW ef_construction parameter
            ids: List of vector IDs
            index_file_size: Size of index file in bytes
            filtered: Whether this was a filtered rebuild (branch isolation).
                      When True, is_stale() will compare HNSW count against
                      visible_count rather than total disk count.
            visible_count: Number of visible vectors included in filtered rebuild.
                          Only meaningful when filtered=True.
            total_on_disk: Total vector files on disk at rebuild time.
                          Only meaningful when filtered=True.
            current_branch: Branch name used for filtering (Bug #306).
                           Only stored when filtered=True. Allows query-time
                           rebuilds after CoW snapshot to pass the branch to
                           rebuild_from_vectors() for hidden_branches filtering.
            clear_stale: Bug #1407 Amendment 1 -- when True (default), writes
                         is_stale=False/last_marked_stale=None (today's
                         unchanged behavior). When False, preserves the
                         PRIOR hnsw_index's is_stale/last_marked_stale
                         (defaulting stale=True when there was no prior
                         hnsw_index -- a virgin shard is never fabricated
                         "fresh").
        """
        import uuid

        meta_file = collection_path / "collection_meta.json"

        # Use file locking to prevent race conditions in concurrent writes
        lock_file = collection_path / ".metadata.lock"
        lock_file.touch(exist_ok=True)

        with open(lock_file, "r+") as lock_f:
            # Acquire exclusive lock — NFS-safe
            _used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                # Load existing metadata or create new
                if meta_file.exists():
                    with open(meta_file) as f:
                        metadata = json.load(f)
                else:
                    metadata = {}

                prior_hnsw = metadata.get("hnsw_index")

                # Create ID mapping (label -> ID)
                id_mapping = {str(i): ids[i] for i in range(len(ids))}

                # Update HNSW index metadata with staleness tracking + rebuild version (AC12)
                hnsw_meta: Dict[str, Any] = {
                    "version": 1,
                    "index_rebuild_uuid": str(
                        uuid.uuid4()
                    ),  # AC12: Track rebuild version
                    "vector_count": vector_count,
                    "vector_dim": self.vector_dim,
                    "M": M,
                    "ef_construction": ef_construction,
                    "space": self.space,
                    "last_rebuild": datetime.now(timezone.utc).isoformat(),
                    "file_size_bytes": index_file_size,
                    "id_mapping": id_mapping,
                }
                if clear_stale:
                    hnsw_meta["is_stale"] = False  # Fresh after rebuild
                    hnsw_meta["last_marked_stale"] = None  # No staleness marking yet
                elif prior_hnsw is not None:
                    hnsw_meta["is_stale"] = prior_hnsw.get("is_stale", True)
                    hnsw_meta["last_marked_stale"] = prior_hnsw.get("last_marked_stale")
                else:
                    # Virgin-shard opt-out default (non-blocking precision
                    # item): never fabricate a fresh meta-less shard.
                    hnsw_meta["is_stale"] = True
                    hnsw_meta["last_marked_stale"] = None

                # Branch isolation: write filtered rebuild metadata
                # This allows is_stale() to compare against visible_count instead
                # of total disk count, preventing false-positive staleness detection.
                if filtered:
                    hnsw_meta["filtered"] = True
                    hnsw_meta["visible_count"] = visible_count
                    hnsw_meta["total_on_disk"] = total_on_disk
                    # Bug #306: Store current_branch so query-time rebuilds after
                    # CoW snapshot can use hidden_branches filtering instead of
                    # overwriting the filtered HNSW with all vectors.
                    if current_branch is not None:
                        hnsw_meta["current_branch"] = current_branch

                metadata["hnsw_index"] = hnsw_meta

                # Save metadata atomically — temp file + rename prevents corruption on crash
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=str(collection_path), suffix=".tmp"
                )
                fd_owned = False
                try:
                    try:
                        tmp_f = os.fdopen(tmp_fd, "w")
                        fd_owned = True
                        with tmp_f:
                            json.dump(metadata, tmp_f, indent=2)
                        os.replace(tmp_path, str(meta_file))
                    finally:
                        if not fd_owned:
                            try:
                                os.close(tmp_fd)
                            except OSError:
                                pass  # Already closed or invalid — discard
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError as cleanup_err:
                        # Best-effort cleanup — log and discard so original exception propagates
                        logger.warning(
                            "Failed to clean up temp metadata file %s: %s",
                            tmp_path,
                            cleanup_err,
                        )
                    raise
            finally:
                # Release lock
                nfs_safe_funlock(lock_f.fileno(), _used_lockf)

    def _load_id_mapping(self, collection_path: Path) -> Dict[int, str]:
        """Load ID mapping from metadata.

        Args:
            collection_path: Path to collection directory

        Returns:
            Dictionary mapping label (int) to vector ID (str)
        """
        meta_file = collection_path / "collection_meta.json"

        if not meta_file.exists():
            return {}

        try:
            with open(meta_file) as f:
                metadata = json.load(f)

            if "hnsw_index" not in metadata:
                return {}

            id_mapping_str = metadata["hnsw_index"].get("id_mapping", {})

            # Convert string keys back to int
            return {int(k): v for k, v in id_mapping_str.items()}

        except (json.JSONDecodeError, KeyError, ValueError):
            return {}

    # === INCREMENTAL UPDATE METHODS (HNSW-001 & HNSW-002) ===

    def load_for_incremental_update(
        self, collection_path: Path
    ) -> Tuple[Optional[Any], Dict[str, int], Dict[int, str], int]:
        """Load HNSW index with metadata for incremental updates.

        Args:
            collection_path: Path to collection directory

        Returns:
            Tuple of (index, id_to_label, label_to_id, next_label)
            - index: hnswlib.Index instance or None if doesn't exist
            - id_to_label: Dict mapping point_id (str) to label (int)
            - label_to_id: Dict mapping label (int) to point_id (str)
            - next_label: Next available label for new vectors

        Note:
            For watch mode real-time updates and batch incremental updates.
        """
        index_file = collection_path / self.INDEX_FILENAME

        if not index_file.exists():
            # No existing index - return empty mappings
            return None, {}, {}, 0

        # Load HNSW index — on corruption (index-time path only) discard and
        # return None so the caller falls back to a full rebuild from vectors.
        # This is safe here because the caller (incremental update / indexing
        # pipeline) is about to write a fresh index anyway.
        # The query-time load_index() path does NOT call this method and still
        # raises on corruption so operators are alerted.
        try:
            index = self.load_index(collection_path, max_elements=1000000)
        except RuntimeError as exc:
            if not _is_corrupt_index_error(exc):
                raise
            logger.warning(
                "Corrupt HNSW index discarded at %s, rebuilding from scratch",
                collection_path,
            )
            discard_corrupt_index(collection_path)
            return None, {}, {}, 0

        # Load ID mappings from metadata
        label_to_id = self._load_id_mapping(collection_path)

        # Create reverse mapping
        id_to_label = {v: k for k, v in label_to_id.items()}

        # Calculate next label
        next_label = max(label_to_id.keys()) + 1 if label_to_id else 0

        return index, id_to_label, label_to_id, next_label

    def add_or_update_vector(
        self,
        index: Any,
        point_id: str,
        vector: np.ndarray,
        id_to_label: Dict[str, int],
        label_to_id: Dict[int, str],
        next_label: int,
    ) -> Tuple[int, Dict[str, int], Dict[int, str], int]:
        """Add new vector or update existing vector in HNSW index.

        Args:
            index: hnswlib.Index instance
            point_id: Point identifier
            vector: Vector to add/update
            id_to_label: Current id_to_label mapping
            label_to_id: Current label_to_id mapping
            next_label: Next available label

        Returns:
            Tuple of (label, updated_id_to_label, updated_label_to_id, updated_next_label)

        Note:
            - For new points: Assigns new label and adds to index
            - For existing points: Reuses label and marks old version as deleted,
              then adds updated version (soft delete + add pattern)
        """
        if point_id in id_to_label:
            # Existing point - reuse label
            label = id_to_label[point_id]

            # Mark old version as deleted (soft delete).
            # Bug #944: hnswlib raises RuntimeError when label is already deleted
            # (concurrent double-delete race). Swallow it — the re-add below
            # proceeds regardless, since the slot is effectively free either way.
            try:
                index.mark_deleted(label)
            except RuntimeError as exc:
                if "already deleted" not in str(exc):
                    raise
                logger.warning(
                    "Tolerated double-delete of HNSW label %d for point %s (desync self-heal)",
                    label,
                    point_id,
                )

            # Add updated version with same label
            # Note: HNSW doesn't support in-place update, so we delete + re-add
            index.add_items(vector.reshape(1, -1), np.array([label]))

            return label, id_to_label, label_to_id, next_label
        else:
            # New point - assign new label
            label = next_label

            # Add to index
            index.add_items(vector.reshape(1, -1), np.array([label]))

            # Update mappings
            id_to_label[point_id] = label
            label_to_id[label] = point_id

            return label, id_to_label, label_to_id, next_label + 1

    def remove_vector(
        self,
        index: Any,
        point_id: str,
        id_to_label: Dict[str, int],
        label_to_id: Optional[Dict[int, str]] = None,
    ) -> None:
        """Remove vector from HNSW index using soft delete and clean up mappings.

        Args:
            index: hnswlib.Index instance
            point_id: Point identifier to remove
            id_to_label: Current id_to_label mapping
            label_to_id: Current label_to_id mapping (optional for backward compatibility)

        Note:
            Uses HNSW soft delete (mark_deleted) which filters results during search.
            Physical removal is NOT performed - the vector remains in the index structure
            but won't appear in search results.

            CRITICAL: Also removes the point_id from id_to_label and label_to_id mappings
            to prevent stale metadata from causing duplicate results in queries (Story #540).
        """
        if point_id in id_to_label:
            label = id_to_label[point_id]
            try:
                index.mark_deleted(label)
            except RuntimeError as exc:
                if "already deleted" not in str(exc):
                    raise
                logger.warning(
                    "Tolerated double-delete of HNSW label %d for point %s (desync self-heal)",
                    label,
                    point_id,
                )

            # Clean up mappings to prevent stale metadata (Story #540)
            del id_to_label[point_id]
            if label_to_id is not None and label in label_to_id:
                del label_to_id[label]

    def save_incremental_update(
        self,
        index: Any,
        collection_path: Path,
        id_to_label: Dict[str, int],
        label_to_id: Dict[int, str],
        vector_count: int,
        clear_stale: bool = True,
    ) -> None:
        """Save HNSW index after incremental updates.

        Args:
            index: hnswlib.Index instance with updates
            collection_path: Path to collection directory
            id_to_label: Updated id_to_label mapping
            label_to_id: Updated label_to_id mapping
            vector_count: Total number of vectors (including deleted)
            clear_stale: Bug #1407 Amendment 1 -- when True (default,
                         today's unchanged behavior for the whole fleet),
                         marks fresh (is_stale=False). When False, preserves
                         the PRIOR is_stale/last_marked_stale (defaulting
                         stale=True for a virgin shard) so only the caller's
                         explicit clear_stale() call can mark it fresh.

        Note:
            Updates both index file and metadata with new mappings.
            Preserves existing HNSW parameters (M, ef_construction).

        """
        # DEBUG: Mark incremental update for manual testing
        current_index_size = index.get_current_count() if index else 0
        num_new_vectors = len(id_to_label)
        # Use INFO level so it's visible in logs
        logger.info(
            f"⚡ INCREMENTAL HNSW UPDATE: Adding/updating {num_new_vectors} vectors (total index size: {current_index_size})"
        )

        # Story #1359 AC1/AC2: detect + repair orphans BEFORE the index is
        # persisted. This is the incremental path's finalize checkpoint --
        # the two single-point add_items sites in add_or_update_vector()
        # batch/defer the full-index integrity check to here rather than
        # running check_integrity() (O(elements)) on every single-point add,
        # which would be impractical for a per-point incremental operation.
        self._detect_and_repair_orphans(
            index,
            context=f"incremental_update:{collection_path}",
        )

        # Save index to disk atomically — temp file + rename prevents corruption on crash
        index_file = collection_path / self.INDEX_FILENAME
        tmp_hnsw_fd, tmp_hnsw_path = tempfile.mkstemp(
            dir=str(collection_path),
            prefix=".tmp_hnsw_",
            suffix=".tmp",
        )
        os.close(tmp_hnsw_fd)  # hnswlib opens by path, not fd
        try:
            self._save_hnsw_index(index, tmp_hnsw_path)
            os.replace(tmp_hnsw_path, str(index_file))
        except Exception:
            try:
                os.unlink(tmp_hnsw_path)
            except OSError as cleanup_err:
                # Best-effort cleanup — temp file may already be gone or unlink may
                # fail on a read-only filesystem.  Log and discard so the original
                # exception propagates unmodified.
                logger.warning(
                    "Failed to clean up temp HNSW file %s after write error: %s",
                    tmp_hnsw_path,
                    cleanup_err,
                )
            raise

        # Update metadata with new mappings
        meta_file = collection_path / "collection_meta.json"
        lock_file = collection_path / ".metadata.lock"
        lock_file.touch(exist_ok=True)

        with open(lock_file, "r+") as lock_f:
            # Acquire exclusive lock
            _used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                # Load existing metadata
                if meta_file.exists():
                    with open(meta_file) as f:
                        metadata = json.load(f)
                else:
                    metadata = {}

                # Get existing HNSW config or use defaults
                existing_hnsw = metadata.get("hnsw_index", {})
                M = existing_hnsw.get("M", 16)
                ef_construction = existing_hnsw.get("ef_construction", 200)

                # Create ID mapping (label -> ID) for metadata
                id_mapping = {
                    str(label): point_id for label, point_id in label_to_id.items()
                }

                # Update HNSW index metadata (AC12: preserve or generate new UUID)
                import uuid

                # Generate new UUID for incremental updates too (version tracking)
                new_hnsw: Dict[str, Any] = {
                    "version": 1,
                    "index_rebuild_uuid": str(
                        uuid.uuid4()
                    ),  # AC12: Track rebuild version
                    "vector_count": vector_count,
                    "vector_dim": self.vector_dim,
                    "M": M,
                    "ef_construction": ef_construction,
                    "space": self.space,
                    "last_rebuild": datetime.now(timezone.utc).isoformat(),
                    "file_size_bytes": index_file.stat().st_size,
                    "id_mapping": id_mapping,
                }
                if clear_stale:
                    new_hnsw["is_stale"] = False
                    new_hnsw["last_marked_stale"] = None
                elif existing_hnsw:
                    new_hnsw["is_stale"] = existing_hnsw.get("is_stale", True)
                    new_hnsw["last_marked_stale"] = existing_hnsw.get(
                        "last_marked_stale"
                    )
                else:
                    # Virgin-shard opt-out default: never fabricate fresh.
                    new_hnsw["is_stale"] = True
                    new_hnsw["last_marked_stale"] = None
                metadata["hnsw_index"] = new_hnsw

                # Save metadata atomically — temp file + rename prevents corruption on crash
                tmp_meta_fd, tmp_meta_path = tempfile.mkstemp(
                    dir=str(collection_path), suffix=".tmp"
                )
                fd_owned_by_file_obj = False
                try:
                    try:
                        tmp_f = os.fdopen(tmp_meta_fd, "w")
                        fd_owned_by_file_obj = (
                            True  # fdopen took ownership; do not close fd directly
                        )
                        with tmp_f:
                            json.dump(metadata, tmp_f, indent=2)
                        os.replace(tmp_meta_path, str(meta_file))
                    finally:
                        if not fd_owned_by_file_obj:
                            # fdopen raised before taking ownership — close raw fd explicitly
                            try:
                                os.close(tmp_meta_fd)
                            except OSError:
                                pass  # Already closed or invalid — discard
                except Exception:
                    try:
                        os.unlink(tmp_meta_path)
                    except FileNotFoundError:
                        pass  # Already gone — nothing to clean up
                    raise
            finally:
                # Release lock
                nfs_safe_funlock(lock_f.fileno(), _used_lockf)
