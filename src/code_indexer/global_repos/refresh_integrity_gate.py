"""Run-boundary durability flush + integrity gate for ordinary golden-repo
refresh (Bug #1506).

Ordinary refresh (``RefreshScheduler._execute_refresh`` -> ``_index_source``)
writes ``chunks.db`` in place against the live, served ``master_path`` with
zero integrity verification before publish (``_create_snapshot`` /
``swap_alias``). A real staging incident produced genuine SQLite corruption
that went undetected until the indexer process itself crashed.

This module reuses the existing, already-tested primitives Bug #1486 built
for the fleet-migration consolidation path rather than reinventing them:

- ``ChunkStore.flush_durable()`` (``storage/sqlite_chunk_store.py``): an
  explicit ``fsync`` of the db file AND its containing directory, defeating
  the confirmed NFS client-cache-coherency failure mode where a write can be
  reported durable locally before it is actually durable on the NFS server.
- ``collection_migration._check_integrity_fresh_connection``: opens a
  genuinely NEW, read-only connection and runs ``PRAGMA integrity_check``,
  returning ``(False, detail)`` rather than raising. Imported directly
  (rather than reimplemented) per this project's anti-duplication rule --
  it is a small, pure, side-effect-free function with no dependency on the
  rest of the migration module's state.

Scope (Bug #1506): between ``_index_source`` returning successfully and
``_create_snapshot``/``swap_alias``, verify every CHUNKS_DB-layout
collection's ``chunks.db`` is durable and structurally healthy. On failure,
refuse to publish this refresh cycle (the caller simply skips
``_create_snapshot``/``swap_alias`` -- the already-published alias keeps
serving the last verified-good snapshot) and attempt a reflink self-heal of
``master_path``'s corrupt ``chunks.db`` from the corresponding collection in
the currently-aliased ("last known good") snapshot, using the SAME
``cp --reflink=auto`` CoW primitive (``CloneBackend.create_clone_at_path``)
already used throughout this codebase for snapshot cloning -- never a new
direct ``cp --reflink`` subprocess call (this project's AC15 anti-orphan
lint gate bans that).

A SHARDED_JSON-only collection set (no ``chunks.db`` anywhere) has nothing
for this gate to check and is a clean, error-free no-op pass.

Codex review Finding 3 (post-approval hardening): the self-heal path must
never trust an unverified "healthy" snapshot merely because its
``chunks.db`` file exists. Before restoring FROM a snapshot, that
snapshot's own ``chunks.db`` is integrity-checked via the SAME
fresh-connection ``PRAGMA integrity_check`` primitive; a snapshot that
itself fails this check is refused as a restore source (never confidently
copies corruption over ``master_path``) and is reported via
``CollectionIntegrityFailure.source_snapshot_also_corrupt=True`` -- a more
severe condition than an ordinary single-sided failure. After a restore
completes, the RESTORED destination is re-verified via the same primitive
before ``self_heal_succeeded`` is ever set True -- a reflink clone that
completes without raising but produces a corrupt (or re-corrupted)
destination is reported as a self-heal FAILURE, not a success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    _check_integrity_fresh_connection,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

logger = logging.getLogger(__name__)

_CHUNKS_DB_FILENAME = "chunks.db"


@dataclass
class CollectionIntegrityFailure:
    """One CHUNKS_DB collection's integrity-gate failure, plus the outcome
    of the reflink self-heal attempt (if any)."""

    collection_dir: Path
    detail: str
    self_heal_attempted: bool = False
    self_heal_succeeded: bool = False
    self_heal_error: Optional[str] = None
    # Codex review Finding 3: True when the last-known-good snapshot ITSELF
    # failed its own integrity_check -- self-heal was refused entirely
    # rather than confidently copying corruption over master_path. Both the
    # current write AND the last-known-good snapshot are corrupt: a more
    # severe condition than an ordinary single-sided failure.
    source_snapshot_also_corrupt: bool = False


@dataclass
class RefreshIntegrityGateResult:
    """Outcome of :func:`run_refresh_integrity_gate` across every
    CHUNKS_DB-layout collection discovered under the refreshed source
    index directory."""

    passed: bool
    checked_collections: List[Path] = field(default_factory=list)
    failures: List[CollectionIntegrityFailure] = field(default_factory=list)


def discover_chunks_db_collection_dirs(index_dir: Path) -> List[Path]:
    """Return every immediate child of *index_dir* that resolves to
    ``ChunkLayout.CHUNKS_DB``.

    Mirrors ``hnsw_orphan_sweep``'s own discovery convention: a collection
    is a direct child directory of the index dir. A missing/non-directory
    *index_dir* returns ``[]`` rather than raising -- a repo's very first
    refresh, or a refresh that produced no semantic collection at all
    (e.g. a purely-temporal repo), must not error here.
    """
    index_dir = Path(index_dir)
    if not index_dir.exists() or not index_dir.is_dir():
        return []

    collections = []
    for entry in sorted(index_dir.iterdir()):
        if not entry.is_dir():
            continue
        if resolve_chunk_layout(entry) == ChunkLayout.CHUNKS_DB:
            collections.append(entry)
    return collections


def flush_and_check_chunks_db_integrity(chunks_db_path: Path) -> Tuple[bool, str]:
    """Force *chunks_db_path* durable on the actual backing store, then
    verify its integrity via a genuinely fresh, read-only connection.

    Returns ``(True, "ok")`` when durable and healthy; ``(False, detail)``
    otherwise -- including when the durability flush itself raises (e.g. an
    ``OSError`` fsync'ing over NFS), which is folded into the same
    ``False``-with-detail contract rather than propagating a raw exception,
    so a single collection's failure never aborts the whole gate run.
    """
    chunks_db_path = Path(chunks_db_path)
    if not chunks_db_path.exists():
        return False, "chunks.db does not exist"

    try:
        with ChunkStore(chunks_db_path, durable_synchronous=True) as store:
            store.flush_durable()
    except Exception as exc:
        logger.error(
            "Bug #1506: durable flush failed for %s (%s)",
            chunks_db_path,
            exc,
        )
        return False, f"flush_durable failed: {type(exc).__name__}: {exc}"

    ok, detail = _check_integrity_fresh_connection(chunks_db_path)
    return ok, detail


def restore_chunks_db_via_reflink(
    clone_backend: Any,
    healthy_chunks_db_path: Path,
    corrupt_chunks_db_path: Path,
) -> None:
    """Restore *corrupt_chunks_db_path* from *healthy_chunks_db_path* using
    the SAME ``cp --reflink=auto`` CoW clone primitive already used
    throughout this codebase for snapshot cloning
    (``CloneBackend.create_clone_at_path``) -- never a direct subprocess
    ``cp`` call in production code (this project's AC15 anti-orphan lint
    gate bans exactly that).

    Raises whatever the underlying *clone_backend* raises on failure (e.g.
    ``subprocess.CalledProcessError``), or ``RuntimeError`` if no
    *clone_backend* is available -- callers must not swallow this, since a
    failed restore leaves ``master_path`` corrupt and must be logged
    loudly by the caller.
    """
    if clone_backend is None:
        raise RuntimeError(
            "restore_chunks_db_via_reflink: no clone_backend available -- "
            "cannot self-heal without the CoW clone primitive"
        )
    clone_backend.create_clone_at_path(
        str(healthy_chunks_db_path), str(corrupt_chunks_db_path)
    )


def run_refresh_integrity_gate(
    *,
    source_index_dir: Path,
    healthy_index_dir: Optional[Path],
    clone_backend: Optional[Any] = None,
) -> RefreshIntegrityGateResult:
    """Run the Bug #1506 durability-flush + integrity gate against every
    CHUNKS_DB collection under *source_index_dir* (the just-mutated
    ``master_path``'s ``.code-indexer/index/``).

    On any collection's integrity-check failure, attempt a reflink
    self-heal of ITS ``chunks.db`` from the corresponding collection
    directory under *healthy_index_dir* (the currently-aliased,
    last-known-good snapshot's index dir). When *healthy_index_dir* is
    ``None`` (e.g. this repo's very first-ever refresh, with no prior
    snapshot to restore from) or the corresponding collection is absent
    there, self-heal is skipped (reported via
    ``self_heal_attempted=False``) -- the caller must still refuse to
    publish this cycle regardless of whether self-heal happened.

    Codex review Finding 3: before trusting *healthy_index_dir*'s
    ``chunks.db`` as a restore source, it is ITSELF integrity-checked via
    the same fresh-connection primitive. A source that fails this check is
    refused (``self_heal_attempted=False``,
    ``source_snapshot_also_corrupt=True``) -- self-heal must never
    confidently copy corruption over ``master_path``. After a restore
    completes, the destination is re-verified the same way; only a restore
    whose destination passes this post-check is reported as
    ``self_heal_succeeded=True``.

    Never raises for a data-integrity failure -- returns ``passed=False``
    instead so the caller can skip ``_create_snapshot``/``swap_alias`` for
    this cycle gracefully. A SHARDED_JSON-only collection set (no
    ``chunks.db`` anywhere) is a clean no-op pass (``passed=True``,
    ``checked_collections=[]``).
    """
    result = RefreshIntegrityGateResult(passed=True)

    collections = discover_chunks_db_collection_dirs(Path(source_index_dir))
    for collection_dir in collections:
        chunks_db_path = collection_dir / _CHUNKS_DB_FILENAME
        ok, detail = flush_and_check_chunks_db_integrity(chunks_db_path)
        if ok:
            result.checked_collections.append(collection_dir)
            continue

        result.passed = False
        failure = CollectionIntegrityFailure(
            collection_dir=collection_dir, detail=detail
        )
        logger.error(
            "Bug #1506: chunks.db integrity check FAILED for %s (%s) -- "
            "refusing to publish this refresh cycle.",
            chunks_db_path,
            detail,
        )

        healthy_chunks_db_path = (
            Path(healthy_index_dir) / collection_dir.name / _CHUNKS_DB_FILENAME
            if healthy_index_dir is not None
            else None
        )
        if healthy_chunks_db_path is not None and healthy_chunks_db_path.exists():
            # Codex review Finding 3: verify the SOURCE is actually healthy
            # before trusting it -- its mere existence is not proof.
            source_ok, source_detail = _check_integrity_fresh_connection(
                healthy_chunks_db_path
            )
            if not source_ok:
                failure.source_snapshot_also_corrupt = True
                logger.error(
                    "Bug #1506 Codex Finding 3: last-known-good snapshot "
                    "%s is ALSO corrupt (%s) -- refusing to self-heal from "
                    "a corrupt source. Both the current write and the "
                    "last-known-good snapshot are corrupt; this is a more "
                    "severe condition requiring immediate operator "
                    "investigation.",
                    healthy_chunks_db_path,
                    source_detail,
                )
            else:
                failure.self_heal_attempted = True
                try:
                    restore_chunks_db_via_reflink(
                        clone_backend, healthy_chunks_db_path, chunks_db_path
                    )
                    # Codex review Finding 3: re-verify the RESTORED
                    # destination before claiming success -- a reflink
                    # clone that completes without raising can still
                    # produce a corrupt (or re-corrupted) destination.
                    post_ok, post_detail = _check_integrity_fresh_connection(
                        chunks_db_path
                    )
                    if post_ok:
                        failure.self_heal_succeeded = True
                        logger.error(
                            "Bug #1506: self-heal SUCCEEDED for %s -- "
                            "restored from last-known-good snapshot %s and "
                            "re-verified; master_path is now clean.",
                            chunks_db_path,
                            healthy_chunks_db_path,
                        )
                    else:
                        failure.self_heal_error = (
                            f"post-restore integrity re-check failed: {post_detail}"
                        )
                        logger.error(
                            "Bug #1506 Codex Finding 3: self-heal restore "
                            "for %s completed but the RESTORED file FAILED "
                            "post-restore integrity re-verification (%s) "
                            "-- treating as a self-heal FAILURE, not a "
                            "success. master_path remains unhealthy; "
                            "operator intervention may be required.",
                            chunks_db_path,
                            post_detail,
                        )
                except Exception as exc:
                    failure.self_heal_error = f"{type(exc).__name__}: {exc}"
                    logger.error(
                        "Bug #1506: self-heal FAILED for %s from %s (%s) -- "
                        "master_path remains corrupt; operator intervention "
                        "may be required.",
                        chunks_db_path,
                        healthy_chunks_db_path,
                        exc,
                    )
        else:
            logger.error(
                "Bug #1506: no last-known-good snapshot available to "
                "self-heal %s from (healthy_index_dir=%s) -- master_path "
                "remains corrupt until a manual restore or a future "
                "successful refresh overwrites it.",
                chunks_db_path,
                healthy_index_dir,
            )

        result.failures.append(failure)

    return result
