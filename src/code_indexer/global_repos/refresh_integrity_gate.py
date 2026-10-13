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
serving the last verified-good snapshot) and attempt a self-heal of
``master_path``'s corrupt ``chunks.db`` from the corresponding collection in
the currently-aliased ("last known good") snapshot, by an atomic single-file
copy made by this process (:func:`restore_chunks_db_file`). Bug #2157: never
the clone backend (the CoW daemon clones directories only) and never a
subprocess ``cp`` (this project's AC15 anti-orphan lint gate bans that).

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
before ``self_heal_succeeded`` is ever set True -- a restore copy that
completes without raising but produces a corrupt (or re-corrupted)
destination is reported as a self-heal FAILURE, not a success.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable, List, Optional, Tuple

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    _check_integrity_fresh_connection,
    run_integrity_check_fresh_connection,
)
from code_indexer.storage.sqlite_chunk_store import (
    ChunkStore,
    sqlite_error_reports_corruption,
)
from code_indexer.utils.file_locking import fsync_directory

logger = logging.getLogger(__name__)

_CHUNKS_DB_FILENAME = "chunks.db"
# Bug #2157: a restore streams into ".<name>.restore-tmp" beside the target;
# the leading dot keeps it out of the "metadata*.json" glob.
_RESTORE_TEMP_SUFFIX = ".restore-tmp"
_RESTORE_COPY_BUFFER_BYTES = 8 * 1024 * 1024
# SQLite's per-database sidecar files: a restored chunks.db must never be
# opened beside the discarded database's journal or WAL.
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


@dataclass
class CollectionIntegrityFailure:
    """One CHUNKS_DB collection's integrity-gate failure, plus the outcome
    of the self-heal attempt (if any): an in-process atomic single-file copy
    from the published snapshot (Bug #2157)."""

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
    # Bug #1509: metadata-{provider}.json file(s) restored alongside a
    # successful chunks.db self-heal, so master_path's metadata never
    # claims a NEW commit that chunks.db does not actually contain.
    metadata_restored_files: List[Path] = field(default_factory=list)
    metadata_restore_error: Optional[str] = None
    # Bug #2022: the integrity check itself could not run or complete (I/O
    # error, lock, busy, missing file). The store is NOT known to be damaged,
    # so no restore is ever attempted over it.
    check_inconclusive: bool = False
    # Story #2126: the established collection's chunks.db is missing (only
    # reported when the gate runs with restore_missing_stores=True). Neither
    # corrupt nor inconclusive: nothing is there to destroy, so it is
    # restored from the published snapshot exactly like a corrupt store.
    store_missing: bool = False


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


class IntegrityVerdict(str, Enum):
    """Bug #2022: outcome of one collection's integrity check. Only CORRUPT
    -- the check completed (or SQLite itself raised a corruption report) and
    found damage -- may ever lead to a restore. INCONCLUSIVE means the check
    could not run or complete (I/O error, lock, busy, missing file)."""

    HEALTHY = "healthy"
    CORRUPT = "corrupt"
    INCONCLUSIVE = "inconclusive"


def _verdict_for_raised(exc: BaseException, step: str) -> Tuple[IntegrityVerdict, str]:
    detail = f"{step} raised {type(exc).__name__}: {exc}"
    if sqlite_error_reports_corruption(exc):
        return IntegrityVerdict.CORRUPT, detail
    logger.error("Bug #2022: integrity check inconclusive -- %s", detail)
    return IntegrityVerdict.INCONCLUSIVE, detail


