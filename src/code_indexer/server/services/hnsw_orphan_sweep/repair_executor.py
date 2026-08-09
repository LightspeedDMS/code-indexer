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
  - A successful repair attempts to invalidate the server-side
    ``HNSWIndexCache`` entry for the collection (the same cache the query path
    serves from), reaching the SAME global singleton via
    ``code_indexer.server.cache.get_global_cache()``.

    Bug #1542: this eviction used to hand-build a bare resolved-path key,
    which never matched the key ``FilesystemVectorStore.search()`` actually
    stores under (Story #1458 AC11's chunk-layout token, and, for an
    activated repo, its ``activation_id``). Fixed by routing through
    ``hnsw_cache_key_for_collection_path()`` (the module-level sibling of
    FSV's ``hnsw_cache_key_for_collection()``, usable here because the sweep
    has no live FSV instance for the collection it just repaired) -- see
    ``_make_default_cache_invalidator``/``_resolve_activation_id``. A
    running server would still have picked the repair up on its next read
    regardless, via Bug #1538's on-disk identity fingerprint; this fix
    restores the IMMEDIATE eviction the
    function was always meant to provide.
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
from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    SweepCandidate,
)
from code_indexer.server.services.hnswlib_capability_check import (
    check_hnswlib_capability,
)
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
    # Bug #1415: the installed hnswlib lacks check_integrity()/
    # repair_orphans() (stock PyPI hnswlib, not the custom fork). Distinct
    # from TRANSIENT_SKIP -- this condition will NOT resolve on its own on
    # a later tick (unlike a genuinely transient filesystem race).
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    # Bug #1529: the SISTER_TEMPORAL_REPAIRED/_REPAIR_FAILED outcomes are
    # GONE. Server-context temporal shards now live at a FIXED, mutable path
    # ({golden_repos_dir}/.temporal/{alias}/), so they are discovered and
    # repaired IN PLACE by process_candidate() like every other collection --
    # there is no immutable version to rebuild and no alias pointer to swap.


def _resolve_activation_id(
    candidate: SweepCandidate,
    # Any: this module deliberately avoids a hard import of
    # ActivatedRepoManager (matching this file's existing `manager: Any` /
    # `locked_index: Any` duck-typed style throughout) -- callers only need
    # to satisfy `.get_activation_id(username, user_alias) -> Optional[str]`,
    # and tests inject a minimal fake exposing just that one method.
    activated_repo_manager: Optional[Any],
) -> Optional[str]:
    """Resolve the ``activation_id`` for an ACTIVATED-repo candidate so the
    default cache invalidator can compose the SAME activation-scoped key
    ``FilesystemVectorStore`` uses for that repo's query-serving cache entries
    (Story #1458 AC11 Finding 7). Golden and ``golden_temporal`` candidates
    never have one -- returns None immediately for those, matching
    ``FilesystemVectorStore``'s own ``activation_id=None`` default for the
    non-activated case.

    Best-effort, matching ``_make_default_cache_invalidator``'s own
    best-effort contract: no ``activated_repo_manager`` injected, a
    malformed ``candidate.alias`` (not a non-empty ``"username/user_alias"``
    string with EXACTLY two non-empty components), or any resolution failure
    all return None rather than raise -- an activated repo's eviction then
    still falls back to Bug #1538's on-disk identity fingerprint on the next
    read (the pre-existing degraded behavior for every collection before
    this fix), never blocking the repair itself.
    """
    if candidate.kind != "activated" or activated_repo_manager is None:
        return None
    alias = candidate.alias
    parts = alias.split("/") if isinstance(alias, str) else []
    if len(parts) != 2 or not parts[0] or not parts[1]:
        logger.warning(
            "hnsw_orphan_sweep: activated candidate alias %r is not "
            "'username/user_alias'; cache invalidation will use a "
            "path-only key",
            alias,
        )
        return None
    username, user_alias = parts
    try:
        activation_id: Optional[str] = activated_repo_manager.get_activation_id(
            username, user_alias
        )
        return activation_id
    except Exception as exc:  # noqa: BLE001 -- best-effort, never block a repair
        logger.warning(
            "hnsw_orphan_sweep: could not resolve activation_id for %s: %s",
            alias,
            exc,
        )
        return None


