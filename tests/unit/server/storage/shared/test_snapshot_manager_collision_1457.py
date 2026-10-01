"""Collision-safe version-id generation for VersionedSnapshotManager (Story #1457 AC9).

create_snapshot currently computes ``timestamp = int(time.time())`` and builds
``v_{timestamp}`` with NO existence check. Two version-creations for the SAME
namespace within the SAME wall-clock second collide on the identical
destination path -- and ``cp --reflink=auto -a source <existing-dir>`` copies
the source INSIDE the existing directory (nesting) rather than erroring,
producing a structurally corrupt snapshot that then gets published via
swap_alias. This story makes temporal quarter-shard versioning a new, more
frequent caller of this same shared code path, materially raising collision
exposure.

Fix: generation must be collision-checked -- compute v_{ts}, verify the
destination does not already exist, and on collision retry with an
incremented/re-sampled id within a bounded iteration count.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from code_indexer.server.storage.shared.snapshot_manager import VersionedSnapshotManager


def test_create_snapshot_retries_to_fresh_path_on_collision(tmp_path):
    """A pre-existing v_{ts} destination must NOT be reused/nested into."""
    manager = VersionedSnapshotManager(versioned_base=str(tmp_path))

    # Simulate a prior snapshot already occupying the timestamp create_snapshot
    # would naively compute for this call.
    colliding_path = Path(tmp_path) / ".versioned" / "myrepo" / "v_1700000000"
    colliding_path.mkdir(parents=True)
    (colliding_path / "sentinel.txt").write_text("pre-existing snapshot content")

    with (
        patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time,
        patch("subprocess.run") as mock_run,
    ):
        mock_time.time.return_value = 1700000000
        result = manager.create_snapshot("myrepo", "/some/source")

    assert result != str(colliding_path), (
        "create_snapshot must not reuse a colliding v_{ts} destination path"
    )
    assert Path(result).name != "v_1700000000", (
        "create_snapshot must retry to a different version id on collision, "
        f"got {result}"
    )
    # The pre-existing colliding directory's content must be untouched --
    # proving no nested cp-into-existing-dir corruption occurred.
    assert (colliding_path / "sentinel.txt").read_text() == (
        "pre-existing snapshot content"
    )
    # The actual cp invocation used the fresh, non-colliding destination.
    assert mock_run.called
    cp_dest_arg = mock_run.call_args[0][0][-1]
    assert cp_dest_arg == result


def test_production_clone_backend_path_is_collision_safe_not_just_the_unused_fallback(
    tmp_path,
):
    """Story #1457 HIGH #8 (2026-07-23 code review): the test above only
    exercises the CoW FALLBACK path (VersionedSnapshotManager constructed
    WITHOUT a clone_backend) -- a path production NEVER uses.

    Production ALWAYS constructs VersionedSnapshotManager WITH a real
    CloneBackend (LocalCloneBackend for filesystem CoW mode). This test
    uses that EXACT production shape, with REAL filesystem operations (no
    subprocess.run mock -- genuine cp --reflink=auto execution), to prove
    the SAME collision protection applies there too, not just the unused
    fallback.
    """
    from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

    manager = VersionedSnapshotManager(
        versioned_base=str(tmp_path),
        clone_backend=LocalCloneBackend(versioned_base=str(tmp_path)),
    )

    source1 = tmp_path / "source1"
    source1.mkdir()
    (source1 / "marker.txt").write_text("first source content")

    source2 = tmp_path / "source2"
    source2.mkdir()
    (source2 / "marker.txt").write_text("second source content")

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.return_value = 1700000000
        result1 = manager.create_snapshot("myrepo", str(source1))
        result2 = manager.create_snapshot("myrepo", str(source2))

    assert result1 != result2, (
        "two same-second create_snapshot calls for the SAME namespace must "
        "land at DISTINCT paths -- the production clone_backend dispatch "
        "path must be collision-safe, not just the unused fallback"
    )
    assert Path(result1, "marker.txt").read_text() == "first source content", (
        "the FIRST snapshot must be uncorrupted -- not nested-into by the second"
    )
    assert Path(result2, "marker.txt").read_text() == "second source content"


_CONCURRENCY_NUM_THREADS = 2
_CONCURRENCY_BARRIER_TIMEOUT_SECONDS = 5
_CONCURRENCY_MOCK_TIMESTAMP = 1700000000
_CONCURRENCY_JOIN_TIMEOUT_SECONDS = 10


def test_concurrent_threads_creating_same_second_snapshots_do_not_collide(tmp_path):
    """Story #1457 HIGH #8 (2026-07-23 code review): genuine CONCURRENT
    (not just sequential) same-second creation must not collide. Two REAL
    threads, synchronized via a barrier so both are guaranteed to be
    racing inside create_snapshot at the same moment (no sleep-based
    timing), both targeting the SAME namespace with the SAME mocked
    timestamp. The clock is patched ONCE from the main thread (wrapping
    both worker threads), never concurrently inside each thread, to avoid
    an unsynchronized patch/unpatch race on the shared module attribute.
    """
    import threading

    from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

    manager = VersionedSnapshotManager(
        versioned_base=str(tmp_path),
        clone_backend=LocalCloneBackend(versioned_base=str(tmp_path)),
    )

    source_a = tmp_path / "source_a"
    source_a.mkdir()
    (source_a / "marker.txt").write_text("thread A content")

    source_b = tmp_path / "source_b"
    source_b.mkdir()
    (source_b / "marker.txt").write_text("thread B content")

    barrier = threading.Barrier(_CONCURRENCY_NUM_THREADS)
    results: dict = {}
    results_lock = threading.Lock()
    errors: list = []

    def _racing_time():
        barrier.wait(timeout=_CONCURRENCY_BARRIER_TIMEOUT_SECONDS)
        return _CONCURRENCY_MOCK_TIMESTAMP

    def _worker(key, source):
        try:
            result = manager.create_snapshot("racerepo", str(source))
            with results_lock:
                results[key] = result
        except Exception as exc:  # noqa: BLE001
            with results_lock:
                errors.append(exc)

    with patch("code_indexer.server.storage.shared.snapshot_manager.time") as mock_time:
        mock_time.time.side_effect = _racing_time

        t_a = threading.Thread(target=_worker, args=("a", source_a))
        t_b = threading.Thread(target=_worker, args=("b", source_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=_CONCURRENCY_JOIN_TIMEOUT_SECONDS)
        t_b.join(timeout=_CONCURRENCY_JOIN_TIMEOUT_SECONDS)

    assert not errors, f"unexpected exceptions in threads: {errors}"
    assert results.get("a") is not None and results.get("b") is not None
    assert results["a"] != results["b"], (
        "two THREADS racing to create a same-second snapshot for the SAME "
        "namespace must land at DISTINCT paths"
    )
    assert Path(results["a"], "marker.txt").read_text() == "thread A content"
    assert Path(results["b"], "marker.txt").read_text() == "thread B content"
