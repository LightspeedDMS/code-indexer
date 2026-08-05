"""Concurrency-safe in-place refresh of a fixed-path temporal shard (Bug
#1529, "Decision 1 mechanism").

A server-context temporal shard now lives at ONE fixed path forever (see
``temporal_server_paths.py``), which removes every reason to version or
pointer-indirect it -- but it introduces one real problem the versioned design
solved for free: a reader must never observe a shard mid-refresh.

Mechanism (the approved Option 2): reflink-copy the live shard directory to a
scratch SIBLING, apply this run's delta to the copy, verify it, then move the
copy's files onto the live names with ``os.replace`` -- which is atomic
per-file. The reflink copy is near-free (CoW; no real byte duplication until
pages diverge) and uses the project's designated CoW primitive
(``CloneBackend.create_clone_at_path``), never a direct ``cp --reflink``
subprocess call, which this project's AC15 anti-orphan lint gate bans.

WHY NOT AN ATOMIC DIRECTORY SWAP: ``os.replace`` cannot atomically replace a
NON-EMPTY directory -- that is precisely why Story #1457 reached for alias
pointers. Per-file replacement is therefore the strongest guarantee available
without reintroducing pointer indirection.

KNOWN, DOCUMENTED RESIDUAL (flagged deliberately, not hidden): the swap is
atomic per FILE, not across the whole set. ``chunks.db`` is swapped first, so
the only state a reader can observe between the chunk swap and the index swap
is "new rows present, not yet reachable through the HNSW graph" -- incomplete,
never torn or corrupt. ``hnsw_index.bin`` and ``collection_meta.json`` are,
however, a genuinely COUPLED pair (the integer-label -> point_id bridge lives
in the metadata's ``hnsw_index.id_mapping``), and no ``os.replace`` sequence
can land both as one unit. They are swapped back-to-back to keep that window
as small as possible, and a reader that loads a new graph against a
not-yet-swapped mapping can only fail to resolve labels the old mapping does
not know -- it drops those candidates rather than mis-resolving existing ones,
PROVIDED the rebuild is additive. A full renumbering rebuild would widen this
to a genuine mis-resolution window. Callers that renumber must not rely on
this helper alone.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

#: Swapped in this order, then every remaining file. chunks.db first so the
#: worst observable intermediate state is "rows present, not yet indexed".
#: The HNSW pair is adjacent to minimize their coupled window (see module
#: docstring's documented residual).
_SWAP_ORDER = ("chunks.db", "hnsw_index.bin", "collection_meta.json")


class TemporalAtomicRefreshError(RuntimeError):
    """A refresh could not be completed safely; the live shard is UNCHANGED.

    Raised for a failed reflink copy, a failed delta application, or a failed
    verification -- all of which happen entirely against the scratch copy,
    before any live file is touched. Fail loud: a caller must never treat this
    as "refresh finished".
    """


def _fsync_dir(path: Path) -> None:
    """Durably persist directory-entry changes (the renames)."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ordered_files(scratch_dir: Path) -> List[str]:
    """Every regular file in the scratch copy, in swap order."""
    try:
        present = {p.name for p in scratch_dir.iterdir() if p.is_file()}
    except OSError as exc:
        raise TemporalAtomicRefreshError(
            f"could not enumerate refreshed shard at {scratch_dir}: {exc}"
        ) from exc

    ordered = [name for name in _SWAP_ORDER if name in present]
    ordered.extend(sorted(present - set(ordered)))
    return ordered


def refresh_temporal_shard_atomically(
    live_shard_dir: Path,
    apply_delta: Callable[[Path], None],
    *,
    clone_backend: Any,
    verify: Optional[Callable[[Path], None]] = None,
) -> None:
    """Refresh ``live_shard_dir`` without ever exposing a partial state.

    Args:
        live_shard_dir: The fixed shard directory to refresh in place.
        apply_delta: Called with the SCRATCH directory. Must apply this run's
            new rows/index there. Any exception aborts the refresh with the
            live shard untouched.
        clone_backend: Object exposing ``create_clone_at_path(src, dest)`` --
            the project's designated ``cp --reflink=auto`` CoW primitive.
            Required; a None backend raises rather than silently degrading to
            a full byte copy.
        verify: Optional check run against the scratch directory after
            ``apply_delta`` and BEFORE anything live is replaced. Should raise
            to reject the refresh.

    Raises:
        TemporalAtomicRefreshError: the refresh was abandoned. The live shard
            is unchanged and the scratch copy has been cleaned up.
    """
    live_shard_dir = Path(live_shard_dir)
    if clone_backend is None:
        raise TemporalAtomicRefreshError(
            "refresh_temporal_shard_atomically: a clone_backend is required "
            "(the CoW reflink primitive) -- refusing to fall back to a full "
            "byte copy of a potentially multi-gigabyte shard"
        )

    if not live_shard_dir.is_dir():
        # Nothing live to protect: this is a first-ever build, so there is no
        # reader that could observe a partial replacement of something that
        # does not exist yet. Build straight into place.
        live_shard_dir.mkdir(parents=True, exist_ok=True)
        apply_delta(live_shard_dir)
        if verify is not None:
            verify(live_shard_dir)
        return

    scratch_dir = live_shard_dir.parent / (
        f".{live_shard_dir.name}.refresh-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    try:
        if scratch_dir.exists():
            shutil.rmtree(scratch_dir)
        try:
            clone_backend.create_clone_at_path(str(live_shard_dir), str(scratch_dir))
        except Exception as exc:
            raise TemporalAtomicRefreshError(
                f"reflink copy of {live_shard_dir} failed: {exc}"
            ) from exc

        try:
            apply_delta(scratch_dir)
        except Exception as exc:
            raise TemporalAtomicRefreshError(
                f"applying the refresh delta to {scratch_dir} failed "
                f"(live shard untouched): {exc}"
            ) from exc

        if verify is not None:
            try:
                verify(scratch_dir)
            except Exception as exc:
                raise TemporalAtomicRefreshError(
                    f"verification of the refreshed shard at {scratch_dir} "
                    f"failed (live shard untouched): {exc}"
                ) from exc

        # Point of no return: from here the live shard is being updated. Every
        # individual rename is atomic, so a concurrent reader always sees a
        # whole file -- old or new, never half of either.
        for name in _ordered_files(scratch_dir):
            os.replace(str(scratch_dir / name), str(live_shard_dir / name))
        _fsync_dir(live_shard_dir)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
