"""Bug #1825 regression coverage: the leaked-TemporalWatchHandler-polling-
thread guard.

Root cause (confirmed via direct source review, cross-checked with live
stack-trace instrumentation): `TemporalWatchHandler._start_polling_thread()`
used to run `while True: time.sleep(5); ...subprocess.run(["git",
"rev-parse", "HEAD"], ...)...` with no stop mechanism anywhere in the class.
Because pytest runs every test in `tests/unit/` in one shared process, a test
that triggered the polling fallback (even incidentally, as four tests in
`tests/unit/cli/test_temporal_watch_handler.py` did) left that thread running
for the rest of the pytest session, periodically spawning a real `git
rev-parse HEAD` subprocess call. When one of those ticks landed inside an
unrelated test's narrow `with patch(subprocess.run)` counting window
(`tests/unit/services/test_reconcile_batch_content_id_1505.py`), it inflated
that test's exact subprocess-call-count assertion by one or two -- this is
the actual root cause of Bug #1825's intermittent full-suite-only failures.

`tests/unit/conftest.py` installs an autouse guard fixture
(`_guard_no_leaked_temporal_watch_polling_thread_1825`) that snapshots
`threading.enumerate()` before each test and, after it, fails loudly if any
NEW thread matching the polling thread's stable name PREFIX (real threads
carry a per-instance suffix -- see `_POLLING_THREAD_NAME` usage in
`_start_polling_thread`) is still alive -- bounding detection to the exact
test that introduced the leak instead of letting it silently corrupt an
unrelated test far later in the run.

These tests cover the pure-logic helper
(`_leaked_temporal_watch_polling_threads`) that fixture is built on, without
needing to drive pytest's own fixture machinery recursively.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from code_indexer.cli_temporal_watch_handler import _POLLING_THREAD_NAME
from tests.unit.conftest import _leaked_temporal_watch_polling_threads

_THREAD_JOIN_TIMEOUT_SECONDS = 5


@contextmanager
def _named_thread(name: str) -> Iterator[threading.Thread]:
    """Start a real, alive, named daemon thread and guarantee it is joined
    (never leaked into the rest of THIS guard test's own suite run) on
    exit, success or failure alike."""
    stop_event = threading.Event()
    thread = threading.Thread(target=stop_event.wait, name=name, daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        stop_event.set()
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)


def test_leaked_temporal_watch_polling_threads_detects_newly_started_thread() -> None:
    pre_existing_ids = {id(t) for t in threading.enumerate()}
    # Real polling threads carry a per-instance suffix (they are not named
    # with the bare prefix) -- exercising that here proves the helper does
    # PREFIX matching, not exact-name equality.
    with _named_thread(f"{_POLLING_THREAD_NAME}-42") as thread:
        leaked = _leaked_temporal_watch_polling_threads(pre_existing_ids)
        assert leaked == [thread], (
            "a newly-started thread whose name starts with the polling "
            "thread prefix, not present in the pre-existing snapshot, "
            "must be reported as leaked"
        )


def test_leaked_temporal_watch_polling_threads_ignores_pre_existing_thread() -> None:
    with _named_thread(_POLLING_THREAD_NAME):
        pre_existing_ids = {id(t) for t in threading.enumerate()}
        leaked = _leaked_temporal_watch_polling_threads(pre_existing_ids)
        assert leaked == [], (
            "a matching thread that was already running BEFORE the test "
            "started must not be reported as leaked -- only threads newly "
            "started during the test are the test's own responsibility"
        )


def test_leaked_temporal_watch_polling_threads_ignores_unrelated_thread_name() -> None:
    pre_existing_ids = {id(t) for t in threading.enumerate()}
    with _named_thread("some-unrelated-background-thread"):
        leaked = _leaked_temporal_watch_polling_threads(pre_existing_ids)
        assert leaked == [], (
            "only threads named after TemporalWatchHandler's polling "
            "thread prefix are this guard's concern -- an unrelated "
            "background thread started by a test is not this bug's shape"
        )
