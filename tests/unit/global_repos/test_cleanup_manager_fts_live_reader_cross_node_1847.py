"""Bug #1847 AC1/AC2 -- the Tantivy FTS index cache is a long-lived reader
of versioned snapshots, exactly like the HNSW cache Bug #1845 protected,
but it publishes no reader lease at all today.

EVIDENCE that FTSIndexCache genuinely outlives a single request (read from
source this run, ``server/cache/fts_index_cache.py``):

- ``get_or_load()`` populates an ``FTSIndexCacheEntry`` holding the live
  ``tantivy.Index`` object, with its own TTL (default 10 minutes,
  ``FTSIndexCacheConfig.ttl_minutes``) independent of any single request's
  lifetime -- evicted only via ``invalidate()``/expiry (lines 161-186,
  320-352), the identical shape as ``HNSWIndexCacheEntry``.
- Worse than HNSW in one respect: with the DEFAULT ``reload_on_access=True``
  (line 48), every cache HIT calls ``entry.tantivy_index.reload()`` (line
  338) -- an ACTIVE re-read of the on-disk segment files on every query
  against a cached entry, not merely a passive in-RAM serve. If the
  snapshot directory is deleted while an entry is cached, the very next
  query against it triggers a real filesystem read against a path that no
  longer exists -- ESTALE on NFS, or a crash if any prior mmap'd segment
  region is still referenced.
- ``FTSIndexCache.__init__`` (line 234) accepts only a ``config`` argument
  today -- unlike ``HNSWIndexCache.__init__``, there is no ``lease_root``
  or ``is_versioned_snapshot`` parameter at all, so there is no way for a
  caller to make this cache publish a reader lease even if it wanted to.

This test builds a REAL Tantivy index on disk via the production
``TantivyIndexManager`` (``services/tantivy_index_manager.py``), loads it
into a REAL ``FTSIndexCache`` through the exact ``get_index_for_caching()``
/ ``open_for_search()`` pair that method's own docstring says is "used by
server-side caching to inject pre-loaded index" -- not a synthetic
loader -- in one separate OS process, while a REAL ``CleanupManager`` runs
its real deletion cycle against the same snapshot path in a second,
separate OS process. Two REAL OS processes (``multiprocessing.get_context
("spawn")``) because the defect IS that a reader's liveness on one
node/process is invisible to a deleter on another -- a single-process test
cannot fail for the right reason (see
test_cleanup_manager_live_reader_cross_node_1845.py, whose shape this test
follows directly).
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
from typing import Any, Optional, Tuple

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
from code_indexer.services.tantivy_index_manager import TantivyIndexManager


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


def _write_real_fts_index(index_dir: Path) -> None:
    """Build and persist a genuinely valid Tantivy index on disk -- via the
    REAL production ``TantivyIndexManager``, the same class the CLI/server
    indexing path uses, not a synthetic stand-in."""
    manager = TantivyIndexManager(index_dir)
    try:
        manager.initialize_index(create_new=True)
        manager.add_document(
            {
                "path": "example.py",
                "content": "def hello_world(): return 42",
                "content_raw": "def hello_world(): return 42",
                "identifiers": ["hello_world"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        manager.commit()
    finally:
        manager.close()


def _fts_ttl_remaining(cache: Any, snapshot_path: str) -> Optional[float]:
    """Return the cached entry's ttl_remaining_seconds for snapshot_path,
    or None if there is no entry at all."""
    normalized = str(Path(snapshot_path).resolve())
    repo_stats = cache.get_stats().per_repository_stats.get(normalized)
    return None if repo_stats is None else repo_stats["ttl_remaining_seconds"]


def _load_fts_reader(snapshot_path: str, mount_point: str) -> Any:
    """Construct a real, leased ``FTSIndexCache`` and load ``snapshot_path``
    into it via the production ``get_index_for_caching()`` /
    ``open_for_search()`` pathway.

    ``lease_root``/``is_versioned_snapshot`` are the SAME injection shape
    ``HNSWIndexCache`` already accepts (Bug #1845 remediation round 2). At
    the time this test was written, ``FTSIndexCache`` accepts NEITHER --
    this call intentionally exercises the constructor shape the fix must
    add, mirroring ``server/cache/__init__.py``'s eventual
    ``_resolve_fts_lease_kwargs()``.
    """
    from code_indexer.server.cache.fts_index_cache import (
        FTSIndexCache,
        FTSIndexCacheConfig,
    )
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    lease_root = Path(mount_point) / "cidx-meta"

    def _loader() -> Tuple[Any, Any]:
        # Any/Any: tantivy is a Rust extension with no Python type stubs
        # (same reason tantivy_index_manager.py's own get_index_for_caching()
        # returns tuple[Any, Any], and the same convention
        # test_cleanup_manager_live_reader_cross_node_1845.py uses for
        # hnswlib.Index).
        index_manager = TantivyIndexManager(Path(snapshot_path) / "fts_index")
        index_manager.open_for_search()
        return index_manager.get_index_for_caching()

    cache = FTSIndexCache(
        FTSIndexCacheConfig(ttl_minutes=READER_TTL_MINUTES),
        lease_root=lease_root,
        is_versioned_snapshot=is_versioned_snapshot,
    )
    cache.get_or_load(snapshot_path, _loader)
    return cache


def reader_worker(
    snapshot_path: str, mount_point: str, ready_event, release_event, result_queue
) -> None:
    """Process A: a REAL, separate OS process. Loads the real Tantivy
    index at ``snapshot_path`` through the REAL, unmodified, already-
    existing production reader lifecycle named in the bug report --
    ``FTSIndexCache.get_or_load()`` -- and stays alive holding that cache
    entry while process B runs its real deletion cycle against the SAME
    path.
    """
    try:
        cache = _load_fts_reader(snapshot_path, mount_point)

        if _fts_ttl_remaining(cache, snapshot_path) is None:
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
        remaining = _fts_ttl_remaining(cache, snapshot_path)
        still_live = remaining is not None and remaining > 0
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
    ``FTSIndexCache`` (a distinct Python interpreter has no way to see
    it), which is exactly the bug.
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
        # Must be the SAME root process A's FTSIndexCache is injected with
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
    _write_real_fts_index(snapshot / "fts_index")
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
    # rebuilding it -- a real race, reproduced during development as
    # FileNotFoundError inside SemLock._rebuild in the child.
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


class TestSnapshotNotDeletedWhileLiveFTSReaderHoldsItCrossProcess:
    def test_snapshot_survives_cleanup_while_fts_reader_on_another_process_holds_it(
        self, tmp_path: Path, mount_point: Path, snapshot_path: Path
    ) -> None:
        """RED on unmodified code: FTSIndexCache accepts no lease_root/
        is_versioned_snapshot at all, so this either fails constructing the
        cache (TypeError -- the missing capability itself) or, once that
        constructor shape exists, CleanupManager deletes the snapshot even
        though a real FTSIndexCache reader entry (loaded from a genuine
        Tantivy index via the production get_index_for_caching() pathway)
        for the exact same path is provably still live in a separate
        process at the moment of deletion. GREEN (after the fix): the
        snapshot must survive while that live reader holds it (AC1+AC2).
        """
        db_path = str(tmp_path / "server.db")
        DatabaseSchema(db_path).initialize_database()
        GoldenRepoMetadataSqliteBackend(db_path).ensure_table_exists()

        ctx = mp.get_context("spawn")
        # ready_event/release_event are unpacked (not just reader/deleter/
        # result_queue) so they stay referenced for the rest of this scope
        # -- see _start_two_processes's return-site comment for why.
        reader, deleter, ready_event, release_event, result_queue = (
            _start_two_processes(ctx, snapshot_path, mount_point, db_path)
        )
        results = _join_and_collect_results(reader, deleter, result_queue)

        assert results["A"]["ok"], f"reader process failed: {results['A']}"
        assert results["A"]["still_live"], (
            "precondition broken: process A's FTSIndexCache entry must "
            "still be live at the moment process B finished its cleanup "
            "cycle, or this test proves nothing about the bug"
        )
        assert results["B"]["ok"], f"deleter process failed: {results['B']}"

        # AC1+AC2: a snapshot with a live FTS reader on ANY node/process
        # must not be deleted while that reader holds it.
        assert not results["B"]["deleted"], (
            "BUG #1847: CleanupManager deleted the snapshot while a real, "
            "live FTSIndexCache reader (loaded from a genuine Tantivy "
            "index) for the SAME path was open in a separate process -- "
            "FTSIndexCache publishes no reader lease at all"
        )
        assert snapshot_path.exists(), (
            "the snapshot directory must survive while a live cross-process "
            "FTS reader holds it"
        )
