"""
Regression test for Bug #1799: TantivyIndexManager._commit_inner re-acquires
an IndexWriter while the previous writer object is still referenced from
Python, so Tantivy's directory lockfile is not deterministically released
before the new writer is created.

Root cause (from the bug report):

    assert self._writer is not None
    writer = self._writer
    writer.commit()
    writer.wait_merging_threads()
    assert self._index is not None
    self._writer = self._index.writer(self._heap_size)

At the moment the new writer is requested (the RHS of the assignment, which
always runs *before* the assignment itself), TWO live Python references to
the OLD writer still exist: the local `writer` variable (still in scope
until the function returns) and the `self._writer` attribute (not yet
reassigned). Tantivy only releases the directory lockfile when the
underlying Rust IndexWriter is dropped, which for a PyO3-wrapped object only
happens once its CPython refcount reaches zero. The fix drops both
references (`self._writer = None; del writer`) BEFORE requesting the
replacement writer.

Bug #1848 (round 2 of this file): the previous version of this test observed
the fix by racing a background poller thread against commit(), sampling the
plain `manager._writer` attribute and hoping to catch it in the `None` state
during the (very short) re-acquisition window -- with `sys.setswitchinterval`
tightened and up to 5 retry cycles to compensate for scheduler luck. That is
inherently probabilistic: verified live under real contention (12 CPU
busy-loop workers on a 12-core box, load average 9.6-11.7), the poller
simply failed to get scheduled during the window on 1 of 10 runs, producing
a false negative on the FIXED implementation. Piling on more retry cycles
only pushed per-run time past the 5s target without fixing the underlying
non-determinism.

This version observes the same real event without racing anything. The
only statement that executes inside the None-window is
`self._index.writer(self._heap_size)` (see `_commit_inner`). This test
installs a thin spy in place of `manager._index` that delegates every
attribute access to the REAL tantivy Index object except `.writer()`, which
it intercepts to record `manager._writer` at the exact instant the real
call happens, then forwards the call and returns the real writer
unchanged. This is a spy over a real object that still does the real work
-- not a mock standing in for behaviour, so it does not conflict with the
project's anti-mock rule: the actual Tantivy writer is still created by the
actual Tantivy library, the actual `commit()`/`_commit_inner()` code path
runs completely unmodified, and only an already-existing attribute read is
observed at the one instant that matters.

On the FIXED implementation, the recorded value at that instant is `None`
(the fix cleared it first). On the UNFIXED implementation, the recorded
value is the OLD WRITER OBJECT (the buggy code reassigns `self._writer`
directly from old to new with no intermediate `None`). Either way the
observation happens exactly once, synchronously, in-process -- no
sampling, no GIL timing, no scheduler luck, so it cannot be missed.

commit() still runs on a bounded worker thread joined with a timeout,
purely as a hang guard (a genuine hang inside commit() must fail the test
with an explicit assertion instead of hanging the test process forever);
that thread plays no role in the observation itself, which is captured
synchronously by the spy regardless of which thread invokes it.

Note on `Any` usage below: `tantivy-py` is a PyO3 extension module with no
published type stubs, so its `Index`/`IndexWriter` objects have no static
type available to reference honestly -- `Any` documents that reality rather
than fabricating a `Protocol` that promises more structure than the real
dynamic API provides. This is confined to the spy's plumbing (forwarding
attribute access to the real object); the test's own assertions are
plainly typed (`None` vs. "some other object").
"""

import tempfile
import threading
from pathlib import Path
from typing import Any, List, cast

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager

_COMMIT_TIMEOUT_SECONDS = 10.0
_COMMITTER_JOIN_TIMEOUT_SECONDS = 1.0


@pytest.fixture
def temp_index_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "tantivy_index"


@pytest.fixture
def tantivy_manager(temp_index_dir):
    manager = TantivyIndexManager(temp_index_dir)
    manager.initialize_index(create_new=True)
    yield manager
    manager.close()


