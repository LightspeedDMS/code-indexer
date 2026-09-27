"""Unit test for Bug #1969 Round 6, finding P1-2 (concurrent sidecar
writers): `record_self_heal_reprocess_pending`/`clear_self_heal_reprocess_
paths` (collection_dedup_repair.py) do an unsynchronized read-merge-write
with NO locking of their own -- two concurrent writers can read `{old}`
and write `{old,a}` / `{old,b}`, losing one path.

Investigation (documented at the fix site): every OTHER corrupt-id_index
escalation call site in `FilesystemVectorStore` (`get_point`/
`upsert_points`/`delete_points`/etc, all via `_load_id_index`) already
serializes through its store instance's `self._id_index_lock` -- confirmed by
reading each call site (`get_point` at the `with self._id_index_lock:`
wrapping its `_load_id_index` call, same for `upsert_points`,
`delete_points`, `_batch_update_points`, etc). `scroll_points()`'s OWN
separate Bug #1579/#1969-Round-4 repair chain (`repair_duplicate_and_
shifted_points` -> `recover_from_corrupt_id_index_by_wiping_files` ->
`record_self_heal_reprocess_pending`) did not hold this lock, so a thread
inside it could race a concurrent write-path escalation on the SAME
sidecar file within the SAME store. The SmartIndexer sidecar clear also
does not acquire that lock, and separate stores have distinct locks.

Cross-process serialization is NOT established by `IndexingLock`:
`cidx watch` calls `process_files_incrementally()` without it, and
`IndexingLock.acquire()` checks then overwrites a heartbeat file rather
than atomically claiming ownership. The sidecar needs its own safe
record/clear protocol, including when separate store instances or
processes use the same collection. The two direct sidecar tests below
force lost-record and clear-versus-record interleavings.

The first test proves the IN-PROCESS gap with a real two-thread mutual-
exclusion measurement: a thread is parked inside
`recover_from_corrupt_id_index_by_wiping_files` (monkeypatched to pause)
while `scroll_points()`'s repair block is holding (once fixed)
`self._id_index_lock`; a concurrent `get_point()` call -- which always
acquires that SAME lock to populate its id-index cache -- must block
until the parked thread releases it.
"""

import hashlib
import json
import multiprocessing
import threading
import time
from pathlib import Path
from unittest.mock import patch

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared import collection_dedup_repair as dedup_repair

VECTOR_DIM = 4
RELEASE_DELAY_SECONDS = 0.3


def _write_repairable_duplicate_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    shard_suffix: str,
    path: str = "src/foo.py",
) -> Path:
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = hashlib.md5(unique_key.encode()).hexdigest()
    payload = {
        "path": path,
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": 1,
        "line_end": 10,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _add_hnsw_build_metadata(collection_dir: Path) -> None:
    meta_path = collection_dir / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["hnsw_index"] = {
        "version": 1,
        "vector_dim": VECTOR_DIM,
        "space": "cosine",
        "vector_count": 0,
        "id_mapping": {},
    }
    meta_path.write_text(json.dumps(meta))


def _corrupt_id_index_bin(collection_dir: Path) -> None:
    (collection_dir / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def _build_corrupt_collection_with_repairable_duplicate(tmp_path: Path, path: str):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _add_hnsw_build_metadata(collection_path)
    _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round6lock",
        index=0,
        vector=[0.1, 0.2, 0.3, 0.4],
        shard_suffix="-a",
        path=path,
    )
    _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round6lock",
        index=0,
        vector=[0.9, 0.9, 0.9, 0.9],
        shard_suffix="-b",
        path=path,
    )
    _corrupt_id_index_bin(collection_path)
    return store, collection_path