def check_chunks_db_integrity(chunks_db_path: Path) -> Tuple[IntegrityVerdict, str]:
    """Force *chunks_db_path* durable on the actual backing store, then run
    ``PRAGMA integrity_check`` on a genuinely fresh, read-only connection.
    Never raises: a flush or check that fails is a verdict, not an error."""
    chunks_db_path = Path(chunks_db_path)
    if not chunks_db_path.exists():
        return IntegrityVerdict.INCONCLUSIVE, "chunks.db does not exist"
    try:
        with ChunkStore(chunks_db_path, durable_synchronous=True) as store:
            store.flush_durable()
    except Exception as exc:
        return _verdict_for_raised(exc, "flush_durable")
    try:
        rows = run_integrity_check_fresh_connection(chunks_db_path)
    except (sqlite3.Error, OSError) as exc:
        return _verdict_for_raised(exc, "integrity_check")
    if rows == ["ok"]:
        return IntegrityVerdict.HEALTHY, "ok"
    return IntegrityVerdict.CORRUPT, "; ".join(rows)


def flush_and_check_chunks_db_integrity(chunks_db_path: Path) -> Tuple[bool, str]:
    """``(True, "ok")`` when durable and healthy; ``(False, detail)``
    otherwise (see :func:`check_chunks_db_integrity`)."""
    verdict, detail = check_chunks_db_integrity(chunks_db_path)
    return verdict is IntegrityVerdict.HEALTHY, detail


def _remove_temp_sibling(temp_path: Path) -> None:
    try:
        temp_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error(
            "Bug #2157: could not remove the temporary restore file %s: %s",
            temp_path,
            exc,
        )


class RestoreNotDurableError(Exception):
    """The restore REPLACED its target(s) -- they now hold the snapshot's
    content -- but making the replacement durable (the directory fsync)
    failed. Distinct from every other restore error, after which the target
    is untouched: the caller must still verify the replaced store and restore
    its metadata (never leave the snapshot's store beside newer metadata),
    and must still fail the cycle."""

    replaced = True

    def __init__(self, replaced_paths: List[Path], cause: BaseException) -> None:
        self.replaced_paths = list(replaced_paths)
        self.cause = cause
        super().__init__(
            f"replaced {', '.join(str(p) for p in self.replaced_paths)} but the "
            f"replacement is not durable: {type(cause).__name__}: {cause}"
        )


def _remove_stale_sidecars(target_path: Path, suffixes: Tuple[str, ...]) -> None:
    """Remove each ``<target><suffix>`` file beside *target_path*. They belong
    to the database being discarded: a hot rollback journal left there would
    be played back INTO the restored file on its next open. An absent sidecar
    is skipped; anything there that is not a regular file raises."""
    for suffix in suffixes:
        sidecar = target_path.with_name(target_path.name + suffix)
        try:
            mode = sidecar.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise OSError(
                errno.EEXIST,
                "refusing to restore over a database whose SQLite sidecar "
                "is not a regular file",
                str(sidecar),
            )
        sidecar.unlink()


