"""HNSW fleet sweep per-item repair executor (Story #1360 AC2).

Given one ``SweepCandidate`` (Component 1/2 discovery output), this module
runs the check-then-repair-then-verify sequence with the concurrency
interlock the issue's "Concurrency interlock" section requires:

  - ``check_integrity()`` may run lock-free (cheap read).
  - Orphans found -> acquire the SAME per-collection lock
    ``HNSWIndexManager``/``BackgroundIndexRebuilder`` finalize/rebuild uses
    (``.index_rebuild.lock``, fcntl flock via ``nfs_safe_flock`` -- already
    cross-process AND NFS-safe, so it is directly reusable here with no new
    locking primitive).
  - RE-CHECK integrity under the lock immediately before writing (the index
    may have changed between the lock-free check and lock acquisition).
  - Write via the same atomic temp-file + ``os.replace`` discipline as a
    rebuild.
  - Re-verify post-repair integrity via a FRESH reload before declaring the
    item complete.
  - Any load/stat failure at any step (ENOENT, corrupt-index-on-reload from
    a concurrent golden-repo refresh swapping the directory mid-repair) is a
    TRANSIENT_SKIP, never an ERROR -- the collection may legitimately no
    longer be the same collection this candidate was discovered against.
  - A repair that runs but fails to converge to zero orphans is loud
    (ERROR, logged) but is NEVER raised -- fail-soft per item, matching the
    story's "a failure on one index does not abort the pass" requirement.
  - A successful repair invalidates the server-side ``HNSWIndexCache`` entry
    for the collection (the same cache the query path serves from), so a
    running server sees the fix without a restart -- confirmed during
    implementation: ``FilesystemVectorStore`` already invalidates this same
    cache after every in-process rebuild via
    ``self.hnsw_index_cache.invalidate(str(collection_path))``; the sweep
    reaches the SAME global singleton via
    ``code_indexer.server.cache.get_global_cache()``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from code_indexer.storage.background_index_rebuilder import BackgroundIndexRebuilder
from code_indexer.storage.hnsw_index_manager import (
    HNSWIndexManager,
    count_orphan_errors,
)
from code_indexer.server.services.hnsw_orphan_sweep.discovery import SweepCandidate
from code_indexer.server.storage.shared.snapshot_paths import is_versioned_snapshot

logger = logging.getLogger(__name__)

# Load/stat failures that mean "the collection is not usable right now" --
# always transient (ENOENT races, concurrent-refresh corrupt-index-on-reload,
# malformed metadata). Deliberately broad: distinguishing "genuinely corrupt"
# from "path identity changed mid-repair" is not reliably possible from the
# exception alone, and both are transient per the concurrency interlock spec.
_TRANSIENT_LOAD_ERRORS = (OSError, RuntimeError, ValueError, KeyError)


class SweepOutcome(str, Enum):
    """Per-item result of the fleet sweep's check+repair sequence."""

    CLEAN = "clean"
    REPAIRED = "repaired"
    TRANSIENT_SKIP = "transient_skip"
    ERROR = "error"


def _default_cache_invalidator(collection_path: str) -> None:
    """Best-effort invalidation of the server-side HNSWIndexCache singleton.

    Lazily imported so this module has no hard dependency on the cache
    singleton being initialized (e.g. under CLI/solo or unit tests that
    inject their own ``cache_invalidator``).
    """
    try:
        from code_indexer.server.cache import get_global_cache

        get_global_cache().invalidate(collection_path)
    except Exception as exc:  # noqa: BLE001 -- best-effort, never block a repair
        logger.warning(
            "hnsw_orphan_sweep: could not invalidate HNSWIndexCache for %s: %s",
            collection_path,
            exc,
        )


def _resolve_collection_context(collection_path: Path) -> Optional[Any]:
    """Read collection_meta.json and build an HNSWIndexManager for this
    collection. Returns None (transient) on any missing file or malformed
    metadata."""
    meta_path = collection_path / "collection_meta.json"
    bin_path = collection_path / HNSWIndexManager.INDEX_FILENAME
    try:
        if not meta_path.is_file() or not bin_path.is_file():
            return None
        with open(meta_path) as f:
            meta = json.load(f)
    except _TRANSIENT_LOAD_ERRORS:
        return None

    hnsw_meta = meta.get("hnsw_index") or {}
    vector_dim = hnsw_meta.get("vector_dim") or meta.get("vector_dim")
    if not vector_dim:
        return None
    space = hnsw_meta.get("space", "cosine")

    try:
        return HNSWIndexManager(vector_dim=int(vector_dim), space=space)
    except _TRANSIENT_LOAD_ERRORS:
        return None


def _persist_repaired_index(locked_index: Any, collection_path: Path) -> bool:
    """Write *locked_index* to disk via the same atomic temp + os.replace
    discipline a rebuild uses. Returns True on success, False (logged) on
    any write failure."""
    bin_path = collection_path / HNSWIndexManager.INDEX_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(collection_path), prefix=".tmp_hnsw_", suffix=".tmp"
    )
    os.close(tmp_fd)
    try:
        locked_index.save_index(tmp_path)
        os.replace(tmp_path, str(bin_path))
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.error(
            "hnsw_orphan_sweep: failed to persist repaired index for %s",
            collection_path,
            exc_info=True,
        )
        return False


