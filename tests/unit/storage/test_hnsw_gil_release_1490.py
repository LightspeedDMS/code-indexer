"""Regression guard: additional hnswlib bindings must not freeze the whole
process (Story #1490, follow-up to Bug #1437).

Bug #1437 added py::call_guard<py::gil_scoped_release>() to the main
Index class's save_index()/load_index() bindings in the vendored
LightspeedDMS/hnswlib fork (third_party/hnswlib/python_bindings/bindings.cpp)
because those calls held the Python GIL for the entire native file I/O +
graph (de)serialization, freezing the whole cidx-server process (Web UI,
MCP front door) for the duration of every HNSW shard load/save.

This story finishes the job for the other long-running, GIL-held, in-process
native calls the live server also uses: orphan sweep (check_integrity,
repair_orphans), golden-repo rebuilds and fleet migration
(init_index, resize_index, get_items, get_ids_list, mark_deleted on the main
Index class), and BFIndex save_index/load_index.

Methodology (identical to Bug #1437's test, tests/unit/storage/
test_hnsw_gil_release_1437.py, and the fork's own
third_party/hnswlib/tests/python/bindings_test_gil_release.py): a
concurrent pure-Python "recorder" thread continuously appends
time.monotonic() timestamps while a SINGLE native call under test runs on
the main thread. If the GIL is held for the whole native call, the
recorder cannot run AT ALL during that window -- the largest gap between
two consecutive timestamps approximates the call's duration. If the GIL is
released, the recorder keeps making progress throughout the call, so the
largest gap stays a small fraction of the call duration.

IMPORTANT -- single call only, never a Python loop around the native call:
an earlier draft of this test measured get_ids_list()/mark_deleted() by
looping the call many times inside blocking_fn(). That is invalid: CPython
itself preempts a running thread roughly every `sys.getswitchinterval()`
(default 5ms) independent of what any C extension does with the GIL, so a
loop whose total wall-clock duration exceeds a few switch intervals lets
the recorder thread progress via the interpreter's OWN fairness mechanism,
regardless of whether the timed native call ever releases the GIL itself.
That makes a loop-based measurement pass even for a completely unguarded
binding, defeating the test's purpose. Every test below instead uses ONE
single native call, sized (via a bigger index and/or richer arguments,
never more calls) so that call alone is long enough to measure.

mark_deleted() is the one exception: hnswlib/hnswalg.h's
markDeletedInternal() is a single hash lookup + one bit flip, an O(1)
operation regardless of index size -- there is no way to construct a
single mark_deleted() call that takes measurable wall-clock time (thread
scheduling granularity is on the order of milliseconds; a sub-microsecond
call cannot be observed this way). It is verified instead via a
structural check that the vendored binding source actually carries the
guard, plus a correctness test -- documented in place below.

Marked @pytest.mark.slow (builds several real, meaningfully-sized on-disk
HNSW/BF indexes) and therefore excluded from fast-automation.sh by its
"-m not slow" filter; run explicitly via
`PYTHONPATH=./src pytest tests/unit/storage/test_hnsw_gil_release_1490.py -v`.
"""

import threading
import time
from pathlib import Path

import numpy as np
import pytest

import hnswlib

# Same discriminator threshold as Bug #1437's test: a stalled (GIL held for
# the whole native call) binding produces a gap ratio near 0.8-0.9; a fixed
# binding produces a gap ratio of a few tenths of a percent. 0.5 sits
# comfortably between the two, with margin on both sides.
MAX_GAP_RATIO = 0.5

# Recorder-thread tuning (identical to Bug #1437's test).
RECORDER_CHUNK_SIZE = 200  # pure-Python no-op iterations between timestamps
RECORDER_WARMUP_SECONDS = 0.05  # let the recorder start before the timed call
RECORDER_COOLDOWN_SECONDS = 0.15  # gather a few post-call samples too
RECORDER_JOIN_TIMEOUT_SECONDS = 60

BINDINGS_CPP_PATH = (
    Path(__file__).resolve().parents[3]
    / "third_party"
    / "hnswlib"
    / "python_bindings"
    / "bindings.cpp"
)