def _open_restore_source(source_path: Path) -> Tuple[BinaryIO, int]:
    """Open a restore source for reading only when it is a regular file,
    never through a symbolic link at its final component.

    Returns the open file and the permission bits of that same open file
    (one ``fstat``), so the bits applied to the copy describe exactly the
    bytes read. ``O_NONBLOCK`` makes the open return at once for any
    non-regular file, which the ``fstat`` check then refuses; it has no
    effect on reads of a regular file. Raises ``OSError`` naming the path
    with "not a regular file" for a symbolic link, a directory or any other
    non-regular file.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source_path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(
                errno.ELOOP,
                "restore source is not a regular file",
                str(source_path),
            ) from exc
        raise
    try:
        source_stat = os.fstat(fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(
                errno.EINVAL,
                "restore source is not a regular file",
                str(source_path),
            )
        source = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return source, stat.S_IMODE(source_stat.st_mode)


def _restore_file_atomically(
    source_path: Path,
    target_path: Path,
    *,
    stale_sidecar_suffixes: Tuple[str, ...] = (),
) -> None:
    """Bug #2157: restore a single file atomically, created by this process
    (so the service user owns it), identically on every deployment mode.

    *source_path* is read only when it is a regular file, never through a
    symbolic link at its final component (:func:`_open_restore_source`).
    Streams it into the temporary sibling ``.<target name>.restore-tmp`` in
    *target_path*'s directory, applies the permission bits of the opened
    source, fsyncs it, removes the target's stale
    *stale_sidecar_suffixes* files (never copying the source's), then
    ``os.replace``s it onto *target_path* and fsyncs the directory. Never the
    clone backend: the CoW daemon clones directories only. On failure before
    the replace only the temporary sibling is removed -- *target_path* keeps
    its previous content (or stays absent) -- and the error propagates.

    The temp name is fixed per target, so a process killed mid-restore
    leaves at most one temp file per target, and the next restore (one at a
    time per target: restores run under the alias's write lock) removes it
    before copying.
    """
    temp_path = target_path.with_name(f".{target_path.name}{_RESTORE_TEMP_SUFFIX}")
    # A leftover of exactly this name is a stale temp from an interrupted
    # restore; a failure to remove it propagates with the target untouched.
    temp_path.unlink(missing_ok=True)
    temp_file = open(temp_path, "xb")
    try:
        with temp_file:
            source, source_mode = _open_restore_source(source_path)
            with source:
                shutil.copyfileobj(source, temp_file, _RESTORE_COPY_BUFFER_BYTES)
            os.fchmod(temp_file.fileno(), source_mode)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        _remove_stale_sidecars(target_path, stale_sidecar_suffixes)
        os.replace(temp_path, target_path)
    except BaseException:
        _remove_temp_sibling(temp_path)
        raise
    try:
        fsync_directory(target_path.parent)
    except OSError as exc:
        logger.error(
            "Bug #2157: restored %s but the replacement is not durable: the "
            "directory fsync failed (%s: %s)",
            target_path,
            type(exc).__name__,
            exc,
        )
        raise RestoreNotDurableError([target_path], exc) from exc


def restore_chunks_db_file(
    healthy_chunks_db_path: Path,
    corrupt_chunks_db_path: Path,
) -> None:
    """Restore *corrupt_chunks_db_path* (corrupt or missing) from
    *healthy_chunks_db_path* atomically (:func:`_restore_file_atomically`).
    The discarded database's journal/WAL files beside the target are removed
    first; the snapshot's are never copied.

    Raises on any failure -- callers must not swallow this, since a failed
    restore leaves ``master_path`` corrupt and must be logged loudly by the
    caller.
    """
    _restore_file_atomically(
        Path(healthy_chunks_db_path),
        Path(corrupt_chunks_db_path),
        stale_sidecar_suffixes=_SQLITE_SIDECAR_SUFFIXES,
    )


def restore_metadata_files(
    healthy_metadata_dir: Path,
    target_metadata_dir: Path,
) -> List[Path]:
    """Bug #1509: restore every ``metadata*.json`` file (e.g.
    ``metadata-voyage-ai.json``, one per embedding provider -- see
    ``cli.py``'s ``_get_provider_metadata_path``) found directly under
    *healthy_metadata_dir* into *target_metadata_dir*, each atomically like
    :func:`restore_chunks_db_file`.

    A self-healed ``chunks.db`` (restored from the last-known-good
    snapshot) must never be left paired with a sibling metadata file that
    still claims the NEW commit was fully indexed -- ``_index_source()``
    already wrote that stale claim to ``master_path``'s ``.code-indexer/``
    dir earlier in the SAME refresh cycle, before the integrity gate ever
    runs. Restoring metadata from the same healthy snapshot keeps
    ``master_path`` self-consistent: metadata now accurately reflects the
    OLD, restored commit that ``chunks.db`` actually contains.

    Returns the list of destination paths actually restored (possibly
    empty, if the healthy snapshot has no metadata files of its own --
    e.g. its very first-ever refresh). Raises on the first failed file --
    callers must not swallow this. A file replaced but not made durable is
    still restored: the rest are replaced too, then RestoreNotDurableError
    names every replaced file.
    """
    restored: List[Path] = []
    if not healthy_metadata_dir.is_dir():
        return restored

    first_cause: Optional[BaseException] = None
    for healthy_metadata_path in sorted(healthy_metadata_dir.glob("metadata*.json")):
        target_metadata_path = target_metadata_dir / healthy_metadata_path.name
        try:
            _restore_file_atomically(healthy_metadata_path, target_metadata_path)
        except RestoreNotDurableError as not_durable:
            first_cause = first_cause or not_durable.cause
        restored.append(target_metadata_path)
    if first_cause is not None:
        raise RestoreNotDurableError(restored, first_cause)
    return restored


class RestoreVetoedError(RuntimeError):
    """Bug #2022: the caller's ``before_restore`` check refused a restore
    (e.g. the refresh lost its write lock). Propagates out of the gate; the
    restore copy never starts."""


def _check_restore_allowed(before_restore: Optional[Callable[[], None]]) -> None:
    if before_restore is None:
        return
    try:
        before_restore()
    except Exception as exc:
        raise RestoreVetoedError(f"restore vetoed: {exc}") from exc


def run_refresh_integrity_gate(
    *,
    source_index_dir: Path,
    healthy_index_dir: Optional[Path],
    before_restore: Optional[Callable[[], None]] = None,
    restore_missing_stores: bool = False,
) -> RefreshIntegrityGateResult:
    """Run the Bug #1506 durability-flush + integrity gate against every
    CHUNKS_DB collection under *source_index_dir* (the just-mutated
    ``master_path``'s ``.code-indexer/index/``).

    Story #2126: with *restore_missing_stores*, an established collection
    whose ``chunks.db`` is missing is reported ``store_missing`` and restored
    from the published snapshot through the same verified path as a corrupt
    one. The missing-store self-heal and the publish gate (Bug #2150: a child
    with nothing to write exits 0 without opening the store) pass it; without
    it (the corruption self-heal), a missing store stays INCONCLUSIVE.

    Bug #2022: a restore is attempted only for a CORRUPT verdict (never for
    an INCONCLUSIVE one), and *before_restore* (e.g. a write-lock ownership
    check) is called immediately before each restore copy; anything it
    raises aborts the restore and propagates as ``RestoreVetoedError``.

    On any collection's integrity-check failure, attempt an atomic
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

    Bug #1509: immediately after a successful chunks.db self-heal, the
    sibling ``metadata-{provider}.json`` file(s) are ALSO restored from
    the same healthy snapshot (``healthy_index_dir.parent`` ->
    ``source_index_dir.parent`` -- the ``.code-indexer/`` dir, sibling of
    ``.code-indexer/index/``), so ``master_path`` never claims a NEW
    commit that chunks.db does not actually contain. This is best-effort:
    a metadata-restore failure is logged loudly and recorded on
    ``metadata_restore_error`` but does not flip ``self_heal_succeeded``
    back to False, since the chunks.db repair itself already succeeded.

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
        # Story #2126: discovered as CHUNKS_DB (its layout discriminator is
        # committed) yet without a store -- lost, never pending (#2119).
        store_missing = restore_missing_stores and not chunks_db_path.exists()
        if store_missing:
            detail = "chunks.db of an established collection is missing"
        else:
            verdict, detail = check_chunks_db_integrity(chunks_db_path)
            if verdict is IntegrityVerdict.HEALTHY:
                result.checked_collections.append(collection_dir)
                continue

        result.passed = False
        failure = CollectionIntegrityFailure(
            collection_dir=collection_dir, detail=detail, store_missing=store_missing
        )
        if store_missing:
            logger.error(
                "Story #2126: %s is missing from an established collection -- "
                "refusing to publish; restoring it from the published snapshot.",
                chunks_db_path,
            )
        elif verdict is IntegrityVerdict.INCONCLUSIVE:
            # Bug #2022: unverifiable is not corrupt -- never restore over it.
            failure.check_inconclusive = True
            logger.error(
                "Bug #2022: integrity of %s could not be verified (%s) -- "
                "refusing to publish and NOT restoring over it.",
                chunks_db_path,
                detail,
            )
            result.failures.append(failure)
            continue
        else:
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
            # healthy_chunks_db_path is only ever non-None when
            # healthy_index_dir is also non-None (see its construction
            # above) -- assert narrows this for mypy across the
            # intermediate variable.
            assert healthy_index_dir is not None
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
                # Bug #2022: the caller re-verifies it may still write
                # (e.g. lock ownership) immediately before each copy.
                _check_restore_allowed(before_restore)
                failure.self_heal_attempted = True
                try:
                    durability_error: Optional[str] = None
                    try:
                        restore_chunks_db_file(healthy_chunks_db_path, chunks_db_path)
                    except RestoreNotDurableError as not_durable:
                        # Bug #2157: the store WAS replaced -- verify it and
                        # restore its metadata like any restore, but the
                        # self-heal is not a success.
                        durability_error = f"restore not durable: {not_durable}"
                    # Codex review Finding 3: re-verify the RESTORED
                    # destination before claiming success -- an in-process
                    # atomic single-file copy from the published snapshot
                    # (Bug #2157) that completes without raising can still
                    # leave a corrupt (or re-corrupted) destination.
                    post_ok, post_detail = _check_integrity_fresh_connection(
                        chunks_db_path
                    )
                    if post_ok and durability_error is not None:
                        failure.self_heal_error = durability_error
                        logger.error(
                            "Bug #2157: self-heal of %s restored and "
                            "re-verified the store from %s, but %s -- the "
                            "self-heal is FAILED, not a success.",
                            chunks_db_path,
                            healthy_chunks_db_path,
                            durability_error,
                        )
                    elif post_ok:
                        failure.self_heal_succeeded = True
                        logger.error(
                            "Bug #1506: self-heal SUCCEEDED for %s -- "
                            "restored from last-known-good snapshot %s and "
                            "re-verified; master_path is now clean.",
                            chunks_db_path,
                            healthy_chunks_db_path,
                        )
                    if post_ok:
                        # Bug #1509: also restore the sibling
                        # metadata-{provider}.json file(s) from the same
                        # healthy snapshot -- _index_source() already
                        # wrote a metadata file to master_path claiming
                        # the NEW commit is fully indexed, earlier in
                        # this SAME refresh cycle, before this gate ever
                        # ran. Left unrestored, that stale claim would
                        # permanently fool the next refresh cycle's
                        # git-ref-only change detection.
                        try:
                            _check_restore_allowed(before_restore)
                            failure.metadata_restored_files = restore_metadata_files(
                                Path(healthy_index_dir).parent,
                                Path(source_index_dir).parent,
                            )
                            if failure.metadata_restored_files:
                                logger.error(
                                    "Bug #1509: restored %d metadata "
                                    "file(s) for %s alongside the "
                                    "chunks.db self-heal: %s",
                                    len(failure.metadata_restored_files),
                                    collection_dir,
                                    failure.metadata_restored_files,
                                )
                        except RestoreVetoedError:
                            raise
                        except RestoreNotDurableError as metadata_not_durable:
                            failure.metadata_restored_files = (
                                metadata_not_durable.replaced_paths
                            )
                            failure.metadata_restore_error = (
                                f"restore not durable: {metadata_not_durable}"
                            )
                            logger.error(
                                "Bug #2157: metadata file(s) for %s were "
                                "restored but are not durable (%s)",
                                collection_dir,
                                metadata_not_durable,
                            )
                        except Exception as metadata_exc:
                            failure.metadata_restore_error = (
                                f"{type(metadata_exc).__name__}: {metadata_exc}"
                            )
                            logger.error(
                                "Bug #1509: failed to restore metadata "
                                "file(s) for %s alongside a successful "
                                "chunks.db self-heal (%s) -- "
                                "master_path's metadata may still claim "
                                "a newer commit than chunks.db actually "
                                "contains.",
                                collection_dir,
                                metadata_exc,
                            )
                    else:
                        failure.self_heal_error = (
                            f"post-restore integrity re-check failed: {post_detail}"
                        )
                        if durability_error is not None:
                            failure.self_heal_error += f"; {durability_error}"
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
                except RestoreVetoedError:
                    raise
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
