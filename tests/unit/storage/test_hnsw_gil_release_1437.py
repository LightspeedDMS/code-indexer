"""Regression guard: HNSWIndexManager's load_index()/save_index() must not
freeze the whole process (Bug #1437).

The custom LightspeedDMS/hnswlib fork's Index.load_index()/save_index()
pybind11 bindings used to hold the Python GIL for the entire native file
read/write + graph (de)serialization. In production this blocked the
server's Web UI and MCP front door for the full duration of every HNSW
shard load (multi-hundred-MB/GB temporal shards over NFS = seconds to tens
of seconds). The fork was fixed to release the GIL for these calls
(py::call_guard<py::gil_scoped_release>() on save_index/load_index).

This is code-indexer's OWN regression guard: it exercises the real,
installed hnswlib binding through HNSWIndexManager (no mocking) so a
future hnswlib version bump that drops the GIL-release fix is caught by
this repo's own test suite, not just the fork's.

Marked @pytest.mark.slow (builds a real, meaningfully-sized on-disk HNSW
index) and therefore excluded from fast-automation.sh by its
"-m not slow" filter; run explicitly via
`PYTHONPATH=./src pytest tests/unit/storage/test_hnsw_gil_release_1437.py -v`.
"""

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

# A stalled (GIL held for the whole native call) binding produces a gap
# ratio near 0.8-0.9 (see fork-level bindings_test_gil_release.py); a fixed
# binding produces a gap ratio of a few tenths of a percent. 0.5 sits
# comfortably between the two, with margin on both sides.
MAX_GAP_RATIO = 0.5

# Tuned so load_index()/save_index() each take a meaningful fraction of a
# second on typical CI/dev hardware -- large enough that recorder-thread
# starvation cannot be missed, small enough to keep this test's wall-clock
# cost bounded.
NUM_VECTORS = 300_000
DIM = 128
HNSW_M = 8
HNSW_EF_CONSTRUCTION = 40

# Recorder-thread tuning (see _max_recorder_gap docstring).
RECORDER_CHUNK_SIZE = 200  # pure-Python no-op iterations between timestamps
RECORDER_WARMUP_SECONDS = 0.05  # let the recorder start before the timed call
RECORDER_COOLDOWN_SECONDS = 0.15  # gather a few post-call samples too
RECORDER_JOIN_TIMEOUT_SECONDS = 60

# Each timed native call must take at least this long, or the test can't
# meaningfully distinguish "GIL released" from "call was just very fast".
MIN_LOAD_CALL_SECONDS = 0.1
MIN_SAVE_CALL_SECONDS = 0.05


def _max_recorder_gap(blocking_fn, *, min_call_seconds):
    """Run `blocking_fn()` on the main thread while a background "recorder"
    thread continuously appends monotonic timestamps.

    If the GIL is held for the whole duration of `blocking_fn()`, the
    recorder thread cannot run AT ALL during that window -- the largest gap
    between two consecutive recorded timestamps will be approximately equal
    to the call's duration. If the GIL is released, the recorder keeps
    appending timestamps every few milliseconds throughout the call, so the
    largest gap stays a small fraction of the call duration.

    Returns (max_gap_seconds, call_duration_seconds). The recorder thread is
    always stopped and joined, even if `blocking_fn()` raises.
    """
    stop_flag = threading.Event()
    timestamps = []

    def _recorder():
        while not stop_flag.is_set():
            for _ in range(RECORDER_CHUNK_SIZE):
                pass
            timestamps.append(time.monotonic())

    thread = threading.Thread(target=_recorder)
    thread.start()
    time.sleep(RECORDER_WARMUP_SECONDS)

    try:
        call_start = time.monotonic()
        blocking_fn()
        call_duration = time.monotonic() - call_start
        time.sleep(RECORDER_COOLDOWN_SECONDS)  # gather a few post-call samples too
    finally:
        stop_flag.set()
        thread.join(timeout=RECORDER_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "recorder thread never stopped"

    assert call_duration >= min_call_seconds, (
        "native call completed too fast to meaningfully exercise the "
        "GIL-release behavior under test -- increase NUM_VECTORS"
    )

    ordered = sorted(timestamps)
    assert len(ordered) >= 2, (
        "recorder thread produced too few samples to measure a gap"
    )
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    return max(gaps), call_duration


def _assert_gil_released(max_gap: float, call_duration: float, *, native_call_name: str) -> None:
    gap_ratio = max_gap / call_duration
    assert gap_ratio < MAX_GAP_RATIO, (
        f"recorder thread was silent for {gap_ratio:.1%} of {native_call_name}'s "
        f"{call_duration:.3f}s duration (max_gap={max_gap:.3f}s) -- the GIL was "
        f"likely held for the whole native call (regression of the #1437 fix -- "
        f"check that the installed hnswlib still carries "
        f"py::call_guard<py::gil_scoped_release>() on save_index/load_index)"
    )


@pytest.mark.slow
class TestHNSWIndexReleasesGIL:
    """Prove HNSWIndexManager's load_index()/save_index() native calls
    release the GIL, using a real (not mocked) on-disk HNSW index.
    """

    @pytest.fixture(scope="class")
    def built_collection(self, tmp_path_factory):
        """Build a real, meaningfully-sized HNSW index once for this class."""
        collection_path: Path = tmp_path_factory.mktemp("hnsw_gil_release_1437")
        manager = HNSWIndexManager(vector_dim=DIM, space="l2")

        rng = np.random.default_rng(1437)
        vectors = rng.random((NUM_VECTORS, DIM)).astype(np.float32)
        ids = [f"vec_{i}" for i in range(NUM_VECTORS)]

        manager.build_index(
            collection_path,
            vectors,
            ids,
            M=HNSW_M,
            ef_construction=HNSW_EF_CONSTRUCTION,
        )

        index_file = collection_path / HNSWIndexManager.INDEX_FILENAME
        assert index_file.exists()
        assert index_file.stat().st_size > 0

        return manager, collection_path

    def test_load_index_releases_gil_during_native_call(self, built_collection):
        """A concurrent Python recorder thread must keep making real
        progress while HNSWIndexManager.load_index() runs on the main
        thread -- proving the real installed hnswlib binding releases the
        GIL for the native file-read + deserialize duration."""
        manager, collection_path = built_collection

        def _do_load():
            index = manager.load_index(collection_path, max_elements=NUM_VECTORS)
            assert index is not None

        max_gap, call_duration = _max_recorder_gap(
            _do_load, min_call_seconds=MIN_LOAD_CALL_SECONDS
        )
        _assert_gil_released(max_gap, call_duration, native_call_name="load_index()")

    def test_save_index_releases_gil_during_native_call(self, built_collection, tmp_path):
        """Same proof as above, for the native save_index() call reached
        through HNSWIndexManager's own internal `_save_hnsw_index` wrapper
        (the exact code path build_index()/rebuild_from_vectors() use)."""
        manager, collection_path = built_collection
        index = manager.load_index(collection_path, max_elements=NUM_VECTORS)
        assert index is not None

        save_path = str(tmp_path / "resaved_hnsw_index.bin")

        def _do_save():
            manager._save_hnsw_index(index, save_path)

        max_gap, call_duration = _max_recorder_gap(
            _do_save, min_call_seconds=MIN_SAVE_CALL_SECONDS
        )
        _assert_gil_released(max_gap, call_duration, native_call_name="save_index()")