def _max_recorder_gap(blocking_fn, *, min_call_seconds):
    """Run `blocking_fn()` (a SINGLE native call) on the main thread while a
    background "recorder" thread continuously appends monotonic timestamps.

    Returns (max_gap_seconds, call_duration_seconds). The recorder thread is
    always stopped and joined, even if `blocking_fn()` raises.

    (Identical logic to tests/unit/storage/test_hnsw_gil_release_1437.py's
    helper of the same name -- the story's mandatory template.)
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
        "GIL-release behavior under test -- increase the workload size"
    )

    ordered = sorted(timestamps)
    assert len(ordered) >= 2, (
        "recorder thread produced too few samples to measure a gap"
    )
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    return max(gaps), call_duration


def _assert_gil_released(
    max_gap: float,
    call_duration: float,
    *,
    native_call_name: str,
    max_gap_ratio: float = MAX_GAP_RATIO,
) -> None:
    gap_ratio = max_gap / call_duration
    assert gap_ratio < max_gap_ratio, (
        f"recorder thread was silent for {gap_ratio:.1%} of {native_call_name}'s "
        f"{call_duration:.3f}s duration (max_gap={max_gap:.3f}s) -- the GIL was "
        f"likely held for the whole native call (missing "
        f"py::call_guard<py::gil_scoped_release>() on {native_call_name} in "
        f"third_party/hnswlib/python_bindings/bindings.cpp -- Story #1490)"
    )


def _build_hnsw_index(n, *, dim, m, ef_construction, seed, num_threads=8):
    """Build a real hnswlib.Index with n random float32 vectors."""
    rng = np.random.default_rng(seed)
    data = np.float32(rng.random((n, dim)))
    index = hnswlib.Index(space="l2", dim=dim)
    index.init_index(max_elements=n, M=m, ef_construction=ef_construction)
    index.set_num_threads(num_threads)
    index.add_items(data)
    return index, data


# ---------------------------------------------------------------------------
# GIL-release proofs: main Index class (each a SINGLE native call)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestInitIndexReleasesGIL:
    """init_index() allocates the internal graph-storage arrays sized by
    max_elements -- a large max_elements makes this a meaningfully long
    native call even with zero data added. dim=32/M=8 (rather than this
    project's production dim=128/M=16) keeps peak RSS bounded to roughly
    350MB instead of several GB, while still exceeding MIN_CALL_SECONDS."""

    DIM = 32
    MAX_ELEMENTS = 8_000_000
    MIN_CALL_SECONDS = 0.08

    def test_init_index_releases_gil_during_native_call(self):
        def _do_init():
            index = hnswlib.Index(space="l2", dim=self.DIM)
            index.init_index(max_elements=self.MAX_ELEMENTS, M=8, ef_construction=40)

        max_gap, call_duration = _max_recorder_gap(
            _do_init, min_call_seconds=self.MIN_CALL_SECONDS
        )
        _assert_gil_released(max_gap, call_duration, native_call_name="init_index()")


@pytest.mark.slow
class TestResizeIndexReleasesGIL:
    """resize_index() reallocates the internal storage arrays sized by the
    NEW max_elements -- starting from a tiny index keeps the fixture build
    cheap while the resize target alone makes the single call meaningfully
    long. dim=32 bounds peak RSS to roughly 300MB at the initial target.

    Bug #1741: a fixed resize target (originally 9,000,000) reliably cleared
    MIN_CALL_SECONDS on the hardware this test was authored against, but a
    sufficiently fast host completes that same call in under the floor
    (observed live: ~0.0698s vs the 0.08s floor) -- the test then fails on
    its own workload-too-small calibration guard inside _max_recorder_gap
    before it ever reaches the GIL-release assertion it exists to verify.
    Lowering the floor would only narrow the window this bug describes, not
    fix it, so instead the workload self-calibrates: if a single
    resize_index() call at the current target doesn't clear
    MIN_CALL_SECONDS, the target is grown by RESIZE_TARGET_GROWTH_FACTOR and
    re-measured as a fresh, independent single-call attempt, bounded by
    MAX_CALIBRATION_ATTEMPTS so a pathologically fast host fails loudly with
    a clear diagnostic instead of retrying forever (anti-unbounded-loop).
    resize_index() supports being called again with a larger target on the
    same index (hnswalg.h's resizeIndex() only requires
    new_max_elements >= cur_element_count), so growing the target across
    calibration attempts on the shared `tiny_index` fixture is valid.

    This does NOT reintroduce the module docstring's forbidden
    same-call-looping pattern: each calibration attempt is still exactly
    ONE native call, timed in its own dedicated _max_recorder_gap()
    invocation (its own recorder thread, its own warmup/cooldown) -- the
    loop is across separate, independent measurements, never inside the
    timed blocking_fn() region itself."""

    DIM = 32
    TINY_ELEMENTS = 1_000
    INITIAL_RESIZE_TARGET = 9_000_000
    RESIZE_TARGET_GROWTH_FACTOR = 1.5
    MIN_CALL_SECONDS = 0.08
    MAX_CALIBRATION_ATTEMPTS = 5

    @pytest.fixture(scope="class")
    def tiny_index(self):
        index = hnswlib.Index(space="l2", dim=self.DIM)
        index.init_index(max_elements=self.TINY_ELEMENTS, M=4, ef_construction=10)
        return index

    def test_resize_index_releases_gil_during_native_call(self, tiny_index):
        target = self.INITIAL_RESIZE_TARGET
        max_gap = None
        call_duration = None

        for attempt in range(1, self.MAX_CALIBRATION_ATTEMPTS + 1):

            def _do_resize(resize_target=target):
                tiny_index.resize_index(resize_target)

            try:
                max_gap, call_duration = _max_recorder_gap(
                    _do_resize, min_call_seconds=self.MIN_CALL_SECONDS
                )
                break
            except AssertionError as exc:
                if "too fast to meaningfully exercise" not in str(exc):
                    raise
                if attempt == self.MAX_CALIBRATION_ATTEMPTS:
                    raise AssertionError(
                        "resize_index() calibration failed to produce a "
                        f"call lasting >= {self.MIN_CALL_SECONDS}s after "
                        f"{self.MAX_CALIBRATION_ATTEMPTS} attempts (largest "
                        f"attempted resize target={target}) -- this host is "
                        "too fast to measure resize_index()'s GIL-release "
                        "behavior even at the largest attempted workload; "
                        "raise INITIAL_RESIZE_TARGET or "
                        "RESIZE_TARGET_GROWTH_FACTOR"
                    ) from exc
                target = int(target * self.RESIZE_TARGET_GROWTH_FACTOR)

        _assert_gil_released(max_gap, call_duration, native_call_name="resize_index()")


class TestGetItemsGuardApplied:
    """get_items() (bindings.cpp's getData()) is NOT amenable to the
    wall-clock recorder-gap methodology used for the other 7 timed
    bindings above. Empirically confirmed (via a temporary debug-
    instrumented build, since reverted): getData()'s pure-C++ copy loop
    (the part this story's fix correctly releases the GIL around) is
    consistently only ~5-15% of the call's TOTAL wall time -- the
    remaining ~85-95% is spent in unavoidable, legitimately GIL-bound
    Python-object construction (py::cast() boxing the output into nested
    Python lists / a numpy array), which happens AFTER the guarded scope
    and genuinely cannot be released (it touches live Python objects).
    This ratio was confirmed intrinsic to the operation across multiple
    workload shapes (dim=128/100k ids, dim=1/1M ids, "list" vs "numpy"
    return_type) -- total float count drives both the loop and the
    marshaling proportionally, so no amount of workload resizing can make
    the releasable portion dominate the call. A recorder-gap test can
    therefore never show <50% freeze for this binding, REGARDLESS of
    whether the guard is correctly applied -- the opposite failure mode
    from mark_deleted (there, the call is too FAST to measure at all;
    here, the call is dominated by other, correctly-GIL-held work).
    Verified instead via a structural check that the fix is genuinely
    present in the vendored source, plus the correctness test below."""

    def test_get_items_body_has_gil_release_scope_in_source(self):
        source = BINDINGS_CPP_PATH.read_text()
        def_marker = "py::object getData("
        assert def_marker in source, (
            "getData() definition not found in "
            f"{BINDINGS_CPP_PATH} -- has it been renamed/removed?"
        )
        start = source.index(def_marker)
        # getData()'s body ends at the next top-level method definition.
        next_method = source.index(
            "std::vector<hnswlib::labeltype> getIdsList()", start
        )
        segment = source[start:next_method]
        assert "py::gil_scoped_release" in segment, (
            "getData() (backing the get_items binding) is missing a "
            "py::gil_scoped_release scope around its copy loop in "
            f"{BINDINGS_CPP_PATH} (Story #1490)"
        )


@pytest.mark.slow
class TestGetIdsListReleasesGIL:
    """get_ids_list() (bindings.cpp's getIdsList()) iterates
    label_lookup_, a hash map keyed by element count -- cost scales with
    element count only, NOT graph degree/connectivity. M=2/ef_construction=4
    keeps the build itself cheap (no meaningful graph needed) while a large
    element count (4M, dim=8 -- dim is irrelevant to this binding's cost,
    kept small to bound peak RSS to roughly 1.2GB) makes ONE
    get_ids_list() call comfortably long (~0.15-0.2s measured) enough that
    OS thread-scheduling wake-up jitter cannot dominate the measurement.

    Uses a non-default, evidence-based max_gap_ratio=0.75 (vs the shared
    0.5): unlike check_integrity()/repair_orphans() (whose return dicts
    are compact and N-independent), getIdsList()'s ENTIRE return value
    scales with N -- pybind11's std::vector<>-to-Python-list conversion
    (unavoidably GIL-bound, boxing one Python int per element) happens
    AFTER this binding's guarded scope and was empirically measured, at
    FOUR independent scales (1.2M/1.8M/6M/8M elements), to occupy a
    consistent ~55-65% of total wall time even with the guard correctly
    applied -- an intrinsic floor, not a sign the fix is missing. This
    is still clearly separated from the unfixed baseline (~90% freeze,
    measured before this story's fix was applied), so 0.75 remains a
    valid discriminator specific to this binding's return-value shape."""

    DIM = 8
    NUM_ELEMENTS = 4_000_000
    MIN_CALL_SECONDS = 0.1

    @pytest.fixture(scope="class")
    def real_index(self):
        index, _data = _build_hnsw_index(
            self.NUM_ELEMENTS,
            dim=self.DIM,
            m=2,
            ef_construction=4,
            seed=1491,
        )
        return index

    def test_get_ids_list_releases_gil_during_native_call(self, real_index):
        result = {}

        def _do_get_ids_list():
            result["ids"] = real_index.get_ids_list()

        max_gap, call_duration = _max_recorder_gap(
            _do_get_ids_list, min_call_seconds=self.MIN_CALL_SECONDS
        )
        assert len(result["ids"]) == self.NUM_ELEMENTS
        _assert_gil_released(
            max_gap,
            call_duration,
            native_call_name="get_ids_list()",
            max_gap_ratio=0.75,
        )


@pytest.mark.slow
class TestCheckIntegrityReleasesGIL:
    """check_integrity() scans every element's inbound-connection count --
    cost scales with element count and graph degree, so a real,
    moderately-sized graph is required (bounded to roughly 200MB peak
    RSS at dim=128/M=8/250k elements)."""

    DIM = 128
    NUM_ELEMENTS = 250_000
    MIN_CALL_SECONDS = 0.1

    @pytest.fixture(scope="class")
    def real_index(self):
        index, _data = _build_hnsw_index(
            self.NUM_ELEMENTS, dim=self.DIM, m=8, ef_construction=40, seed=1493
        )
        return index

    def test_check_integrity_releases_gil_during_native_call(self, real_index):
        result = {}

        def _do_check_integrity():
            result["report"] = real_index.check_integrity()

        max_gap, call_duration = _max_recorder_gap(
            _do_check_integrity, min_call_seconds=self.MIN_CALL_SECONDS
        )
        assert "valid" in result["report"]
        assert result["report"]["element_count"] == self.NUM_ELEMENTS
        _assert_gil_released(
            max_gap, call_duration, native_call_name="check_integrity()"
        )


@pytest.mark.slow
class TestRepairOrphansReleasesGIL:
    """repair_orphans() rescans + repairs zero-inbound nodes -- same
    scaling requirement as check_integrity(). M=8/ef_construction=40 on
    random high-dimensional data reliably produces a real population of
    orphan nodes, so this exercises genuine repair work, not a fast
    already-clean no-op scan."""

    DIM = 128
    NUM_ELEMENTS = 250_000
    MIN_CALL_SECONDS = 0.015

    @pytest.fixture(scope="class")
    def real_index_with_orphans(self):
        index, _data = _build_hnsw_index(
            self.NUM_ELEMENTS, dim=self.DIM, m=8, ef_construction=40, seed=1494
        )
        return index

    def test_repair_orphans_releases_gil_during_native_call(
        self, real_index_with_orphans
    ):
        result = {}

        def _do_repair_orphans():
            result["report"] = real_index_with_orphans.repair_orphans()

        max_gap, call_duration = _max_recorder_gap(
            _do_repair_orphans, min_call_seconds=self.MIN_CALL_SECONDS
        )
        assert result["report"]["orphans_before"] > 0
        assert result["report"]["orphans_after"] == 0
        _assert_gil_released(
            max_gap, call_duration, native_call_name="repair_orphans()"
        )


# ---------------------------------------------------------------------------
# GIL-release proofs: BFIndex class (each a SINGLE native call)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestBFIndexSaveIndexReleasesGIL:
    DIM = 128
    NUM_ELEMENTS = 500_000
    MIN_CALL_SECONDS = 0.05

    @pytest.fixture(scope="class")
    def bf_index(self):
        rng = np.random.default_rng(1495)
        data = np.float32(rng.random((self.NUM_ELEMENTS, self.DIM)))
        bf = hnswlib.BFIndex(space="l2", dim=self.DIM)
        bf.init_index(max_elements=self.NUM_ELEMENTS)
        bf.add_items(data)
        return bf

    def test_bfindex_save_index_releases_gil_during_native_call(
        self, bf_index, tmp_path
    ):
        save_path = str(tmp_path / "bf_index.bin")

        def _do_save():
            bf_index.save_index(save_path)

        max_gap, call_duration = _max_recorder_gap(
            _do_save, min_call_seconds=self.MIN_CALL_SECONDS
        )
        assert Path(save_path).exists()
        assert Path(save_path).stat().st_size > 0
        _assert_gil_released(
            max_gap, call_duration, native_call_name="BFIndex.save_index()"
        )


@pytest.mark.slow
class TestBFIndexLoadIndexReleasesGIL:
    DIM = 128
    NUM_ELEMENTS = 500_000
    MIN_CALL_SECONDS = 0.05

    @pytest.fixture(scope="class")
    def saved_bf_index_path(self, tmp_path_factory):
        rng = np.random.default_rng(1496)
        data = np.float32(rng.random((self.NUM_ELEMENTS, self.DIM)))
        bf = hnswlib.BFIndex(space="l2", dim=self.DIM)
        bf.init_index(max_elements=self.NUM_ELEMENTS)
        bf.add_items(data)
        save_dir: Path = tmp_path_factory.mktemp("bf_load_1490")
        save_path = str(save_dir / "bf_index.bin")
        bf.save_index(save_path)
        return save_path

    def test_bfindex_load_index_releases_gil_during_native_call(
        self, saved_bf_index_path
    ):
        result = {}

        def _do_load():
            bf2 = hnswlib.BFIndex(space="l2", dim=self.DIM)
            bf2.load_index(saved_bf_index_path, max_elements=self.NUM_ELEMENTS)
            result["index"] = bf2

        max_gap, call_duration = _max_recorder_gap(
            _do_load, min_call_seconds=self.MIN_CALL_SECONDS
        )
        assert result["index"] is not None
        _assert_gil_released(
            max_gap, call_duration, native_call_name="BFIndex.load_index()"
        )


# ---------------------------------------------------------------------------
# mark_deleted(): structural verification, not wall-clock timing (see the
# module docstring for why a concurrent-thread measurement is not
# physically meaningful for this O(1) binding).
# ---------------------------------------------------------------------------


class TestMarkDeletedGuardApplied:
    def test_mark_deleted_binding_has_gil_release_guard_in_source(self):
        source = BINDINGS_CPP_PATH.read_text()
        def_marker = '.def("mark_deleted"'
        assert def_marker in source, (
            "mark_deleted binding definition not found in "
            f"{BINDINGS_CPP_PATH} -- has it been renamed/removed?"
        )
        start = source.index(def_marker)
        next_def = source.index(".def(", start + len(def_marker))
        segment = source[start:next_def]
        assert "py::call_guard<py::gil_scoped_release>()" in segment, (
            "mark_deleted binding is missing "
            "py::call_guard<py::gil_scoped_release>() in "
            f"{BINDINGS_CPP_PATH} (Story #1490)"
        )


# ---------------------------------------------------------------------------
# Correctness proofs (deterministic, no GIL timing involved)
# ---------------------------------------------------------------------------


class TestBindingsCorrectnessUnaffectedByGuard:
    """Proves the 9 guarded bindings still behave correctly -- releasing
    the GIL around a native call must never change its observable result.
    """

    DIM = 128

    def test_check_integrity_reports_no_orphans_after_repair(self):
        index, _data = _build_hnsw_index(
            50_000, dim=self.DIM, m=8, ef_construction=40, seed=1
        )
        before = index.check_integrity()
        # A low-M/ef graph on random data reliably produces real orphans.
        assert before["valid"] is False
        assert before["errors"]

        repair_result = index.repair_orphans()
        assert repair_result["orphans_after"] == 0
        assert repair_result["valid"] is True

        after = index.check_integrity()
        assert after["valid"] is True
        assert after["errors"] == []

    def test_repair_orphans_is_idempotent_on_already_clean_index(self):
        index, _data = _build_hnsw_index(
            20_000, dim=self.DIM, m=8, ef_construction=40, seed=2
        )
        index.repair_orphans()  # first pass: repair any real orphans

        # Second call on an already-repaired (clean) index must report zero
        # additional repair work and remain valid.
        second = index.repair_orphans()
        assert second["orphans_before"] == 0
        assert second["orphans_after"] == 0
        assert second["repaired_count"] == 0
        assert second["valid"] is True

    def test_init_index_add_items_knn_query_returns_correct_neighbor(self):
        index = hnswlib.Index(space="l2", dim=self.DIM)
        index.init_index(max_elements=1_000, M=16, ef_construction=200)
        index.set_num_threads(4)

        rng = np.random.default_rng(3)
        data = rng.random((1_000, self.DIM)).astype(np.float32)
        ids = np.arange(1_000)
        index.add_items(data, ids)
        index.set_ef(100)

        query = data[42:43]
        labels, distances = index.knn_query(query, k=1)
        assert labels[0][0] == 42
        assert distances[0][0] == pytest.approx(0.0, abs=1e-4)

    def test_get_items_returns_correct_vectors_for_requested_ids(self):
        index = hnswlib.Index(space="l2", dim=self.DIM)
        index.init_index(max_elements=100, M=4, ef_construction=10)
        rng = np.random.default_rng(4)
        data = rng.random((100, self.DIM)).astype(np.float32)
        index.add_items(data)

        requested = [5, 5, 10, 0]
        retrieved = np.array(index.get_items(requested))
        for i, label in enumerate(requested):
            assert np.allclose(retrieved[i], data[label], atol=1e-5)

    def test_get_ids_list_matches_added_ids(self):
        index = hnswlib.Index(space="l2", dim=self.DIM)
        index.init_index(max_elements=100, M=4, ef_construction=10)
        rng = np.random.default_rng(5)
        data = np.float32(rng.random((100, self.DIM)))
        ids = np.arange(100)
        index.add_items(data, ids)

        assert sorted(index.get_ids_list()) == list(range(100))

    def test_mark_deleted_excludes_point_from_knn_results(self):
        index = hnswlib.Index(space="l2", dim=self.DIM)
        index.init_index(max_elements=100, M=16, ef_construction=200)
        rng = np.random.default_rng(6)
        data = rng.random((100, self.DIM)).astype(np.float32)
        ids = np.arange(100)
        index.add_items(data, ids)
        index.set_ef(100)

        index.mark_deleted(7)
        labels, _distances = index.knn_query(data[7:8], k=1)
        assert labels[0][0] != 7

    def test_resize_index_allows_adding_beyond_original_capacity(self):
        index = hnswlib.Index(space="l2", dim=self.DIM)
        index.init_index(max_elements=10, M=4, ef_construction=10)
        rng = np.random.default_rng(7)
        data = np.float32(rng.random((10, self.DIM)))
        index.add_items(data, np.arange(10))

        index.resize_index(20)
        more_data = np.float32(rng.random((10, self.DIM)))
        index.add_items(more_data, np.arange(10, 20))

        assert index.get_current_count() == 20

    def test_bfindex_save_load_roundtrip_returns_identical_results(self, tmp_path):
        rng = np.random.default_rng(8)
        data = rng.random((500, self.DIM)).astype(np.float32)
        ids = np.arange(500)

        bf = hnswlib.BFIndex(space="l2", dim=self.DIM)
        bf.init_index(max_elements=500)
        bf.add_items(data, ids)

        query = data[10:11]
        labels_before, distances_before = bf.knn_query(query, k=5)

        save_path = str(tmp_path / "bf_roundtrip.bin")
        bf.save_index(save_path)

        bf2 = hnswlib.BFIndex(space="l2", dim=self.DIM)
        bf2.load_index(save_path, max_elements=500)
        labels_after, distances_after = bf2.knn_query(query, k=5)

        assert labels_before.tolist() == labels_after.tolist()
        assert np.allclose(distances_before, distances_after)
