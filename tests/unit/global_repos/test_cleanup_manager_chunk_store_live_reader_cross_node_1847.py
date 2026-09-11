"""Bug #1847 AC1/AC2 -- ChunkStoreThreadCache is a long-lived reader of
versioned snapshots that holds a REAL, live, open sqlite3 file descriptor
per thread, but it publishes no reader lease at all today.

EVIDENCE this is the MOST exposed of the three unprotected readers (read
from source this run, ``storage/shared/chunk_store_cache.py``):

- Unlike HNSWIndexCache/FTSIndexCache/IdIndexCache, ``ChunkStoreThreadCache``
  has NO TTL-based eviction at all. An entry is dropped only when: (a) the
  file's mtime changes AT THE SAME PATH (never happens for an immutable,
  published ``.versioned/`` snapshot -- it is never rewritten in place),
  (b) an explicit ``invalidate_prefix()`` call after a real ALIAS SWAP
  (module docstring: "called by snapshot_cache_invalidation.
  invalidate_snapshot_caches() after a real alias swap/publish" -- a
  DIFFERENT event from CleanupManager physically deleting an old,
  already-superseded snapshot later), or (c) the per-thread 32-entry LRU
  cap rotating it out.
- The module's own docstring documents a REAL PRODUCTION INCIDENT (Bug
  #1775, ~1260 leaked file descriptors) caused by exactly this shape: a
  stale handle for an old snapshot path was held open indefinitely because
  the only-known invalidation trigger (alias swap) never fired for it, or
  the sweep that fixes that hadn't reached that thread yet. That proves
  the "still live at time of physical deletion" window is real, not
  theoretical.
- The cached value is a real ``sqlite3`` connection (an open file
  descriptor into the snapshot's ``chunks.db``) -- the closest analogue to
  the "SIGBUS on an mmap'd region" risk in the bug report: on the shared
  NFSv3 ``.versioned/`` mount, a connection left open on one node while
  another node unlinks the file is exactly the cross-node ESTALE scenario
  this bug class exists to close.
- ``ChunkStoreThreadCache.__init__`` accepts only ``max_entries_per_thread``
  / ``max_tracked_stale_prefixes`` today -- no ``lease_root``/
  ``is_versioned_snapshot`` parameter exists.
- ``ChunkStoreThreadCache`` has no ``get_stats()`` and (unlike the other
  three caches) has NO background cleanup thread at all -- there is
  nowhere today a lease could be periodically renewed from. This is a
  real open design question for the GREEN implementation, flagged here
  rather than solved by this RED-test turn.

This test builds a REAL ``chunks.db`` on disk via the production
``ChunkStore`` (``storage/sqlite_chunk_store.py``), opens it into a REAL
``ChunkStoreThreadCache`` through ``get_or_open()`` -- the exact production
reader lifecycle -- in one separate OS process, while a REAL
``CleanupManager`` runs its real deletion cycle against the same snapshot
path in a second, separate OS process. Two REAL OS processes
(``multiprocessing.get_context("spawn")``) because the defect IS that a
reader's liveness on one node/process is invisible to a deleter on
another -- a single-process test cannot fail for the right reason (see
test_cleanup_manager_live_reader_cross_node_1845.py and this bug's own
FTS/IdIndex sibling tests, whose shape this test follows directly).

Liveness verification note: unlike the FTS/IdIndex tests (which use a
public stats API or the public get_or_load() contract), this test reads
``cache._entries()`` -- the thread-local dict backing this cache -- from
INSIDE the SAME thread that owns it (the reader process has a single
thread). This is safe by construction (``threading.local()`` means no
other thread can ever see or race this dict), unlike reading a
cross-thread shared cache's private state, which the #1845 test's own
docstring explicitly warns against for lock-safety reasons. There is no
public alternative: this class exposes no stats/introspection method at
all.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
from typing import Any, Tuple

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


pytestmark = pytest.mark.slow

#: Bounded join/event deadline for the two-process test (Messi Rule #14 --
#: the test itself must not be able to hang forever either).
PROCESS_DEADLINE_SECONDS = 60.0

#: Bounded wait for a result already known to be sitting in the queue
#: (both worker processes have already been join()'d by this point).
QUEUE_RESULT_TIMEOUT_SECONDS = 10.0

#: Bounded wait for a leftover process to exit after terminate().
TERMINATION_JOIN_TIMEOUT_SECONDS = 10.0

CHUNKS_DB_FILENAME = "chunks.db"


def _write_real_chunk_store(db_path: Path) -> None:
    """Build and persist a genuinely valid chunks.db -- via the REAL
    production ``ChunkStore``, the same class the indexing path uses, not
    a synthetic stand-in."""
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [
                {
                    "id": "point-1",
                    "vector": [0.1, 0.2, 0.3],
                    "payload": {"path": "example.py"},
                }
            ]
        )
    finally:
        store.close()


def _open_chunk_store_reader(snapshot_path: str, mount_point: str) -> Any:
    """Construct a real, leased ``ChunkStoreThreadCache`` and open
    ``snapshot_path``'s chunks.db into it via the production
    ``get_or_open()`` pathway.

    ``lease_root``/``is_versioned_snapshot`` are the SAME injection shape
    the other three caches already accept (or, for IdIndex/FTS, were just
    given in this bug's earlier rounds). At the time this test was
    written, ``ChunkStoreThreadCache`` accepts NEITHER -- this call
    intentionally exercises the constructor shape the fix must add.
    """
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )
    from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache

    lease_root = Path(mount_point) / "cidx-meta"
    db_path = str(Path(snapshot_path) / CHUNKS_DB_FILENAME)

    cache = ChunkStoreThreadCache(
        lease_root=lease_root,
        is_versioned_snapshot=is_versioned_snapshot,
    )
    cache.get_or_open(db_path, snapshot_path)
    return cache, db_path


def _entry_still_cached(cache: Any, db_path: str) -> bool:
    """True iff (db_path, read_only=False) is present in THIS thread's
    own cached entries -- see the module docstring's liveness-
    verification note for why direct ``_entries()`` access is safe here."""
    return (db_path, False) in cache._entries()


def reader_worker(
    snapshot_path: str, mount_point: str, ready_event, release_event, result_queue
) -> None:
    """Process A: a REAL, separate OS process. Opens the real chunks.db at
    ``snapshot_path`` through the REAL, unmodified, already-existing
    production reader lifecycle -- ``ChunkStoreThreadCache.get_or_open()``
    -- and stays alive holding that open sqlite3 connection while process
    B runs its real deletion cycle against the SAME path.
    """
    try:
        cache, db_path = _open_chunk_store_reader(snapshot_path, mount_point)

        if not _entry_still_cached(cache, db_path):
            result_queue.put(
                {
                    "role": "A",
                    "ok": False,
                    "error": "cache entry missing after open",
                }
            )
            return

        ready_event.set()

        if not release_event.wait(timeout=PROCESS_DEADLINE_SECONDS):
            result_queue.put(
                {"role": "A", "ok": False, "error": "B never signaled done"}
            )
            return

        # Re-check AFTER B has finished its cleanup cycle: this proves the
        # reader was genuinely still holding the snapshot at the moment
        # deletion happened, not merely that it once opened it earlier.
        still_live = _entry_still_cached(cache, db_path)
        result_queue.put({"role": "A", "ok": True, "still_live": still_live})
    except Exception as exc:
        result_queue.put(
            {"role": "A", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )


def deleter_worker(
    db_path: str,
    mount_point: str,
    snapshot_path: str,
    ready_event,
    release_event,
    result_queue,
) -> None:
    """Process B: a REAL, separate OS process running the REAL, unmodified
    production ``CleanupManager`` deletion gate against the same snapshot
    path -- entirely independent of process A's in-memory
    ``ChunkStoreThreadCache`` (a distinct Python interpreter has no way to
    see it), which is exactly the bug.
    """
    try:
        if not ready_event.wait(timeout=PROCESS_DEADLINE_SECONDS):
            result_queue.put(
                {"role": "B", "ok": False, "error": "A never signaled ready"}
            )
            return

        backend = GoldenRepoMetadataSqliteBackend(db_path)
        manager = CleanupManager(
            query_tracker=QueryTracker(),
            job_tracker=JobTracker(db_path),
            # AC4: bypass the 900s wall-clock floor entirely so this test
            # proves the defect independent of timing.
            min_retention_age_seconds=0.0,
            persistence_backend=backend,
        )
        manager.set_snapshot_manager(
            VersionedSnapshotManager(
                versioned_base=mount_point,
                clone_backend=LocalCloneBackend(versioned_base=mount_point),
            )
        )
        # Must be the SAME root process A's ChunkStoreThreadCache is
        # injected with via lease_root=Path(mount_point) / "cidx-meta" in
        # reader_worker, or the two processes would look in different
        # directories and the liveness check would be meaningless.
        manager.set_lease_root(Path(mount_point) / "cidx-meta")
        manager.schedule_cleanup(snapshot_path)
        manager._process_cleanup_queue()

        deleted = not Path(snapshot_path).exists()
        result_queue.put({"role": "B", "ok": True, "deleted": deleted})
    except Exception as exc:
        result_queue.put(
            {"role": "B", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        # Always release A, even on failure, so the test cannot hang.
        release_event.set()


@pytest.fixture
def mount_point(tmp_path: Path) -> Path:
    mount = tmp_path / "cow-storage"
    mount.mkdir()
    return mount


@pytest.fixture
def snapshot_path(mount_point: Path) -> Path:
    snapshot = mount_point / ".versioned" / "repo_ns" / "v_1789090800"
    snapshot.mkdir(parents=True)
    _write_real_chunk_store(snapshot / CHUNKS_DB_FILENAME)
    return snapshot


def _start_two_processes(
    ctx, snapshot_path: Path, mount_point: Path, db_path: str
) -> Tuple[Any, Any, Any, Any, Any]:
    """Start reader (A) and deleter (B); return every object the caller
    must keep referenced until join (see the return-site comment)."""
    ready_event = ctx.Event()
    release_event = ctx.Event()
    result_queue = ctx.Queue()

    reader = ctx.Process(
        target=reader_worker,
        args=(
            str(snapshot_path),
            str(mount_point),
            ready_event,
            release_event,
            result_queue,
        ),
    )
    deleter = ctx.Process(
        target=deleter_worker,
        args=(
            db_path,
            str(mount_point),
            str(snapshot_path),
            ready_event,
            release_event,
            result_queue,
        ),
    )
    reader.start()
    deleter.start()
    # ready_event/release_event MUST be returned, not dropped: each is a
    # real POSIX named semaphore a spawned child rebuilds from pickled
    # state on its own schedule. If the caller holds no reference, GC can
    # unlink the semaphore file here before a slower child finishes
    # rebuilding it -- a real race, already hit and fixed once in
    # test_cleanup_manager_fts_live_reader_cross_node_1847.py.
    return reader, deleter, ready_event, release_event, result_queue


def _join_and_collect_results(reader: Any, deleter: Any, result_queue: Any) -> dict:
    """Join both processes within the bounded deadline, collect their two
    result-queue entries keyed by role, and forcibly terminate anything
    still alive (Messi Rule #14 -- this helper itself cannot hang)."""
    try:
        reader.join(timeout=PROCESS_DEADLINE_SECONDS)
        deleter.join(timeout=PROCESS_DEADLINE_SECONDS)
        assert not reader.is_alive(), "reader process (A) did not finish in time"
        assert not deleter.is_alive(), "deleter process (B) did not finish in time"
        return {
            r["role"]: r
            for r in (
                result_queue.get(timeout=QUEUE_RESULT_TIMEOUT_SECONDS) for _ in range(2)
            )
        }
    finally:
        for proc in (reader, deleter):
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=TERMINATION_JOIN_TIMEOUT_SECONDS)


class TestSnapshotNotDeletedWhileLiveChunkStoreReaderHoldsItCrossProcess:
    def test_snapshot_survives_cleanup_while_chunk_store_reader_on_another_process_holds_it(
        self, tmp_path: Path, mount_point: Path, snapshot_path: Path
    ) -> None:
        """RED on unmodified code: ChunkStoreThreadCache accepts no
        lease_root/is_versioned_snapshot at all, so this either fails
        constructing the cache (TypeError -- the missing capability
        itself) or, once that constructor shape exists, CleanupManager
        deletes the snapshot even though a real ChunkStoreThreadCache
        reader entry (an open sqlite3 connection to a genuine chunks.db)
        for the exact same path is provably still live in a separate
        process at the moment of deletion. GREEN (after the fix): the
        snapshot must survive while that live reader holds it (AC1+AC2).
        """
        db_path = str(tmp_path / "server.db")
        DatabaseSchema(db_path).initialize_database()
        GoldenRepoMetadataSqliteBackend(db_path).ensure_table_exists()

        ctx = mp.get_context("spawn")
        reader, deleter, ready_event, release_event, result_queue = (
            _start_two_processes(ctx, snapshot_path, mount_point, db_path)
        )
        results = _join_and_collect_results(reader, deleter, result_queue)

        assert results["A"]["ok"], f"reader process failed: {results['A']}"
        assert results["A"]["still_live"], (
            "precondition broken: process A's ChunkStoreThreadCache entry "
            "must still be live at the moment process B finished its "
            "cleanup cycle, or this test proves nothing about the bug"
        )
        assert results["B"]["ok"], f"deleter process failed: {results['B']}"

        # AC1+AC2: a snapshot with a live ChunkStoreThreadCache reader on
        # ANY node/process must not be deleted while that reader holds it.
        assert not results["B"]["deleted"], (
            "BUG #1847: CleanupManager deleted the snapshot while a real, "
            "live ChunkStoreThreadCache reader (an open sqlite3 connection "
            "to a genuine chunks.db) for the SAME path was open in a "
            "separate process -- ChunkStoreThreadCache publishes no "
            "reader lease at all"
        )
        assert snapshot_path.exists(), (
            "the snapshot directory must survive while a live cross-process "
            "ChunkStoreThreadCache reader holds it"
        )
