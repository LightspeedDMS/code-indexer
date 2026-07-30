"""Standalone ``cidx index --migrate-chunks-to-sqlite`` orchestration
(Story #1488 AC3-AC11, Epic #1454 "Fleet Migration").

The CLI command in ``cli.py`` stays thin: it validates option exclusivity
(AC11) and dispatches to :func:`run_chunk_migration` here, which owns the
whole migration lifecycle:

  * AC7  -- acquire a repo-scoped EXCLUSIVE, non-blocking index-mutation
    lock (``.code-indexer/.index-mutation.lock`` via fcntl flock) held for
    the ENTIRE migration. Codex Finding 1a: the SAME lock is acquired by the
    standalone foreground ``cidx index`` mutation path (``cli.py``), so the
    two are MUTUALLY EXCLUSIVE. Fail CLOSED if a live writer (the cidx daemon
    / ``cidx watch``) is detected -- OR if daemon liveness cannot be
    DETERMINED (Codex Finding 6) -- via an AUTHORITATIVE socket-connect probe
    (never a bare ``socket_path.exists()``).
  * AC5  -- build a TYPED inventory of what to migrate: semantic/multimodal
    collections (dirs with ``collection_meta.json``) and temporal shards
    (discovered via the temporal subsystem's own ``is_temporal_collection``
    parser). Codex Finding 7: the bare ``code-indexer-temporal`` directory is
    EXCLUDED UNCONDITIONALLY by EXACT NAME (it anchors the shared temporal
    bookkeeping store, Bug #1405) -- if it unexpectedly holds vector-looking
    data it is surfaced as an operator-visible ANOMALY, never migrated. Any
    unrecognized legacy-looking dir is REPORTED, never deleted. Codex Finding
    8: an unrecognized/anomaly directory forces a NON-ZERO exit.
  * AC3/AC4 -- consolidate each collection IN PLACE via the shared engine
    :func:`consolidate_collection_in_place`, inheriting its
    durable-before-delete safety contract (fresh-connection integrity gate
    before any legacy file is deleted). The SAME engine is reused verbatim
    for temporal shards: an in-place layout consolidation of the existing
    sharded ``vector_*.json`` into ``chunks.db`` is exactly what it does --
    the shard's ``hnsw_index.bin`` (bridged by ``collection_meta.json``'s
    ``id_mapping``) and every temporal bookkeeping file
    (``temporal_metadata.db`` / ``temporal_progress.json`` /
    ``temporal_structure.json`` / ``projection_matrix.npy`` /
    ``path_index.bin``) are left UNTOUCHED. NO sister root, NO alias
    publish, NO versioned snapshot (those belong exclusively to the SERVER
    relocation path).
  * AC6  -- idempotent + crash-resume: an already-fully-migrated collection
    is a safe no-op (``resolve_chunk_layout`` already ``CHUNKS_DB`` and
    :func:`verify_collection_fully_migrated`); an interrupted-then-rerun
    resumes safely because the engine is discriminator-driven crash-safe.
  * AC8/AC9/AC10 -- fail LOUD: classify each collection
    migrated/already-migrated/failed/skipped/cleanup-pending, print a final
    per-collection status table, and exit NON-ZERO if any collection
    failed, was skipped, or is cleanup-pending. Insufficient disk (AC10) is
    surfaced from the engine's own ``statvfs`` preflight as ``skipped``.
  * AC11 -- empty handling: a missing ``index/`` dir is an actionable error
    (non-zero); an index with only ``CHUNKS_DB`` collections (or genuinely
    empty) is a successful no-op (exit 0).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import socket
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, List

from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
    is_temporal_collection,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationCleanupError,
    ConsolidationVerificationError,
    UnrecoverableConsolidationCorruptionError,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)
from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

logger = logging.getLogger(__name__)

#: Repo-scoped EXCLUSIVE index-mutation lock (Codex Finding 1a). The SAME
#: file is acquired by BOTH the standalone foreground ``cidx index`` mutation
#: path (in ``cli.py``) and this migration, so the two are mutually exclusive
#: -- a foreground index can never write a NEW legacy point while a migration
#: is scanning+consolidating+cleaning up (which would otherwise let cleanup
#: delete an unverified, just-written point). Renamed from ``.migration.lock``
#: to reflect that it now guards ALL index mutation, not just migration.
INDEX_MUTATION_LOCK_FILENAME = ".index-mutation.lock"

#: Authoritative daemon-liveness connect-probe timeout (seconds). Kept short
#: -- a live daemon accepts instantly on a local Unix socket.
_DAEMON_PROBE_TIMEOUT_SECONDS = 2.0

#: Codex Finding F: the CONTAINER directory holding LEGACY multimodal
#: collections. It sits DIRECTLY under ``.code-indexer/index/`` (the migration
#: root) and is created by ``FilesystemVectorStore(..., subdirectory=
#: "multimodal_index")`` whose ``base_path`` is ``.code-indexer/index`` -- so
#: real multimodal collections live one level DEEPER, at
#: ``.code-indexer/index/multimodal_index/<collection>/`` (see
#: ``services/multi_index_query_service.py`` and
#: ``FilesystemVectorStore._get_collection_path``). The container itself has NO
#: direct ``collection_meta.json`` and is NEVER a migratable collection; its
#: child directories are ordinary (multimodal) semantic collections.
MULTIMODAL_CONTAINER_DIRNAME = "multimodal_index"


#: The default value of `cidx index`'s ``--batch-size`` option. A migration
#: run performs no embedding/indexing pass, so a non-default batch size is a
#: meaningless (and therefore rejected) combination.
_DEFAULT_INDEX_BATCH_SIZE = 50


class MigrationLockError(Exception):
    """Raised when the repo-scoped exclusive index-mutation lock could not
    be acquired, OR a live writer (the cidx daemon / ``cidx watch``) was
    detected -- fail CLOSED before touching any on-disk data (AC7)."""


def validate_migrate_flag_exclusivity(
    *,
    clear: bool,
    reconcile: bool,
    reconcile_embedder: tuple,
    detect_deletions: bool,
    rebuild_indexes: bool,
    rebuild_index: bool,
    fts: bool,
    rebuild_fts_index: bool,
    index_commits: bool,
    all_branches: bool,
    max_commits: Any,
    since_date: Any,
    diff_context: Any,
    new_collection_layout: Any,
    files_count_to_process: Any,
    progress_json: bool,
    batch_size: int,
) -> None:
    """AC11: reject ``--migrate-chunks-to-sqlite`` combined with ANY flag
    that implies a real indexing pass.

    A migration run consolidates existing on-disk collections and then EXITS
    -- it performs no discovery/embedding/index pass -- so every index-pass
    option is meaningless alongside it and MUST be rejected loudly (never
    silently ignored), naming every offending flag.

    Raises:
        click.UsageError: one or more conflicting flags were set.
    """
    import click

    offenders = []
    if clear:
        offenders.append("--clear")
    if reconcile:
        offenders.append("--reconcile")
    if reconcile_embedder:
        offenders.append("--reconcile-embedder")
    if detect_deletions:
        offenders.append("--detect-deletions")
    if rebuild_indexes:
        offenders.append("--rebuild-indexes")
    if rebuild_index:
        offenders.append("--rebuild-index")
    if fts:
        offenders.append("--fts")
    if rebuild_fts_index:
        offenders.append("--rebuild-fts-index")
    if index_commits:
        offenders.append("--index-commits")
    if all_branches:
        offenders.append("--all-branches")
    if max_commits is not None:
        offenders.append("--max-commits")
    if since_date is not None:
        offenders.append("--since-date")
    if diff_context is not None:
        offenders.append("--diff-context")
    if new_collection_layout is not None:
        offenders.append("--new-collection-layout")
    if files_count_to_process is not None:
        offenders.append("--files-count-to-process")
    if progress_json:
        offenders.append("--progress-json")
    if batch_size != _DEFAULT_INDEX_BATCH_SIZE:
        offenders.append("--batch-size")

    if offenders:
        raise click.UsageError(
            "--migrate-chunks-to-sqlite runs a one-shot storage migration and "
            "then exits; it cannot be combined with indexing-pass options. "
            "Remove: " + ", ".join(offenders) + "."
        )


class MigrationTargetKind(str, Enum):
    SEMANTIC = "semantic"
    TEMPORAL = "temporal"


class MigrationStatus(str, Enum):
    MIGRATED = "migrated"
    ALREADY_MIGRATED = "already-migrated"
    FAILED = "failed"
    SKIPPED = "skipped"
    CLEANUP_PENDING = "cleanup-pending"


@dataclass
class CollectionOutcome:
    """Per-collection migration result for the final status table."""

    name: str
    kind: MigrationTargetKind
    status: MigrationStatus
    detail: str = ""


@dataclass
class MigrationInventory:
    """Typed inventory of what to migrate (AC5). ``unrecognized`` and
    ``anomalies`` entries are REPORTED only -- never migrated, never deleted
    -- and their presence forces a NON-ZERO exit (Codex Finding 8).

    ``anomalies`` (Codex Finding 7) is specifically the bare
    ``code-indexer-temporal`` directory when it unexpectedly holds
    vector-looking data: the bare directory is NEVER a migratable shard (it
    anchors the shared temporal bookkeeping store, Bug #1405), so it is
    excluded by EXACT NAME -- but data inside it is an operator-visible
    anomaly worth surfacing rather than silently ignoring."""

    semantic: List[Path] = field(default_factory=list)
    temporal: List[Path] = field(default_factory=list)
    unrecognized: List[Path] = field(default_factory=list)
    anomalies: List[Path] = field(default_factory=list)


def _looks_like_legacy_data(entry: Path) -> bool:
    """True iff ``entry`` contains legacy-looking chunk artifacts (a
    ``vector_*.json`` file or a retired ``id_index.bin``) despite lacking a
    ``collection_meta.json`` and not being a temporal collection -- i.e. an
    UNRECOGNIZED legacy directory worth reporting."""
    if (entry / "id_index.bin").exists():
        return True
    return next(entry.rglob("vector_*.json"), None) is not None


def _bare_temporal_has_vector_data(entry: Path) -> bool:
    """Codex Finding 7: True iff the bare ``code-indexer-temporal`` directory
    holds vector-looking artifacts (an ``hnsw_index.bin``, a ``chunks.db``,
    or any ``vector_*.json``). The bare directory is NEVER a migratable shard
    regardless -- this only decides whether to SURFACE it as an anomaly
    (data present) or silently skip it as the empty shared bookkeeping
    directory (Bug #1405)."""
    if (entry / "hnsw_index.bin").exists():
        return True
    if (entry / "chunks.db").exists():
        return True
    return next(entry.rglob("vector_*.json"), None) is not None


def _descend_multimodal_container(
    container: Path, inventory: MigrationInventory
) -> None:
    """Codex Finding F: inventory the CHILDREN of the ``multimodal_index``
    container as (multimodal) semantic collections.

    Real legacy multimodal collections live one level deeper than an ordinary
    semantic collection -- ``.code-indexer/index/multimodal_index/<collection>/``
    -- each with its OWN ``collection_meta.json``. The container itself is
    NEVER a migratable collection and is NEVER reported as unrecognized (even
    though it holds nested ``vector_*.json`` data, so the top-level
    ``_looks_like_legacy_data`` rglob would otherwise mis-flag it). Each child
    is classified exactly like a top-level entry: a ``collection_meta.json``
    marks a migratable semantic collection; a legacy-looking child WITHOUT a
    meta is REPORTED (never deleted), matching the top-level contract. An empty
    container contributes nothing (silently skipped).
    """
    for child in sorted(container.iterdir()):
        if not child.is_dir():
            continue
        if (child / "collection_meta.json").exists():
            inventory.semantic.append(child)
            continue
        if _looks_like_legacy_data(child):
            inventory.unrecognized.append(child)


def enumerate_migration_targets(index_dir: Path) -> MigrationInventory:
    """Build the AC5 typed inventory from ``index_dir`` (``.code-indexer/index``).

    Temporal shards are discovered via the temporal subsystem's own
    ``is_temporal_collection`` parser. Codex Finding 7: the bare
    ``code-indexer-temporal`` directory is EXCLUDED UNCONDITIONALLY by exact
    name (``LEGACY_TEMPORAL_COLLECTION``) -- it anchors the shared temporal
    bookkeeping store and is never a shard. If it unexpectedly holds
    vector-looking data it is surfaced as an operator-visible anomaly (never
    migrated, never deleted); otherwise it is silently skipped.

    Codex Finding F: the ``multimodal_index`` CONTAINER directory (a direct
    child of ``index_dir`` with no ``collection_meta.json`` of its own) is
    DESCENDED -- its child directories are the real (multimodal) semantic
    collections to migrate. The container itself is never migrated and never
    reported as unrecognized.
    """
    inventory = MigrationInventory()
    index_dir = Path(index_dir)
    if not index_dir.is_dir():
        return inventory

    for entry in sorted(index_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if is_temporal_collection(name):
            # Codex Finding 7: the bare directory is NEVER a shard. Exclude
            # it by EXACT NAME (not the data-presence discriminator, which
            # would misclassify a bare dir WITH data as migratable).
            if name == LEGACY_TEMPORAL_COLLECTION:
                if _bare_temporal_has_vector_data(entry):
                    inventory.anomalies.append(entry)
                continue
            inventory.temporal.append(entry)
            continue
        if name == MULTIMODAL_CONTAINER_DIRNAME:
            # Codex Finding F: descend the container; its CHILDREN are the
            # migratable collections. The container is never a collection and
            # never an unrecognized dir.
            _descend_multimodal_container(entry, inventory)
            continue
        if (entry / "collection_meta.json").exists():
            inventory.semantic.append(entry)
            continue
        if _looks_like_legacy_data(entry):
            inventory.unrecognized.append(entry)
    return inventory


#: The ONLY connect-probe errno values that DEFINITIVELY prove a daemon is
#: not live: ENOENT (no socket file at all) and ECONNREFUSED (a socket path
#: that exists but nothing is listening -- a stale socket file, or a regular
#: file). Any OTHER OSError (EACCES from a mode-000 socket, a connect
#: timeout, EIO, ...) is INDETERMINATE and must fail CLOSED (Codex Finding 6).
_DEFINITIVE_NOT_LIVE_ERRNOS = frozenset({errno.ENOENT, errno.ECONNREFUSED})


def _daemon_is_live(
    socket_path: Path, timeout: float = _DAEMON_PROBE_TIMEOUT_SECONDS
) -> bool:
    """Authoritative daemon-liveness probe: attempt an AF_UNIX connect.

    Returns ``True`` if the connection is accepted (a live daemon). Returns
    ``False`` ONLY on a DEFINITIVE not-live signal (ENOENT/ECONNREFUSED --
    absent path, stale socket, or regular file). Re-RAISES any other
    ``OSError`` (Codex Finding 6): an EACCES (mode-000 live socket), a
    connect timeout, or any other error is INDETERMINATE and must NOT be
    silently classified as not-live -- a bare ``socket_path.exists()`` or a
    swallow-everything ``except OSError: return False`` both false-positive
    "not live" on an indeterminate state and would let a migration proceed
    while a writer may still be active.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError as exc:
        if exc.errno in _DEFINITIVE_NOT_LIVE_ERRNOS:
            return False
        raise  # INDETERMINATE -> caller fails CLOSED
    finally:
        with contextlib.suppress(Exception):
            sock.close()


def check_no_live_daemon(config_manager: Any) -> None:
    """Raise :class:`MigrationLockError` if a live cidx daemon (or
    ``cidx watch``, which runs inside the daemon) is serving this repository,
    OR if the daemon's liveness cannot be DETERMINED (Codex Finding 6 --
    fail CLOSED rather than proceed on an indeterminate probe).

    Uses the authoritative connect-probe against the daemon's Unix socket.
    """
    socket_path = Path(config_manager.get_socket_path())
    try:
        live = _daemon_is_live(socket_path)
    except OSError as exc:
        raise MigrationLockError(
            f"Cannot determine daemon liveness for this repository (socket "
            f"{socket_path}): {exc}. Chunk migration mutates on-disk index "
            f"data and MUST NOT run while a writer may be active -- refusing "
            f"to migrate on an INDETERMINATE liveness result. Resolve the "
            f"underlying error (e.g. socket permissions) and retry."
        ) from exc
    if live:
        raise MigrationLockError(
            "A live cidx daemon (or `cidx watch`) is running for this "
            f"repository (socket {socket_path}). Chunk migration mutates "
            "on-disk index data and MUST NOT run while another writer is "
            "active. Stop it first: run `cidx stop` (and `cidx watch-stop` "
            "if a watch is active), then retry the migration."
        )


@contextlib.contextmanager
def acquire_index_mutation_lock(config_dir: Path):
    """Acquire the repo-scoped EXCLUSIVE, NON-BLOCKING index-mutation lock
    (``.index-mutation.lock`` under ``config_dir``), holding it for the whole
    ``with`` body and releasing it (and closing the fd) in ``finally`` on
    every path.

    Codex Finding 1a: this SAME lock is acquired by the standalone foreground
    ``cidx index`` mutation path (``cli.py``), so a foreground index and a
    migration are mutually exclusive.

    Raises :class:`MigrationLockError` immediately (never blocks) if another
    cidx index/migration already holds it (AC7 fail-closed).
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config_dir / INDEX_MUTATION_LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    used_lockf = False
    acquired = False
    try:
        try:
            used_lockf = nfs_safe_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise MigrationLockError(
                "Another cidx index or migration is already running for this "
                f"repository (lock {lock_path} is held). Wait for it to "
                "finish, then retry."
            ) from exc
        yield
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                nfs_safe_funlock(fd, used_lockf)
        with contextlib.suppress(Exception):
            os.close(fd)


def _is_already_fully_migrated(path: Path) -> bool:
    """AC6 idempotency: True iff ``path`` already resolves to ``CHUNKS_DB``
    AND passes the engine's read-only completeness oracle (discriminator set,
    zero legacy files left, chunks.db opens cleanly). Never raises."""
    try:
        if resolve_chunk_layout(path) != ChunkLayout.CHUNKS_DB:
            return False
        return bool(verify_collection_fully_migrated(path))
    except Exception:  # pragma: no cover - oracle is documented never-raising
        return False


def _migrate_one(
    path: Path, kind: MigrationTargetKind, *, console: Any
) -> CollectionOutcome:
    """Consolidate ONE collection in place, classifying the outcome.

    Ordinary, independent per-collection failures (verification/durability)
    are recorded and processing continues to the next collection -- doing so
    cannot worsen repairability (the engine never deletes legacy data before
    a durable, fresh-connection proof, so a failed collection is left fully
    authoritative in its legacy sharded state, safe to retry). A terminal
    :class:`UnrecoverableConsolidationCorruptionError` is surfaced LOUDLY.
    """
    name = path.name

    if _is_already_fully_migrated(path):
        return CollectionOutcome(name, kind, MigrationStatus.ALREADY_MIGRATED)

    try:
        result = consolidate_collection_in_place(path, deletion_authorized=True)
    except UnrecoverableConsolidationCorruptionError as exc:
        console.print(
            f"❌ {name}: UNRECOVERABLE chunks.db corruption -- {exc}",
            style="red",
        )
        return CollectionOutcome(
            name, kind, MigrationStatus.FAILED, f"unrecoverable corruption: {exc}"
        )
    except ConsolidationCleanupError as exc:
        console.print(
            f"⚠️  {name}: consolidated durably but legacy cleanup incomplete -- {exc}",
            style="yellow",
        )
        return CollectionOutcome(name, kind, MigrationStatus.CLEANUP_PENDING, str(exc))
    except ConsolidationVerificationError as exc:
        # ConsolidationDurabilityError is a subclass -- caught here too.
        console.print(f"❌ {name}: migration verification failed -- {exc}", style="red")
        return CollectionOutcome(name, kind, MigrationStatus.FAILED, str(exc))
    except Exception as exc:
        # Codex Finding 4(b): a RAW/untyped error (anything the engine's
        # typed envelope did not convert -- a genuine bug, or a collaborator
        # failure) must NOT propagate and abort the whole command before the
        # final status table. Record THIS collection as FAILED and continue;
        # the engine never deletes legacy data before a durable proof, so a
        # failed collection is left fully authoritative, safe to retry.
        console.print(
            f"❌ {name}: unexpected error during migration -- "
            f"{type(exc).__name__}: {exc}",
            style="red",
        )
        return CollectionOutcome(
            name,
            kind,
            MigrationStatus.FAILED,
            f"unexpected {type(exc).__name__}: {exc}",
        )

    if result.status == "skipped_insufficient_disk":
        console.print(
            f"⚠️  {name}: SKIPPED -- {result.detail}",
            style="yellow",
        )
        return CollectionOutcome(name, kind, MigrationStatus.SKIPPED, result.detail)

    if result.status == "already_consolidated" and result.old_files_deleted == 0:
        return CollectionOutcome(name, kind, MigrationStatus.ALREADY_MIGRATED)

    return CollectionOutcome(name, kind, MigrationStatus.MIGRATED)


def _print_status_table(
    outcomes: List[CollectionOutcome],
    inventory: MigrationInventory,
    *,
    console: Any,
) -> None:
    """Print the final per-collection status table (AC8) plus any
    unrecognized-directory warnings (AC5)."""
    from rich.table import Table

    table = Table(title="Chunk Migration Results")
    table.add_column("Collection", overflow="fold")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    status_style = {
        MigrationStatus.MIGRATED: "green",
        MigrationStatus.ALREADY_MIGRATED: "cyan",
        MigrationStatus.FAILED: "red",
        MigrationStatus.SKIPPED: "yellow",
        MigrationStatus.CLEANUP_PENDING: "yellow",
    }
    for o in outcomes:
        table.add_row(
            o.name,
            o.kind.value,
            f"[{status_style[o.status]}]{o.status.value}[/{status_style[o.status]}]",
            o.detail,
        )
    console.print(table)

    if inventory.unrecognized:
        console.print(
            "⚠️  Unrecognized legacy-looking directories were found and left "
            "UNTOUCHED (reported, never deleted):",
            style="yellow",
        )
        for entry in inventory.unrecognized:
            console.print(f"   • {entry}", style="yellow")

    if inventory.anomalies:
        console.print(
            "⚠️  ANOMALY: the bare 'code-indexer-temporal' directory holds "
            "vector-looking data. It anchors the shared temporal bookkeeping "
            "store and is NEVER a migratable shard -- left UNTOUCHED (never "
            "migrated, never deleted). Investigate manually:",
            style="yellow",
        )
        for entry in inventory.anomalies:
            console.print(f"   • {entry}", style="yellow")


def _exit_code(outcomes: List[CollectionOutcome], inventory: MigrationInventory) -> int:
    """Non-zero if ANY collection failed, was skipped, or is cleanup-pending
    (AC9), OR the inventory contains an unrecognized / anomaly directory
    (Codex Finding 8) -- old-format or anomalous data remaining on disk must
    never be reported as a clean success, even when it is left untouched."""
    bad = {
        MigrationStatus.FAILED,
        MigrationStatus.SKIPPED,
        MigrationStatus.CLEANUP_PENDING,
    }
    if any(o.status in bad for o in outcomes):
        return 1
    if inventory.unrecognized or inventory.anomalies:
        return 1
    return 0


def run_chunk_migration(config: Any, config_manager: Any, *, console: Any) -> int:
    """Run the full standalone chunk-storage migration for the local repo.

    Args:
        config: loaded CIDX config (only ``config.codebase_dir`` is read).
        config_manager: exposes ``get_socket_path()`` for the daemon probe.
        console: a Rich Console for human-readable output.

    Returns:
        Process exit code: 0 on full success (including a no-op), 1 if any
        collection failed/was skipped/is cleanup-pending, or the index dir
        is missing.

    Raises:
        MigrationLockError: a live daemon/watch was detected, or the
            exclusive migration lock could not be acquired.
    """
    codebase_dir = Path(config.codebase_dir)
    config_dir = codebase_dir / ".code-indexer"
    index_dir = config_dir / "index"

    # AC11: a missing index/ dir is an actionable error, not a silent no-op.
    if not index_dir.is_dir():
        console.print(
            f"❌ No index directory found at {index_dir}. There is nothing "
            "to migrate. Run `cidx index` first to create an index.",
            style="red",
        )
        return 1

    # AC7: acquire the exclusive index-mutation lock FIRST (mutually
    # exclusive with a foreground `cidx index` -- Codex Finding 1a), then
    # probe daemon liveness UNDER the lock (so a concurrent migration cannot
    # race the probe), and hold the lock for the ENTIRE migration.
    with acquire_index_mutation_lock(config_dir):
        check_no_live_daemon(config_manager)

        inventory = enumerate_migration_targets(index_dir)
        targets = [(p, MigrationTargetKind.SEMANTIC) for p in inventory.semantic] + [
            (p, MigrationTargetKind.TEMPORAL) for p in inventory.temporal
        ]

        # Codex Finding 8: only a genuinely clean index (no targets AND no
        # reportable unrecognized/anomaly dirs) is a success no-op. If
        # anything reportable remains, fall through to print + non-zero exit.
        if not targets and not inventory.unrecognized and not inventory.anomalies:
            console.print(
                "✅ Nothing to migrate -- no legacy sharded collections found "
                f"under {index_dir}.",
                style="green",
            )
            return 0

        if targets:
            console.print(
                f"🔧 Migrating {len(targets)} collection(s) to consolidated "
                "chunks.db storage (in place)...",
                style="blue",
            )

        outcomes: List[CollectionOutcome] = []
        for path, kind in targets:
            outcomes.append(_migrate_one(path, kind, console=console))

        _print_status_table(outcomes, inventory, console=console)
        return _exit_code(outcomes, inventory)