def _verify_post_repair(manager: Any, collection_path: Path) -> bool:
    """Re-verify integrity via a FRESH reload after a repair write.
    Returns True iff the fresh reload shows zero orphans."""
    try:
        reloaded = manager.load_index(collection_path)
    except _TRANSIENT_LOAD_ERRORS:
        reloaded = None
    if reloaded is None:
        logger.error(
            "hnsw_orphan_sweep: post-repair reload failed for %s", collection_path
        )
        return False
    verify = reloaded.check_integrity()
    orphan_count = count_orphan_errors(verify)
    if orphan_count > 0:
        logger.error(
            "hnsw_orphan_sweep: post-repair reload still shows %d orphan(s) for %s",
            orphan_count,
            collection_path,
        )
        return False
    return True


def _repair_under_lock(manager: Any, collection_path: Path) -> SweepOutcome:
    """Runs entirely inside the per-collection rebuild lock: re-check,
    repair, persist, re-verify. Called only after the lock-free check found
    orphans and the lock has just been acquired."""
    if is_versioned_snapshot(str(collection_path)):
        return SweepOutcome.TRANSIENT_SKIP

    # RE-CHECK under the lock -- the index may have changed between the
    # lock-free check and acquiring the lock.
    try:
        locked_index = manager.load_index(collection_path)
    except _TRANSIENT_LOAD_ERRORS:
        return SweepOutcome.TRANSIENT_SKIP
    if locked_index is None:
        return SweepOutcome.TRANSIENT_SKIP

    try:
        recheck = locked_index.check_integrity()
    except _TRANSIENT_LOAD_ERRORS:
        return SweepOutcome.TRANSIENT_SKIP

    if count_orphan_errors(recheck) == 0:
        # Someone else (S2's own finalize path, or a racing sweep tick)
        # already repaired it.
        return SweepOutcome.CLEAN

    locked_index.repair_orphans()

    post = locked_index.check_integrity()
    if count_orphan_errors(post) > 0:
        logger.error(
            "hnsw_orphan_sweep: repair_orphans() failed to converge for %s "
            "(%d orphan(s) remain)",
            collection_path,
            count_orphan_errors(post),
        )
        return SweepOutcome.ERROR

    if not _persist_repaired_index(locked_index, collection_path):
        return SweepOutcome.ERROR

    if not _verify_post_repair(manager, collection_path):
        return SweepOutcome.ERROR

    return SweepOutcome.REPAIRED


def process_candidate(
    candidate: SweepCandidate,
    *,
    cache_invalidator: Optional[Callable[[str], None]] = None,
) -> SweepOutcome:
    """Check (and repair, if needed) one HNSW collection.

    Args:
        candidate: A SweepCandidate produced by Component 1/2 discovery.
        cache_invalidator: Optional callable(collection_path_str) invoked
            after a successful repair. Defaults to invalidating the real
            server-side HNSWIndexCache singleton; tests may inject a
            recording callable instead.

    Returns:
        SweepOutcome describing what happened.
    """
    invalidate = cache_invalidator or _default_cache_invalidator
    collection_path = candidate.repo_root / candidate.index_relpath.parent

    if is_versioned_snapshot(str(collection_path)):
        return SweepOutcome.TRANSIENT_SKIP

    manager = _resolve_collection_context(collection_path)
    if manager is None:
        return SweepOutcome.TRANSIENT_SKIP

    # --- Lock-free check ---------------------------------------------------
    try:
        index = manager.load_index(collection_path)
    except _TRANSIENT_LOAD_ERRORS:
        return SweepOutcome.TRANSIENT_SKIP
    if index is None:
        return SweepOutcome.TRANSIENT_SKIP

    try:
        integrity = index.check_integrity()
    except _TRANSIENT_LOAD_ERRORS:
        return SweepOutcome.TRANSIENT_SKIP

    if count_orphan_errors(integrity) == 0:
        return SweepOutcome.CLEAN

    # --- Orphans found: acquire the SAME per-collection lock the build/
    # finalize path uses before writing ------------------------------------
    rebuilder = BackgroundIndexRebuilder(collection_path)
    try:
        with rebuilder.acquire_lock():
            outcome = _repair_under_lock(manager, collection_path)
    except _TRANSIENT_LOAD_ERRORS as exc:
        logger.debug(
            "hnsw_orphan_sweep: transient error repairing %s: %s",
            collection_path,
            exc,
        )
        return SweepOutcome.TRANSIENT_SKIP

    if outcome == SweepOutcome.REPAIRED:
        invalidate(str(collection_path))
        logger.info("hnsw_orphan_sweep: repaired orphans for %s", collection_path)

    return outcome