def _make_default_cache_invalidator(
    activation_id: Optional[str],
) -> Callable[[str], None]:
    """Build the default best-effort HNSWIndexCache invalidator, bound to
    *activation_id* (None for golden/``golden_temporal`` candidates, resolved
    via ``_resolve_activation_id`` for activated-repo candidates).

    Lazily imported so this module has no hard dependency on the cache
    singleton (or on ``FilesystemVectorStore``'s heavier import chain) being
    initialized (e.g. under CLI/solo or unit tests that inject their own
    ``cache_invalidator``).

    Bug #1542 (+ Codex-review follow-up): a bare ``collection_path`` does NOT
    match the key ``FilesystemVectorStore.search()`` stores the entry under
    -- that key also embeds Story #1458 AC11's chunk-layout token and, for an
    activated repo, its ``activation_id``. This composes the SAME key via
    ``hnsw_cache_key_for_collection_path()``, the module-level sibling of
    ``FilesystemVectorStore.hnsw_cache_key_for_collection()`` usable here
    because the sweep has no live FSV instance for the collection it just
    repaired -- only its resolved path (and, now, the activation_id resolved
    separately by the caller).
    """

    def _invalidate(collection_path: str) -> None:
        try:
            from code_indexer.server.cache import get_global_cache
            from code_indexer.storage.filesystem_vector_store import (
                hnsw_cache_key_for_collection_path,
            )

            canonical_key = hnsw_cache_key_for_collection_path(
                collection_path, activation_id=activation_id
            )
            get_global_cache().invalidate(canonical_key)
        except Exception as exc:  # noqa: BLE001 -- best-effort, never block a repair
            logger.warning(
                "hnsw_orphan_sweep: could not invalidate HNSWIndexCache for %s: %s",
                collection_path,
                exc,
            )

    return _invalidate


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
    activated_repo_manager: Optional[Any] = None,
) -> SweepOutcome:
    """Check (and repair, if needed) one HNSW collection.

    Args:
        candidate: A SweepCandidate produced by Component 1/2 discovery.
        cache_invalidator: Optional callable(collection_path_str) invoked
            after a successful repair. Defaults to invalidating the real
            server-side HNSWIndexCache singleton; tests may inject a
            recording callable instead.
        activated_repo_manager: Optional object exposing
            ``get_activation_id(username, user_alias) -> Optional[str]``
            (``ActivatedRepoManager``'s real signature). Used ONLY when
            ``cache_invalidator`` is not supplied, to resolve the
            activation-scoped cache key for a ``kind == "activated"``
            candidate (Story #1458 AC11 Finding 7, Bug #1542 follow-up).
            Ignored for golden/``golden_temporal`` candidates.

    Returns:
        SweepOutcome describing what happened.
    """
    activation_id = _resolve_activation_id(candidate, activated_repo_manager)
    invalidate = cache_invalidator or _make_default_cache_invalidator(activation_id)
    collection_path = candidate.repo_root / candidate.index_relpath.parent

    if is_versioned_snapshot(str(collection_path)):
        return SweepOutcome.TRANSIENT_SKIP

    manager = _resolve_collection_context(collection_path)
    if manager is None:
        return SweepOutcome.TRANSIENT_SKIP

    # Bug #1415: guard BEFORE the lock-free check_integrity() call below --
    # reuses the existing Bug #1392 server-side capability probe rather than
    # reimplementing the hasattr check a third time. Missing capability logs
    # ONE WARNING and returns immediately; check_integrity()/repair_orphans()
    # are never called, so this candidate cannot AttributeError downstream
    # (in the lock-free check here, nor in _repair_under_lock/
    # _verify_post_repair, which are unreachable from this early return).
    capability_ok, _capability_message = check_hnswlib_capability()
    if not capability_ok:
        logger.warning(
            "hnsw_orphan_sweep: installed hnswlib lacks check_integrity()/"
            "repair_orphans() -- skipping orphan check for %s (degraded "
            "capability, Bug #1415)",
            collection_path,
        )
        return SweepOutcome.CAPABILITY_UNAVAILABLE

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
        # Bug #1542: the DEFAULT invalidator (built by
        # _make_default_cache_invalidator, bound to the activation_id
        # _resolve_activation_id resolved above) re-derives the canonical,
        # chunk-layout-token-and-activation-id-bearing cache key from this
        # bare path before evicting -- passing the bare path itself here
        # keeps injected test callables (which record "which candidate was
        # flagged") receiving the plain collection path, unchanged.
        invalidate(str(collection_path))
        logger.info("hnsw_orphan_sweep: repaired orphans for %s", collection_path)

    return outcome
