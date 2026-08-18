"""Bug #1575 Part C -- Visibility Epoch + Complete Affected-ID Tracking.

Persisted synchronization state (an optional ``hnsw_sync`` object living at
the TOP LEVEL of ``collection_meta.json``, a sibling of the existing
``metadata``/``hnsw_index``/``chunks_db`` keys) plus the in-memory
per-collection session used to decide, at ``end_indexing()``, whether a full
filtered rebuild, a visibility-aware incremental update, or a byte-for-byte
reuse of the existing HNSW artifact is safe.

Schema (additive, ignored by old readers, preserved by all writers)::

    {
      "hnsw_sync": {
        "schema_version": 1,
        "mutation_epoch": 42,
        "published_epoch": 42,
        "status": "clean",
        "current_branch": "main",
        "layout": "chunks_db"
      }
    }

Field rules, enforced by :func:`parse_hnsw_sync_state`:

- ``mutation_epoch``/``published_epoch``: non-negative integers (a ``bool``
  is explicitly rejected even though ``isinstance(True, int)`` is ``True`` in
  Python -- the same discipline ``chunk_layout.py`` applies to its own
  ``version`` field).
- ``status == "clean"`` iff ``mutation_epoch == published_epoch``;
  ``"dirty"`` iff ``mutation_epoch > published_epoch``. Any other
  relationship is internally inconsistent and invalid.
- ANY missing, malformed, negative, non-integer, overflowing (see
  ``_MAX_EPOCH``), or internally inconsistent value makes the WHOLE state
  invalid -- ``parse_hnsw_sync_state`` returns ``None`` rather than raising,
  so every caller's fail-safe response is uniform: treat as "no valid state"
  and force a full rebuild (Bug #1575 AC10).

This module intentionally owns ONLY the state schema, its durable
read/write, and the epoch-transition arithmetic -- it does not decide WHEN to
transition (that is ``FilesystemVectorStore``'s job) and does not perform any
HNSW/chunk-store I/O itself.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from code_indexer.storage.shared.chunk_layout import ChunkLayout
from code_indexer.utils.file_locking import (
    fsync_directory,
    nfs_safe_flock,
    nfs_safe_fsync,
    nfs_safe_funlock,
)

#: Current schema version for the ``hnsw_sync`` object. Bump only on a
#: genuine breaking change to its shape -- any other value is rejected by
#: ``parse_hnsw_sync_state`` (fail-safe: force a full rebuild rather than
#: guess how to interpret an unknown future/past shape).
HNSW_SYNC_SCHEMA_VERSION = 1

#: Top-level collection_meta.json key.
HNSW_SYNC_KEY = "hnsw_sync"

#: Sane upper bound for an epoch value. This is a defensive corruption guard,
#: not a realistic production ceiling (reaching it would require this many
#: mutation-entry-point calls against a single collection) -- any value
#: beyond it is treated as corrupt/overflowed data (AC49) rather than trusted.
_MAX_EPOCH = 2**53

#: Bug #1575 Part C review fix (Defect 3a bypass 3): a spawned `cidx index`
#: CLI child process (e.g. the server's own `cidx index --fts` subprocess,
#: golden_repo_manager.py) has no `app.state` to inspect via
#: `is_postgres_storage_mode()` -- that probe always returns False in a
#: plain CLI process, regardless of the PARENT server's actual storage
#: mode. The parent sets this env var to "1" (via
#: `build_cidx_subprocess_env`) when it knows it is running in
#: postgres/cluster mode; the child (FilesystemBackend.get_vector_store_client())
#: reads it as a fallback ONLY when no in-process app.state signal
#: (hnsw_index_cache) is available. Absent (the overwhelmingly common
#: standalone-CLI case) preserves today's enabled-by-default behavior
#: exactly.
CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV = "CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE"


def resolve_hnsw_sync_epoch_env_var() -> Dict[str, str]:
    """Single shared resolver for CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV.

    Bug #1575 Part C remediation: every server-side `cidx index` child
    spawn site must call this (never reimplement the check) to decide
    whether to signal postgres/cluster mode to the child. Returns
    {ENV: "1"} in postgres mode, else {} (also on any resolution error,
    logged at debug -- fail safe to the child's enabled-by-default).
    Merge the result ON TOP of an already-built env dict; never pass it
    as base_env.
    """
    try:
        from code_indexer.server.services.config_service import get_config_service

        if get_config_service().get_config().storage_mode == "postgres":
            return {CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV: "1"}
    except Exception as exc:  # noqa: BLE001 -- fail safe: unset/enabled
        logging.debug(
            "Bug #1575 Part C: could not resolve storage mode for "
            "hnsw_sync_epoch env var (defaulting to unset/enabled): %s",
            exc,
        )
    return {}


_VALID_LAYOUT_VALUES = {ChunkLayout.SHARDED_JSON.value, ChunkLayout.CHUNKS_DB.value}
_VALID_STATUSES = {"clean", "dirty"}


@dataclass(frozen=True)
class HNSWSyncState:
    """A VALIDATED ``hnsw_sync`` record. Only ever constructed directly by a
    caller that already knows the values are consistent (e.g. the
    dirty-before-mutation transition), or returned by
    :func:`parse_hnsw_sync_state` after full validation.
    """

    schema_version: int
    mutation_epoch: int
    published_epoch: int
    status: str
    current_branch: Optional[str]
    layout: str

    @property
    def is_clean(self) -> bool:
        return self.status == "clean"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mutation_epoch": self.mutation_epoch,
            "published_epoch": self.published_epoch,
            "status": self.status,
            "current_branch": self.current_branch,
            "layout": self.layout,
        }


def _is_valid_epoch(value: Any) -> bool:
    """Non-negative int, never a bool, never overflowing ``_MAX_EPOCH``."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, int):
        return False
    return 0 <= value <= _MAX_EPOCH


def parse_hnsw_sync_state(raw: Any) -> Optional[HNSWSyncState]:
    """Validate a raw (already-JSON-decoded) ``hnsw_sync`` value.

    Returns ``None`` for ANY missing/malformed/inconsistent shape -- never
    raises. This is the fail-safe contract every caller (both the read side
    and the dirty-before-mutation write side) relies on: "could not prove
    this state is valid" always means "force a full rebuild", never a guess.
    """
    if not isinstance(raw, dict):
        return None

    schema_version = raw.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != HNSW_SYNC_SCHEMA_VERSION
    ):
        return None

    mutation_epoch = raw.get("mutation_epoch")
    published_epoch = raw.get("published_epoch")
    if not _is_valid_epoch(mutation_epoch) or not _is_valid_epoch(published_epoch):
        return None
    # _is_valid_epoch() already guarantees both are non-negative ints --
    # these asserts are pure mypy type-narrowing (Any -> int), zero
    # behavioral change.
    assert isinstance(mutation_epoch, int)
    assert isinstance(published_epoch, int)

    status = raw.get("status")
    # A membership test against a set requires a hashable value -- an
    # unhashable status (e.g. a list/dict) must be rejected explicitly
    # rather than letting `in` raise TypeError (fail-safe: never raise on
    # malformed input, always return None instead).
    if not isinstance(status, str) or status not in _VALID_STATUSES:
        return None

    if status == "clean" and mutation_epoch != published_epoch:
        return None
    if status == "dirty" and not (mutation_epoch > published_epoch):
        return None

    current_branch = raw.get("current_branch")
    if current_branch is not None and not isinstance(current_branch, str):
        return None

    layout = raw.get("layout")
    if not isinstance(layout, str) or layout not in _VALID_LAYOUT_VALUES:
        return None

    return HNSWSyncState(
        schema_version=schema_version,
        mutation_epoch=mutation_epoch,
        published_epoch=published_epoch,
        status=status,
        current_branch=current_branch,
        layout=layout,
    )