def test_scroll_points_repair_block_serializes_with_id_index_lock(
    tmp_path: Path, monkeypatch
) -> None:
    store, collection_path = _build_corrupt_collection_with_repairable_duplicate(
        tmp_path, "src/round6_lock.py"
    )

    entered = threading.Event()
    release = threading.Event()

    import code_indexer.storage.shared.collection_dedup_repair as dedup_repair_module

    original_recover = dedup_repair_module.recover_from_corrupt_id_index_by_wiping_files

    def paused_recover(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        dedup_repair_module,
        "recover_from_corrupt_id_index_by_wiping_files",
        paused_recover,
    )

    thread_a_done = threading.Event()

    def run_thread_a():
        store.scroll_points(collection_name="coll", limit=100, self_heal=True)
        thread_a_done.set()

    thread_a = threading.Thread(target=run_thread_a, daemon=True)
    thread_a.start()
    assert entered.wait(timeout=5), (
        "thread A never reached recover_from_corrupt_id_index_by_wiping_files"
    )

    # Release thread A after a fixed, generous delay from a background
    # timer -- long enough that a concurrent get_point() call, IF it must
    # wait for self._id_index_lock (the fix), cannot possibly return
    # before it.
    timer = threading.Timer(RELEASE_DELAY_SECONDS, release.set)
    timer.start()

    thread_b_result = []

    def run_thread_b():
        thread_b_result.append(store.get_point("round6-nonexistent-point-id", "coll"))

    start = time.monotonic()
    thread_b = threading.Thread(target=run_thread_b)
    thread_b.start()
    thread_b.join(timeout=5)
    elapsed = time.monotonic() - start

    thread_a.join(timeout=5)
    timer.join(timeout=1)

    assert thread_a_done.is_set(), "thread A's scroll_points call never completed"
    assert thread_b_result == [None], (
        "get_point for a nonexistent id must return None once it "
        f"proceeds (got {thread_b_result!r})"
    )
    assert elapsed >= RELEASE_DELAY_SECONDS - 0.05, (
        f"get_point() returned after only {elapsed:.3f}s while thread A "
        f"was paused inside scroll_points' own repair/wipe escalation "
        f"(released after {RELEASE_DELAY_SECONDS}s) -- "
        "self._id_index_lock is not held around that escalation, so a "
        "concurrent in-process writer (any OTHER _id_index_lock-guarded "
        "operation) is not blocked and can race collection_dedup_repair"
        ".py's unsynchronized record_self_heal_reprocess_pending "
        "read-merge-write cycle, losing an entry."
    )


def _run_sidecar_race(
    collection_dir: Path,
    monkeypatch,
    first_operation,
    second_operation,
) -> None:
    """Pause the first operation after it enumerates the marker directory
    (the only shared read left in the Round 6 P1-2 lock-free design --
    ``record_self_heal_reprocess_pending`` no longer reads anything before
    writing its own independent marker, so this pause is only reachable
    when `first_operation` is a ``clear`` call), then start a second
    writer. Forcing the pause is a best-effort interleaving aid, not a
    requirement: a `first_operation` with nothing to pause on (a plain
    record) simply proceeds immediately, which is itself the point --
    there is no shared state left for two concurrent records to race on.
    """
    first_read = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    failures = []
    original_read = dedup_repair._read_self_heal_reprocess_markers

    def paused_read(directory):
        result = original_read(directory)
        if threading.current_thread().name == "sidecar-first":
            first_read.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first sidecar writer was never released")
        return result

    monkeypatch.setattr(dedup_repair, "_read_self_heal_reprocess_markers", paused_read)

    def run_first():
        try:
            first_operation(collection_dir)
        except BaseException as exc:
            failures.append(exc)

    def run_second():
        second_started.set()
        try:
            second_operation(collection_dir)
        except BaseException as exc:
            failures.append(exc)
        finally:
            second_finished.set()

    first = threading.Thread(target=run_first, name="sidecar-first")
    second = threading.Thread(target=run_second, name="sidecar-second")
    try:
        first.start()
        # Not asserted: a plain record has nothing to pause on (see
        # docstring above) and would time out here even though it is
        # behaving correctly.
        first_read.wait(timeout=1)
        second.start()
        assert second_started.wait(timeout=5)
        second_finished.wait(timeout=1)
    finally:
        release_first.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert not failures, failures


