"""Bug #1847 AC1/AC2 -- IdIndexCache is a long-lived cross-query reader of
versioned snapshots, exactly like the HNSW and FTS caches, but it publishes
no reader lease at all today.

EVIDENCE that IdIndexCache genuinely outlives a single request (read from
source this run):

- ``server/cache/id_index_cache.py``'s own module docstring: "Mirrors
  HNSWIndexCache exactly" -- same TTL-based eviction (default 10 minutes,
  ``IdIndexCacheConfig.ttl_minutes``), same per-key Event-sentinel
  cross-query caching, evicted only via ``invalidate()``/expiry, same as
  ``HNSWIndexCacheEntry``/``FTSIndexCacheEntry``.
- ``storage/filesystem_vector_store.py``'s ``load_index()`` (~line 6489)
  loads the id_index THROUGH ``self.id_index_cache.get_or_load()`` in the
  SAME worker that loads the HNSW index for the SAME collection/query --
  it is not an unrelated, independently-lived cache.
- The cached value is a ``Dict[str, Path]`` (point_id -> file path) built
  by ``IDIndexManager.load_index()`` (``storage/id_index_manager.py``).
  Those paths point INTO the collection/snapshot directory (chunk shard
  files for the legacy SHARDED_JSON layout). A subsequent cache-HIT query
  within the TTL window reads those paths to hydrate chunk content -- if
  the snapshot directory was deleted in between, that read fails
  (ESTALE/FileNotFoundError), the same risk class as HNSW/FTS.
- ``IdIndexCache.__init__`` (line 142) accepts only a ``config`` argument
  today -- no ``lease_root``/``is_versioned_snapshot`` parameter exists.
- Unlike ``HNSWIndexCache``/``FTSIndexCache``, ``IdIndexCache`` has NO
  ``get_stats()``/introspection method at all, so this test verifies
  liveness through the public ``get_or_load()`` CONTRACT itself (a second
  call with a loader that must not fire on a cache HIT), never a private
  attribute.

This test builds a REAL ``id_index.bin`` on disk via the production
``IDIndexManager`` (``storage/id_index_manager.py``), loads it into a REAL
``IdIndexCache`` through ``IDIndexManager().load_index()`` -- the exact
production loader shape -- in one separate OS process, while a REAL
``CleanupManager`` runs its real deletion cycle against the same snapshot
path in a second, separate OS process. Two REAL OS processes
(``multiprocessing.get_context("spawn")``) because the defect IS that a
reader's liveness on one node/process is invisible to a deleter on
another -- a single-process test cannot fail for the right reason (see
test_cleanup_manager_live_reader_cross_node_1845.py and
test_cleanup_manager_fts_live_reader_cross_node_1847.py, whose shape this
test follows directly).
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
from code_indexer.storage.id_index_manager import IDIndexManager


pytestmark = pytest.mark.slow

#: Bounded join/event deadline for the two-process test (Messi Rule #14 --
#: the test itself must not be able to hang forever either).
PROCESS_DEADLINE_SECONDS = 60.0

#: Bounded wait for a result already known to be sitting in the queue
#: (both worker processes have already been join()'d by this point).
QUEUE_RESULT_TIMEOUT_SECONDS = 10.0

#: Bounded wait for a leftover process to exit after terminate().
TERMINATION_JOIN_TIMEOUT_SECONDS = 10.0

#: Default cache TTL is 10 minutes in production; using it here (rather
#: than an artificially long value) proves the reader is live via the
#: SAME configuration production actually ships.
READER_TTL_MINUTES = 10.0


def _write_real_id_index(collection_path: Path) -> None:
    """Build and persist a genuinely valid id_index.bin -- via the REAL
    production ``IDIndexManager``, the same class the CLI/server indexing
    path uses, not a synthetic stand-in."""
    (collection_path / "vector_0.json").write_text("{}", encoding="utf-8")
    IDIndexManager().save_index(
        collection_path, {"point-1": collection_path / "vector_0.json"}
    )


def _reader_still_holds_entry(cache: Any, collection_path: str) -> bool:
    """Return True iff collection_path is currently a cache HIT.

    IdIndexCache exposes no get_stats()/introspection method (unlike
    HNSWIndexCache/FTSIndexCache), so liveness is observed through the
    public get_or_load() CONTRACT itself: a loader that must never run on
    a genuine cache hit. If it runs, the entry was missing/expired.
    """

    def _loader_must_not_run() -> Any:
        raise AssertionError("cache MISS -- entry not live")

    try:
        cache.get_or_load(collection_path, _loader_must_not_run)
        return True
    except AssertionError:
        return False


def _load_id_index_reader(snapshot_path: str, mount_point: str) -> Any:
    """Construct a real, leased ``IdIndexCache`` and load ``snapshot_path``
    into it via the production ``IDIndexManager().load_index()`` pathway.

    ``lease_root``/``is_versioned_snapshot`` are the SAME injection shape
    ``HNSWIndexCache``/``FTSIndexCache`` already accept. At the time this
    test was written, ``IdIndexCache`` accepts NEITHER -- this call
    intentionally exercises the constructor shape the fix must add.
    """
    from code_indexer.server.cache.id_index_cache import (
        IdIndexCache,
        IdIndexCacheConfig,
    )
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    lease_root = Path(mount_point) / "cidx-meta"

    def _loader() -> Any:
        return IDIndexManager().load_index(Path(snapshot_path))

    cache = IdIndexCache(
        IdIndexCacheConfig(ttl_minutes=READER_TTL_MINUTES),
        lease_root=lease_root,
        is_versioned_snapshot=is_versioned_snapshot,
    )
    cache.get_or_load(snapshot_path, _loader)
    return cache


def reader_worker(
    snapshot_path: str, mount_point: str, ready_event, release_event, result_queue
) -> None:
    """Process A: a REAL, separate OS process. Loads the real id_index at
    ``snapshot_path`` through the REAL, unmodified, already-existing
    production reader lifecycle -- ``IdIndexCache.get_or_load()`` -- and
    stays alive holding that cache entry while process B runs its real
    deletion cycle against the SAME path.
    """
    try:
        cache = _load_id_index_reader(snapshot_path, mount_point)

        if not _reader_still_holds_entry(cache, snapshot_path):
            result_queue.put(
                {
                    "role": "A",
                    "ok": False,
                    "error": "cache entry missing or expired after load",
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
        # deletion happened, not merely that it once loaded it earlier.
        still_live = _reader_still_holds_entry(cache, snapshot_path)
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
    path -- entirely independent of process A's in-memory ``IdIndexCache``
    (a distinct Python interpreter has no way to see it), which is exactly
    the bug.
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
        # Must be the SAME root process A's IdIndexCache is injected with
        # via lease_root=Path(mount_point) / "cidx-meta" in reader_worker,
        # or the two processes would look in different directories and the
        # liveness check would be meaningless.
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
    _write_real_id_index(snapshot)
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


class TestSnapshotNotDeletedWhileLiveIdIndexReaderHoldsItCrossProcess:
    def test_snapshot_survives_cleanup_while_id_index_reader_on_another_process_holds_it(
        self, tmp_path: Path, mount_point: Path, snapshot_path: Path
    ) -> None:
        """RED on unmodified code: IdIndexCache accepts no lease_root/
        is_versioned_snapshot at all, so this either fails constructing the
        cache (TypeError -- the missing capability itself) or, once that
        constructor shape exists, CleanupManager deletes the snapshot even
        though a real IdIndexCache reader entry (loaded from a genuine
        id_index.bin via the production IDIndexManager) for the exact same
        path is provably still live in a separate process at the moment of
        deletion. GREEN (after the fix): the snapshot must survive while
        that live reader holds it (AC1+AC2).
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
            "precondition broken: process A's IdIndexCache entry must "
            "still be live at the moment process B finished its cleanup "
            "cycle, or this test proves nothing about the bug"
        )
        assert results["B"]["ok"], f"deleter process failed: {results['B']}"

        # AC1+AC2: a snapshot with a live IdIndexCache reader on ANY
        # node/process must not be deleted while that reader holds it.
        assert not results["B"]["deleted"], (
            "BUG #1847: CleanupManager deleted the snapshot while a real, "
            "live IdIndexCache reader (loaded from a genuine id_index.bin) "
            "for the SAME path was open in a separate process -- "
            "IdIndexCache publishes no reader lease at all"
        )
        assert snapshot_path.exists(), (
            "the snapshot directory must survive while a live cross-process "
            "IdIndexCache reader holds it"
        )