def read_collection_meta(collection_path: Path) -> Optional[Dict[str, Any]]:
    """Read+parse ``collection_meta.json`` for ``collection_path``.

    Returns ``None`` on ANY read/parse failure (missing file, unreadable,
    empty, invalid JSON, non-dict root) -- fail-safe, mirroring
    ``resolve_chunk_layout``'s own contract for this exact file.
    """
    meta_file = collection_path / "collection_meta.json"
    try:
        content = meta_file.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    if not content.strip():
        return None
    try:
        meta = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def read_hnsw_sync_state(collection_path: Path) -> Optional[HNSWSyncState]:
    """Read and validate the ``hnsw_sync`` state for ``collection_path``.

    Returns ``None`` when ``collection_meta.json`` is missing/unreadable,
    when it has no ``hnsw_sync`` key (pre-existing collection, AC42), or when
    the stored value fails validation (AC10) -- every case is the uniform
    "force a full rebuild" signal.
    """
    meta = read_collection_meta(collection_path)
    if meta is None:
        return None
    return parse_hnsw_sync_state(meta.get(HNSW_SYNC_KEY))


def _atomic_write_json_durable(target_path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``target_path`` atomically and durably:
    temp file in the same directory, flush+fsync, ``os.replace``, then fsync
    the containing directory so the rename itself survives a crash.

    Mirrors ``HNSWIndexManager._atomic_write_metadata_durable`` /
    ``chunk_layout._atomic_write_json_durable`` -- this file
    (``collection_meta.json``) holds the load-bearing HNSW
    ``id_mapping``, so a bare ``write_text()`` is never acceptable here.
    """
    collection_dir = target_path.parent
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                json.dump(data, tmp_f)
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(target_path))
        finally:
            if not fd_owned:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass  # Already closed or invalid -- discard
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Best-effort cleanup -- discard so original exception propagates
        raise

    fsync_directory(collection_dir)


def write_hnsw_sync_state(collection_path: Path, state: HNSWSyncState) -> None:
    """Durably merge ``{"hnsw_sync": {...}}`` into an EXISTING
    ``collection_meta.json`` -- additive, preserving every other top-level
    key untouched.

    Raises ``FileNotFoundError`` if ``collection_meta.json`` does not
    already exist -- mirrors ``write_chunks_db_discriminator``'s contract:
    this function never creates the base metadata file from scratch, so an
    out-of-order caller fails loudly instead of masking the ordering bug.

    Bug #1575 Part C review Finding 2: the read-merge-write below is
    guarded by ``.metadata.lock`` -- the SAME lock file (and fcntl-based
    flock pattern) ``HNSWIndexManager._update_metadata`` already uses for
    every OTHER writer of this same ``collection_meta.json`` file. Without
    this, a concurrent ``_update_metadata``/``save_incremental_update``
    call (guarded only by its own ``.metadata.lock`` acquisition) and this
    function's read-merge-write had no mutual exclusion against each
    other, allowing either writer's update to be silently lost.
    """
    meta_file = collection_path / "collection_meta.json"
    lock_file = collection_path / ".metadata.lock"
    lock_file.touch(exist_ok=True)

    with open(lock_file, "r+") as lock_f:
        used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            meta = read_collection_meta(collection_path)
            if meta is None:
                raise FileNotFoundError(
                    f"Cannot write hnsw_sync state: {meta_file} does not exist or "
                    f"is unreadable/invalid. write_hnsw_sync_state() must be "
                    f"called AFTER the base collection_meta.json has already "
                    f"been written."
                )
            meta[HNSW_SYNC_KEY] = state.to_dict()
            _atomic_write_json_durable(meta_file, meta)
        finally:
            nfs_safe_funlock(lock_f.fileno(), used_lockf)


def compute_dirty_transition(prior: Optional[HNSWSyncState]) -> Tuple[int, int]:
    """Compute ``(next_mutation_epoch, published_epoch)`` for a
    dirty-before-write transition.

    ``prior`` is the current VALIDATED state (``None`` when missing/invalid
    -- fail-safe: start from a fresh, unambiguous epoch pair). The returned
    pair is always "dirty" (``next_mutation_epoch > published_epoch``).

    AC49: an epoch that would overflow ``_MAX_EPOCH`` is never written --
    the pair wraps back to ``(1, 0)`` instead, which is both bounded and
    unambiguously dirty (forcing the next refresh through a full rebuild,
    after which the pair is naturally reset to a valid, consistent state by
    that rebuild's successful publication).
    """
    if prior is None:
        return 1, 0

    next_mutation = prior.mutation_epoch + 1
    if next_mutation > _MAX_EPOCH:
        return 1, 0
    return next_mutation, prior.published_epoch


@dataclass
class HNSWSyncSession:
    """In-memory, per-PHYSICAL-collection-path session state (Bug #1575 Part
    C). Replaces the bare, unscoped
    ``FilesystemVectorStore._branch_isolation_did_filtered_rebuild`` boolean.

    Keying instances by ``str(collection_path.resolve())`` (done by the
    owning store, not by this dataclass) is what makes one collection's
    rebuild/skip decision structurally unable to affect another's.
    """

    collection_path: Path
    collection_name: str
    layout: ChunkLayout
    start_epoch: int
    current_branch: Optional[str] = None
    visible_files: Set[str] = field(default_factory=set)
    added: Set[str] = field(default_factory=set)
    updated: Set[str] = field(default_factory=set)
    deleted: Set[str] = field(default_factory=set)
    visibility_changed: Set[str] = field(default_factory=set)
    complete_change_tracking: bool = True
    #: Whether ``set_hnsw_branch_context()`` was ever called for THIS
    #: session -- the real signal for "is branch-isolation filtering
    #: active", independent of ``current_branch``'s value. A caller may
    #: legitimately request a filtered rebuild via an explicit
    #: ``visible_files`` set WITHOUT naming a branch (``current_branch``
    #: stays None), so gating filtering on ``current_branch is not None``
    #: would silently produce an UNFILTERED rebuild in that case.
    branch_context_set: bool = False
    #: Bug #1575 Part C review fix (Defect 2): count of
    #: ``_mark_hnsw_dirty_before_mutation`` calls THIS session has
    #: personally issued. Combined with ``start_epoch``, this lets the
    #: decision engine detect a mutation performed by a DIFFERENT session
    #: (this process's own aborted/discarded prior session, another
    #: in-process session, or another process/instance entirely) that
    #: advanced ``mutation_epoch`` without this session's knowledge: if
    #: the on-disk epoch delta since this session started does not equal
    #: this counter, some mutation happened that this session never
    #: tracked, and trusting this session for an incremental publish would
    #: silently drop that mutation from the HNSW graph.
    own_mutation_count: int = 0