def test_concurrent_sidecar_records_preserve_both_paths(
    tmp_path: Path, monkeypatch
) -> None:
    _run_sidecar_race(
        tmp_path,
        monkeypatch,
        lambda directory: dedup_repair.record_self_heal_reprocess_pending(
            directory, frozenset({"src/first.py"})
        ),
        lambda directory: dedup_repair.record_self_heal_reprocess_pending(
            directory, frozenset({"src/second.py"})
        ),
    )
    assert dedup_repair.read_pending_self_heal_reprocess_paths(tmp_path) == {
        "src/first.py",
        "src/second.py",
    }


def test_concurrent_sidecar_clear_preserves_new_record(
    tmp_path: Path, monkeypatch
) -> None:
    dedup_repair.record_self_heal_reprocess_pending(
        tmp_path, frozenset({"src/finished.py"})
    )
    _run_sidecar_race(
        tmp_path,
        monkeypatch,
        lambda directory: dedup_repair.clear_self_heal_reprocess_paths(
            directory, {"src/finished.py"}
        ),
        lambda directory: dedup_repair.record_self_heal_reprocess_pending(
            directory, frozenset({"src/new.py"})
        ),
    )
    assert dedup_repair.read_pending_self_heal_reprocess_paths(tmp_path) == {
        "src/new.py"
    }


def test_separate_process_sidecar_records_preserve_both_paths(tmp_path: Path) -> None:
    """A per-event marker file needs no cross-process lock at all (Round 6
    P1-2, Amendment 6): each process creates its own marker, so there is
    no shared read for either to pause on -- unlike the legacy shared-file
    design this test originally targeted."""
    context = multiprocessing.get_context("fork")
    first_read = context.Event()
    release_first = context.Event()
    second_finished = context.Event()

    def first_writer():
        original_read = dedup_repair._read_self_heal_reprocess_markers

        def paused_read(directory):
            result = original_read(directory)
            first_read.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first process was never released")
            return result

        dedup_repair._read_self_heal_reprocess_markers = paused_read
        dedup_repair.record_self_heal_reprocess_pending(
            tmp_path, frozenset({"src/first.py"})
        )

    def second_writer():
        try:
            dedup_repair.record_self_heal_reprocess_pending(
                tmp_path, frozenset({"src/second.py"})
            )
        finally:
            second_finished.set()

    first = context.Process(target=first_writer)
    second = context.Process(target=second_writer)
    try:
        first.start()
        # Not asserted: a plain record never reaches this seam in the new
        # design (see docstring above).
        first_read.wait(timeout=1)
        second.start()
        second_finished.wait(timeout=1)
    finally:
        release_first.set()
        if first.pid is not None:
            first.join(timeout=5)
        if second.pid is not None:
            second.join(timeout=5)
    assert first.exitcode == 0 and second.exitcode == 0
    assert dedup_repair.read_pending_self_heal_reprocess_paths(tmp_path) == {
        "src/first.py",
        "src/second.py",
    }


