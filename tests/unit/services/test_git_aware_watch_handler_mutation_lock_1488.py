"""GitAwareWatchHandler per-cycle mutation-lock injection (Codex Finding, Story #1488).

Finding 2 (16th Codex review): the daemon's watch-mode ONGOING per-event
re-index cycles (GitAwareWatchHandler._process_pending_changes -> ...
smart_indexer.process_files_incrementally) run entirely OUTSIDE the daemon's
`mutation_lock` -- only the watch START boundary (exposed_watch_start) is
covered. A watch re-index cycle can therefore race a manual daemon `index`
(or clean_data) on the same collection.

The fix threads an OPTIONAL mutation lock into GitAwareWatchHandler so each
per-event mutation cycle acquires it (default None -- standalone non-daemon
`cidx watch` is byte-identical/unaffected).

These tests prove mutual exclusion deterministically using threading.Event
gates (no wall-clock ordering sleeps): a handler constructed WITH an injected
real threading.RLock must hold that lock for the duration of the mutation
cycle (a concurrent non-blocking acquire from another thread must fail while
the cycle is in flight, and must succeed once it completes); a handler
constructed WITHOUT a lock (None) must run the cycle unaffected.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.slow


class _FakeStats:
    files_processed = 1


def _make_handler(smart_indexer, mutation_lock=None):
    """Build a real GitAwareWatchHandler with only non-core dependencies stubbed.

    `config` and `git_topology_service` are unrelated to the mutation-lock
    behavior under test, so they are MagicMocks (mirroring the existing
    precedent in test_git_aware_watch_handler_tmp_filter.py). `watch_metadata`
    is the REAL dataclass -- it is cheap, side-effect-free, and exercising it
    for real avoids yet another mock.
    """
    from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
    from code_indexer.services.watch_metadata import WatchMetadata

    config = MagicMock()
    config.codebase_dir = Path("/fake/repo")
    config.file_extensions = ["py"]

    git_topology = MagicMock()

    watch_metadata = WatchMetadata()

    handler = GitAwareWatchHandler(
        config=config,
        smart_indexer=smart_indexer,
        git_topology_service=git_topology,
        watch_metadata=watch_metadata,
        debounce_seconds=0.1,
        mutation_lock=mutation_lock,
    )
    return handler


def test_process_pending_changes_holds_injected_mutation_lock_across_cycle():
    """A concurrent thread must NOT acquire the SAME injected mutation lock
    while a per-event mutation cycle (_process_pending_changes) is running."""
    lock = threading.RLock()
    entered = threading.Event()
    release_gate = threading.Event()

    class _BlockingSmartIndexer:
        def process_files_incrementally(self, *args, **kwargs):
            entered.set()
            # Bounded wait: released the instant the assertion below runs.
            release_gate.wait(timeout=5)
            return _FakeStats()

    handler = _make_handler(_BlockingSmartIndexer(), mutation_lock=lock)
    handler.pending_changes.add(handler.config.codebase_dir / "some_file.py")

    t = threading.Thread(target=handler._process_pending_changes)
    t.start()
    try:
        assert entered.wait(timeout=2), "mutation cycle never started"
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        assert not acquired, (
            "a concurrent thread acquired the injected mutation lock while a "
            "per-event mutation cycle was in progress -- lock not actually held"
        )
    finally:
        release_gate.set()
        t.join(timeout=5)

    assert not t.is_alive(), "mutation cycle thread hung"
    # Lock must be released and re-acquirable after the cycle completes.
    reacquired = lock.acquire(blocking=False)
    assert reacquired, "mutation lock was not released after the cycle completed"
    lock.release()


def test_process_pending_changes_without_injected_lock_is_unaffected():
    """A handler constructed WITHOUT an injected lock (standalone `cidx watch`)
    must run its mutation cycle unaffected -- byte-identical to before."""
    entered = threading.Event()

    class _ImmediateSmartIndexer:
        def process_files_incrementally(self, *args, **kwargs):
            entered.set()
            return _FakeStats()

    handler = _make_handler(_ImmediateSmartIndexer(), mutation_lock=None)
    handler.pending_changes.add(handler.config.codebase_dir / "some_file.py")

    handler._process_pending_changes()

    assert entered.is_set(), "mutation cycle did not run for the no-lock handler"
    assert handler.files_processed_count == 1