def _make_doc(i: int) -> dict:
    return {
        "path": f"file_{i}.py",
        "content": f"def func_{i}(): return {i}",
        "content_raw": f"def func_{i}(): return {i}",
        "identifiers": [f"func_{i}"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    }


class _WriterAcquisitionSpy:
    """Delegates every call to the REAL tantivy Index except .writer(),
    which it intercepts to record manager._writer at the exact instant the
    real .writer() is invoked, then forwards to the real call and returns
    the real writer unchanged.

    This is a spy over a real object that still performs the real work --
    not a mock standing in for behaviour. commit() and Tantivy's own
    writer-acquisition logic run completely unmodified; only the
    already-existing `manager._writer` attribute is observed at the one
    instant that matters.

    `real_index` and the `Any`-typed members below are `tantivy.Index` /
    `tantivy.IndexWriter` PyO3 objects, which ship no type stubs -- `Any`
    is the honest type here, not a shortcut around a real static type that
    was skipped.
    """

    def __init__(self, real_index: Any, manager: TantivyIndexManager) -> None:
        self._real_index = real_index
        self._manager = manager
        self.writer_state_at_reacquisition: Any = "NOT_CALLED"

    def writer(self, heap_size: int) -> Any:
        self.writer_state_at_reacquisition = self._manager._writer
        return self._real_index.writer(heap_size)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_index, name)


def _run_commit_with_spy(
    tantivy_manager: TantivyIndexManager,
) -> "_WriterAcquisitionSpy":
    """Install the spy in place of manager._index, run commit() on a bounded
    worker thread (hang guard only -- the observation itself is synchronous
    within that thread, not raced against anything), then restore the real
    index.

    Returns the spy so the caller can inspect what it recorded. Raises
    whatever commit() raised, or fails the test explicitly if it hung.
    """
    real_index = tantivy_manager._index
    spy = _WriterAcquisitionSpy(real_index, tantivy_manager)
    # manager._index is statically typed Optional[Index]; substituting a
    # duck-typed spy here is the deliberate mechanism this test relies on
    # (see _WriterAcquisitionSpy docstring), so the cast documents an
    # intentional type substitution rather than masking a real bug.
    tantivy_manager._index = cast(Any, spy)

    commit_finished = threading.Event()
    commit_exceptions: List[Exception] = []

    def run_commit() -> None:
        try:
            tantivy_manager.commit()
        except Exception as exc:  # noqa: BLE001 -- surfaced explicitly by caller
            commit_exceptions.append(exc)
        finally:
            commit_finished.set()

    committer = threading.Thread(target=run_commit, daemon=True)
    try:
        committer.start()
        commit_completed = commit_finished.wait(timeout=_COMMIT_TIMEOUT_SECONDS)
        committer.join(timeout=_COMMITTER_JOIN_TIMEOUT_SECONDS)

        assert commit_completed, (
            f"commit() did not complete within {_COMMIT_TIMEOUT_SECONDS}s"
        )
        assert not committer.is_alive(), (
            "Committer thread did not terminate within its join timeout"
        )
    finally:
        tantivy_manager._index = real_index

    if commit_exceptions:
        raise commit_exceptions[0]

    return spy


class TestBug1799WriterReferenceReleasedBeforeReacquire:
    """Discriminating RED test: must fail against the unpatched implementation."""

    def test_old_writer_reference_is_dropped_before_new_writer_is_acquired(
        self, tantivy_manager
    ):
        """
        GIVEN a manager with a pending document and a live writer
        WHEN commit() re-acquires a replacement writer
        THEN the writer state recorded at the exact instant the real
             tantivy Index.writer() call happens must be None -- proving
             neither a lingering local variable nor a not-yet-reassigned
             attribute keeps the old writer (and its Tantivy directory
             lock) alive during re-acquisition (see module docstring for
             the full mechanism).
        """
        tantivy_manager.add_document(_make_doc(0))

        spy = _run_commit_with_spy(tantivy_manager)

        assert spy.writer_state_at_reacquisition is None, (
            "self._writer was not None at the instant self._index.writer() "
            "was called to acquire the replacement writer. This means the "
            "old writer's Python references (the local `writer` variable "
            "and/or the `self._writer` attribute) were never cleared "
            "before requesting a replacement writer -- the exact Bug "
            "#1799 race that delays Tantivy's directory lockfile release "
            "and can surface as intermittent LockBusy under production "
            "load."
        )

        # The manager must remain fully functional afterward -- this is not
        # a vacuous/instrumentation-only assertion.
        tantivy_manager.add_document(_make_doc(1))
        tantivy_manager.commit()
        results = tantivy_manager.search("func_1", limit=10)
        assert len(results) > 0, (
            "Manager must remain usable for add/commit/search after the "
            "observed commit() call"
        )
