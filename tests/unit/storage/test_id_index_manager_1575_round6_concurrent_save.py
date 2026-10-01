"""Bug #1575 round 6, item 3b (found by the #1579 agent while working a
different bug, confirmed via direct 8-thread reproduction outside pytest).

``IDIndexManager.save_index()`` writes to a FIXED temp filename
(``index_file.with_suffix(".bin.tmp")`` == ``id_index.bin.tmp``), guarded
only by ``self._lock`` -- an instance-local ``threading.RLock()``. Every
production call site in this codebase constructs a FRESH
``IDIndexManager()`` per call (confirmed: ``filesystem_vector_store.py``,
``temporal_reconciliation.py``, ``collection_dedup_repair.py``,
``collection_migration.py``, ``daemon/service.py`` -- none share an
instance), so that lock is never actually shared across concurrent
callers for the SAME collection: two threads racing to save the same
collection's ``id_index.bin`` each acquire their OWN uncontended lock and
proceed to write/rename the SAME fixed tmp path concurrently. One
thread's ``os.replace()`` can then target a tmp file the OTHER thread
already renamed away, raising ``FileNotFoundError``.

Fix: a per-call-unique temp filename (pid + thread-id suffix, matching
``FilesystemVectorStore._atomic_write_json``'s established convention) so
concurrent writers can never collide on the same tmp path, regardless of
whether their ``IDIndexManager`` instances share a lock.

Real threads, real filesystem, real ``IDIndexManager`` -- no mocking of
the code under test. Errors are collected via a thread-safe ``queue.Queue``
(never a bare shared list) and every worker thread's completion is
explicitly confirmed via ``is_alive()`` after joining.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from code_indexer.storage.id_index_manager import IDIndexManager

NUM_THREADS = 8
SAVES_PER_THREAD = 25
JOIN_TIMEOUT_SECONDS = 60


def test_concurrent_save_index_from_separate_manager_instances_never_raises(
    tmp_path: Path,
) -> None:
    collection_path = tmp_path / "coll"
    collection_path.mkdir()

    errors: "queue.Queue" = queue.Queue()
    barrier = threading.Barrier(NUM_THREADS)

    def worker(thread_index: int) -> None:
        barrier.wait()
        try:
            for i in range(SAVES_PER_THREAD):
                # A FRESH IDIndexManager() per call -- exactly the real
                # production call pattern every call site in this codebase
                # uses (never a shared instance).
                manager = IDIndexManager()
                point_id = f"pt_{thread_index}_{i}"
                manager.save_index(
                    collection_path,
                    {point_id: collection_path / f"{point_id}.json"},
                )
        except Exception as exc:  # noqa: BLE001 -- capturing for the assertion below
            errors.put((thread_index, exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT_SECONDS)
    still_alive = [t for t in threads if t.is_alive()]
    assert not still_alive, (
        f"{len(still_alive)} worker thread(s) did not finish within "
        f"{JOIN_TIMEOUT_SECONDS}s -- a genuine hang, not a pass"
    )

    collected_errors = []
    while not errors.empty():
        collected_errors.append(errors.get_nowait())

    assert not collected_errors, (
        f"expected ZERO exceptions from {NUM_THREADS} threads concurrently "
        f"calling IDIndexManager().save_index() against the same "
        f"collection, got {len(collected_errors)}: {collected_errors!r} -- "
        f"this reproduces the fixed-shared-tmp-filename race (one thread's "
        f"os.replace() target vanishing mid-flight because another thread "
        f"renamed away the SAME 'id_index.bin.tmp' path)"
    )

    # Final state must be readable and non-corrupt (the last writer wins).
    final_index = IDIndexManager().load_index(collection_path)
    assert isinstance(final_index, dict)
