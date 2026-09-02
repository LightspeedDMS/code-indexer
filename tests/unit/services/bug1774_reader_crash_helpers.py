"""Shared helpers/constants for the Bug #1774 (indexing lockup) test
suite, split across:

  - test_progress_subprocess_runner_reader_crash_1774_root_cause.py
  - test_progress_subprocess_runner_reader_crash_1774_reader_loops.py
  - test_progress_subprocess_runner_reader_crash_1774_e2e.py

See the root_cause module's docstring for the full bug/fix narrative.
Not collected by pytest itself (does not match `test_*.py`, per
`pyproject.toml`'s `python_files` setting) -- mirrors the existing
`incremental_filter_helpers.py` convention in this same directory.

Every fd primitive here uses only real OS calls -- no mocking of the
deadlock mechanism, no monkeypatching of any stdlib global or SUT
internal. `fcntl.F_DUPFD` (never `os.dup2` onto a hardcoded number)
obtains every "high fd" so no already-open descriptor is ever clobbered.
"""

import contextlib
import errno
import fcntl
import os
import threading
from typing import List, Optional

# Comfortably above glibc's hard FD_SETSIZE=1024 ceiling -- the fd class
# that broke select.select() (Bug #1774 root cause). Passed to
# fcntl.F_DUPFD as the minimum target; the OS returns the lowest
# actually-free fd >= this, so it never collides with an already-open
# descriptor.
FD_BEYOND_SELECT_CEILING = 1024

THREAD_JOIN_TIMEOUT_SECONDS = 5.0
SELECTOR_WAIT_TIMEOUT_SECONDS = 5.0

# Time to let a reader thread settle into its first blocking sel.select()
# call before closing fds out from under it (code review finding A repro).
CLOSED_FD_RACE_SETTLE_SECONDS = 0.2

# Messi Rule #14 (anti-unbounded-loop) bound for _drain_queue below.
_DRAIN_QUEUE_MAX_ITEMS = 10_000


def close_quietly(fd: int) -> None:
    """Close an fd, tolerating only EBADF (already closed) -- any other
    OSError is a genuine unexpected failure and must propagate.
    """
    try:
        os.close(fd)
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise


def high_unused_fd() -> int:
    """A fresh, real, currently-CLOSED fd >= FD_BEYOND_SELECT_CEILING via
    `fcntl.F_DUPFD`. The source pipe is fully closed (both ends) even if
    `fcntl.fcntl()` itself raises; the duplicate is then closed too
    before returning the now-invalid number.
    """
    probe_r, probe_w = os.pipe()
    try:
        fd = fcntl.fcntl(probe_r, fcntl.F_DUPFD, FD_BEYOND_SELECT_CEILING)
    finally:
        close_quietly(probe_r)
        close_quietly(probe_w)
    close_quietly(fd)
    return fd


@contextlib.contextmanager
def high_fd_pipe():
    """Open a real pipe; duplicate its READ end to a fresh fd number >=
    FD_BEYOND_SELECT_CEILING via `fcntl.F_DUPFD`, keeping the original
    WRITE end open. Yields (high_read_fd, write_fd); guarantees cleanup
    including partial setup failure.
    """
    read_fd: Optional[int]
    read_fd, write_fd = os.pipe()
    high_read_fd: Optional[int] = None
    try:
        high_read_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD, FD_BEYOND_SELECT_CEILING)
        close_quietly(read_fd)
        read_fd = None
        yield high_read_fd, write_fd
    finally:
        if read_fd is not None:
            close_quietly(read_fd)
        if high_read_fd is not None:
            close_quietly(high_read_fd)
        close_quietly(write_fd)


def run_stdout_reader_loop(fd: int) -> tuple:
    """Start `_stdout_reader_loop` against `fd` on a background thread.
    Returns (thread, line_queue, reader_failed, shutdown_r, shutdown_w).
    Imports inside the function so an ImportError on unfixed code is a
    normal test failure, not a whole-file collection error.
    """
    import queue as queue_module

    from code_indexer.services.progress_subprocess_runner import (
        _stdout_reader_loop,
    )

    shutdown_r, shutdown_w = os.pipe()
    line_queue: "queue_module.Queue" = queue_module.Queue()
    reader_failed = threading.Event()

    thread = threading.Thread(
        target=_stdout_reader_loop,
        args=(fd, shutdown_r, line_queue, "test", reader_failed),
        daemon=True,
    )
    thread.start()
    return thread, line_queue, reader_failed, shutdown_r, shutdown_w


def run_stderr_reader_loop(fd: int) -> tuple:
    """Mirrors run_stdout_reader_loop for `_stderr_reader_loop`. Returns
    (thread, stderr_lines, reader_failed, shutdown_r, shutdown_w).
    """
    from code_indexer.services.progress_subprocess_runner import (
        _stderr_reader_loop,
    )

    shutdown_r, shutdown_w = os.pipe()
    stderr_lines: List[str] = []
    reader_failed = threading.Event()

    thread = threading.Thread(
        target=_stderr_reader_loop,
        args=(fd, shutdown_r, stderr_lines, "test", reader_failed),
        daemon=True,
    )
    thread.start()
    return thread, stderr_lines, reader_failed, shutdown_r, shutdown_w


def drain_queue(line_queue, max_items: int = _DRAIN_QUEUE_MAX_ITEMS) -> list:
    """Drain a Queue via non-blocking get_nowait() up to `max_items`
    (Messi Rule #14 explicit bound) or until the sentinel (None) is
    reached, whichever comes first. Raises AssertionError -- a clear
    test failure, not a hang -- if the bound is hit without a sentinel.
    """
    received = []
    for _ in range(max_items):
        item = line_queue.get_nowait()
        received.append(item)
        if item is None:
            return received
    raise AssertionError(
        f"drained {max_items} items without reaching the sentinel -- "
        f"either the sentinel is genuinely missing or max_items is too low"
    )