def test_separate_process_records_survive_without_os_lock(tmp_path: Path) -> None:
    """Distinct workers must preserve both records without NFS lock support."""
    context = multiprocessing.get_context("fork")
    first_read = context.Event()
    release_first = context.Event()
    second_finished = context.Event()

    def first_writer() -> None:
        # mypy has no static knowledge of these names on the module (they
        # were removed under Amendment 8's legacy-lock cleanup); setattr
        # bypasses static attribute checking without changing behavior.
        setattr(dedup_repair, "nfs_safe_flock", lambda _fd, _op: True)
        setattr(dedup_repair, "nfs_safe_funlock", lambda _fd, _used: None)
        original_read = dedup_repair.read_pending_self_heal_reprocess_paths

        def paused_legacy_read(directory):
            result = original_read(directory)
            first_read.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first writer was never released")
            return result

        # The current shared-file implementation reads before replacing.
        # A per-event marker writer need not call this seam at all.
        dedup_repair.read_pending_self_heal_reprocess_paths = paused_legacy_read
        dedup_repair.record_self_heal_reprocess_pending(
            tmp_path, frozenset({"src/first.py"})
        )

    def second_writer() -> None:
        # mypy has no static knowledge of these names on the module (they
        # were removed under Amendment 8's legacy-lock cleanup); setattr
        # bypasses static attribute checking without changing behavior.
        setattr(dedup_repair, "nfs_safe_flock", lambda _fd, _op: True)
        setattr(dedup_repair, "nfs_safe_funlock", lambda _fd, _used: None)
        try:
            dedup_repair.record_self_heal_reprocess_pending(
                tmp_path, frozenset({"src/second.py"})
            )
        finally:
            second_finished.set()

    first = context.Process(target=first_writer)
    second = context.Process(target=second_writer)
    try:
        first.start()
        # This event forces the lost-update interleaving on the current
        # implementation. Its absence is valid for a read-free writer.
        first_read.wait(timeout=1)
        second.start()
        assert second_finished.wait(timeout=5), "second writer did not finish"
    finally:
        release_first.set()
        if first.pid is not None:
            first.join(timeout=5)
        if second.pid is not None:
            second.join(timeout=5)

    assert first.exitcode == 0 and second.exitcode == 0
    assert dedup_repair.read_pending_self_heal_reprocess_paths(tmp_path) == {
        "src/first.py",
        "src/second.py",
    }


def test_separate_process_clear_keeps_later_same_path_record_without_os_lock(
    tmp_path: Path,
) -> None:
    """A clear must remove only the old event, even when the new event
    names the same path and no cross-process file lock is honored.

    Pausing immediately before clear's unlink is supported by both the
    committed single-file implementation and the marker-file design. On
    committed HEAD, the later record rewrites the shared sidecar and the
    paused clear unlinks it, losing the new event. With per-event markers,
    clear unlinks only the marker it enumerated before the new record.
    """
    rel_path = "src/repeated.py"
    dedup_repair.record_self_heal_reprocess_pending(tmp_path, frozenset({rel_path}))
    context = multiprocessing.get_context("fork")
    about_to_unlink = context.Event()
    release_clear = context.Event()
    record_finished = context.Event()

    def clearer() -> None:
        # mypy has no static knowledge of these names on the module (they
        # were removed under Amendment 8's legacy-lock cleanup); setattr
        # bypasses static attribute checking without changing behavior.
        setattr(dedup_repair, "nfs_safe_flock", lambda _fd, _op: True)
        setattr(dedup_repair, "nfs_safe_funlock", lambda _fd, _used: None)
        original_unlink = Path.unlink

        def paused_unlink(path: Path, *args, **kwargs):
            about_to_unlink.set()
            if not release_clear.wait(timeout=5):
                raise AssertionError("clear was never released")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", paused_unlink):
            dedup_repair.clear_self_heal_reprocess_paths(tmp_path, {rel_path})

    def recorder() -> None:
        # mypy has no static knowledge of these names on the module (they
        # were removed under Amendment 8's legacy-lock cleanup); setattr
        # bypasses static attribute checking without changing behavior.
        setattr(dedup_repair, "nfs_safe_flock", lambda _fd, _op: True)
        setattr(dedup_repair, "nfs_safe_funlock", lambda _fd, _used: None)
        try:
            dedup_repair.record_self_heal_reprocess_pending(
                tmp_path, frozenset({rel_path})
            )
        finally:
            record_finished.set()

    clear_process = context.Process(target=clearer)
    record_process = context.Process(target=recorder)
    try:
        clear_process.start()
        assert about_to_unlink.wait(timeout=5), "clear did not reach unlink"
        record_process.start()
        assert record_finished.wait(timeout=5), "new record did not finish"
    finally:
        release_clear.set()
        if clear_process.pid is not None:
            clear_process.join(timeout=5)
        if record_process.pid is not None:
            record_process.join(timeout=5)

    assert clear_process.exitcode == 0 and record_process.exitcode == 0
    assert dedup_repair.read_pending_self_heal_reprocess_paths(tmp_path) == {rel_path}
