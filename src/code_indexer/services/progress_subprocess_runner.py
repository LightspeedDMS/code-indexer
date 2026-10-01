"""
Shared progress subprocess runner utilities.

Story #482: Extend Real-Time Progress Reporting to All User-Facing Indexing Paths.

Extracts run_with_popen_progress and gather_repo_metrics from golden_repo_manager.py
into a reusable shared module so all indexing paths (PATH A-E) can use them
without code duplication.

Usage::

    from code_indexer.services.progress_subprocess_runner import (
        run_with_popen_progress,
        gather_repo_metrics,
    )

    file_count, commit_count = gather_repo_metrics(repo_path)
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(["semantic", "fts"], file_count, commit_count)

    all_stdout: list[str] = []
    all_stderr: list[str] = []
    run_with_popen_progress(
        command=["cidx", "index", "--clear", "--progress-json"],
        phase_name="semantic",
        allocator=allocator,
        progress_callback=progress_callback,
        all_stdout=all_stdout,
        all_stderr=all_stderr,
        cwd=repo_path,
        # No timeout: Bug #1218 — only per-request outbound HTTP timeouts are allowed.
    )
"""

import io
import logging
import math
import os
import queue
import selectors
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from code_indexer.services.activity_heartbeat_writer import (
    ACTIVITY_HEARTBEAT_PATH_ENV,
)
from code_indexer.services.activity_watchdog import (
    check_and_terminate_if_stale,
    terminate_process_group,
)
from code_indexer.storage.hnsw_index_manager import HNSW_ORPHAN_REPAIR_MARKER

logger = logging.getLogger(__name__)

_HNSW_ORPHAN_MARKER_PREFIX = HNSW_ORPHAN_REPAIR_MARKER + ":"

#: Issue #1530: how often (seconds) the watchdog re-checks the heartbeat
#: file for staleness. Decoupled from the tighter stdout-drain cadence
#: used later in this module -- staleness only needs checking every
#: couple of seconds, not on every poll iteration.
_WATCHDOG_CHECK_INTERVAL_SECONDS = 2.0

#: Bug #1774: buffer size for the raw os.read() calls in the stdout/
#: stderr reader loops below. Promoted to module scope (previously a
#: local inside _run_with_popen_progress_impl) so those loops can be
#: extracted to module-level, independently unit-testable functions.
READ_BUFFER_SIZE = 4096

#: Bug #1774: how long the main loop sleeps between poll iterations while
#: waiting for more output or a watchdog check. Promoted to module scope
#: alongside READ_BUFFER_SIZE, for the same reason.
POLL_INTERVAL_SECONDS = 0.05


def _forward_hnsw_orphan_events(
    stderr_text: str, callback: Optional[Callable[[str], None]]
) -> None:
    """Bug #1388: scan a subprocess's captured stderr text for
    HNSW_ORPHAN_REPAIR_MARKER-prefixed lines and forward each one, verbatim,
    to `callback`. This is a channel entirely separate from the percentage
    `progress_callback`/`_emit` machinery below -- it is never subject to
    the monotonic high-water-mark suppression, so it survives even when the
    percentage channel would drop a same-moment event (see module/class
    docstrings on `_emit` and `HNSW_ORPHAN_REPAIR_MARKER` for the full
    rationale). A no-op when callback is None (default: most callers don't
    care about this event).
    """
    if callback is None:
        return
    for line in stderr_text.splitlines():
        if line.startswith(_HNSW_ORPHAN_MARKER_PREFIX):
            callback(line)


def _get_fd(stream) -> "Optional[int]":
    """Return the OS file descriptor for *stream*, or None if unavailable.

    Real subprocess PIPE streams always expose a valid fd via fileno().
    Mocked/StringIO streams raise io.UnsupportedOperation (or AttributeError /
    ValueError) — those callers get None and fall back to line-iteration.
    """
    if stream is None:
        return None
    try:
        fd = stream.fileno()
        # A non-negative integer means a real OS fd.
        return fd if isinstance(fd, int) and fd >= 0 else None
    except (io.UnsupportedOperation, AttributeError, ValueError):
        return None


class IndexingSubprocessError(Exception):
    """
    Raised by run_with_popen_progress when the subprocess exits non-zero.

    Callers that need a domain-specific error (e.g. GoldenRepoError,
    RuntimeError) should catch this and re-raise.  Using a local error type
    avoids importing from consumer modules (golden_repo_manager, etc.) which
    would create circular dependencies.
    """


class IndexingWatchdogKillError(IndexingSubprocessError):
    """Issue #1530: raised when the parent-side watchdog kills a subprocess
    for showing ZERO forward progress past `stale_activity_timeout_seconds`
    -- never for being merely slow (Bug #1218: total elapsed time alone
    never triggers this). A DISTINCT subclass of `IndexingSubprocessError`
    so callers/logs can tell "watchdog determined this was wedged and
    killed it" apart from "the subprocess exited non-zero on its own" --
    the watchdog-killed case should force index validation/rebuild on
    retry. Never caught as an ordinary failure; never auto-retried.
    """


# Timeout for quick git metadata commands (ls-files, rev-list --count)
GIT_COMMAND_TIMEOUT_SECONDS = 30


def gather_repo_metrics(repo_path) -> tuple:
    """
    Gather file count and commit count for a repository.

    Used by indexing paths to compute ProgressPhaseAllocator weights.
    Both commands are fast for most repos.

    Args:
        repo_path: Path to the git repository (str or Path)

    Returns:
        (file_count, commit_count) as integers.  Returns (0, 0) if repo is
        not a git repository or if git commands fail (graceful degradation).
    """
    # Check if this is actually a git repository (Bug #589: local:// repos have no .git)
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return (0, 0)

    repo_str = str(repo_path)

    # Count tracked files
    try:
        ls_result = subprocess.run(
            ["git", "-C", repo_str, "ls-files"],
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if ls_result.returncode == 0:
            file_count = len(
                [line for line in ls_result.stdout.splitlines() if line.strip()]
            )
        else:
            logger.warning(
                "gather_repo_metrics: git ls-files failed in %s (exit %d): %s",
                repo_str,
                ls_result.returncode,
                ls_result.stderr.strip(),
            )
            file_count = 0
    except Exception as e:
        logger.warning(
            "gather_repo_metrics: failed to count tracked files in %s: %s", repo_str, e
        )
        file_count = 0

    # Count commits on current branch
    try:
        rev_result = subprocess.run(
            ["git", "-C", repo_str, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if rev_result.returncode == 0:
            commit_count = int(rev_result.stdout.strip() or "0")
        else:
            logger.warning(
                "gather_repo_metrics: git rev-list failed in %s (exit %d): %s",
                repo_str,
                rev_result.returncode,
                rev_result.stderr.strip(),
            )
            commit_count = 0
    except Exception as e:
        logger.warning(
            "gather_repo_metrics: failed to count commits in %s: %s", repo_str, e
        )
        commit_count = 0

    return file_count, commit_count


def _validate_stale_activity_timeout(value: Optional[float]) -> None:
    """Issue #1530: the staleness threshold must be None (watchdog off) or a
    finite positive number. Rejected loudly up front, never coerced.
    """
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or math.isnan(value)
        or math.isinf(value)
        or value <= 0
    ):
        raise ValueError(
            f"stale_activity_timeout_seconds must be None or a finite "
            f"number > 0, got {value!r}"
        )


def _prepare_heartbeat_path(
    stale_activity_timeout_seconds: Optional[float], heartbeat_dir: Optional[str]
) -> Optional[str]:
    """Generate this invocation's unique node-local heartbeat path, or None
    when the watchdog is not armed (Issue #1530 design point 5: one file per
    subprocess invocation, generated by the node that will call Popen).
    """
    if stale_activity_timeout_seconds is None:
        return None
    if heartbeat_dir is not None and not os.path.isdir(heartbeat_dir):
        raise ValueError(
            f"heartbeat_dir must be an existing directory, got {heartbeat_dir!r}"
        )
    directory = heartbeat_dir if heartbeat_dir is not None else tempfile.gettempdir()
    return os.path.join(directory, f"cidx_activity_heartbeat_{uuid.uuid4().hex}.json")


def _terminate_and_delete_heartbeat(
    heartbeat_path: Optional[str], process_holder: List["subprocess.Popen"]
) -> None:
    """Issue #1530 design point 5: the watching node stops watching, so the
    child must stop too and the heartbeat file must go.

    Order matters: a still-alive child still has a heartbeat writer thread
    that would re-create the file right after an unlink, so its process
    group is terminated FIRST -- and `terminate_process_group` blocks until
    the child is reaped on every branch, so no writer of that child exists
    by the time the unlink runs.

    Called from a `finally`, so nothing here may propagate: the termination
    step catches broad `Exception` and logs a WARNING (a cleanup failure
    must never replace, and thus hide, the real exception being unwound),
    and the unlink -- which realistically only raises `OSError` -- logs at
    DEBUG because an already-absent file is the expected steady state.
    """
    if heartbeat_path is None:
        return
    assert isinstance(process_holder, list), (
        f"process_holder must be a list, got {type(process_holder).__name__}"
    )
    for process in process_holder:
        try:
            if process.poll() is None:
                terminate_process_group(process)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning(
                "run_with_popen_progress: could not terminate child "
                "pid=%s during cleanup: %s",
                process.pid,
                exc,
            )
    try:
        os.unlink(heartbeat_path)
    except OSError as exc:
        logger.debug(
            "run_with_popen_progress: could not remove heartbeat file %s: %s",
            heartbeat_path,
            exc,
        )


def _fd_is_open(fd: int) -> bool:
    """Return True if `fd` still refers to a genuinely open file
    description. Bug #1774 (code review finding): epoll silently drops a
    closed fd from its interest set instead of raising like
    select.select() used to -- this is how _read_available_bytes detects
    that a monitored fd was closed out from under it rather than
    blocking forever waiting on a fd that will never become ready again.
    """
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


# Bug #1774: shared selector-driven read loop used by both
# _stdout_reader_loop and _stderr_reader_loop below (previously two
# duplicated closures, each using select.select() with a narrow
# `except OSError`). select.select() has glibc's hard FD_SETSIZE=1024
# ceiling: a monitored fd >= 1024 raises ValueError -- NOT OSError --
# which used to escape that narrow handler entirely and silently kill
# the reader thread before it ever reached its sentinel/flush code.
# selectors.DefaultSelector (epoll-backed on Linux) has no such ceiling,
# closing the hole at its source. The broadened `except Exception` below
# additionally guarantees any OTHER internal failure is caught, logged
# with a full traceback, and recorded via `reader_failed` (set only on a
# genuine internal failure, never on a normal EOF/shutdown finish) so the
# caller can surface the distinction instead of an abnormal termination
# looking identical to clean success -- see _stdout_reader_loop/
# _stderr_reader_loop and the WARNING logged near their join() below.
#
# This project targets Linux-only server deployments (systemd units, see
# docs/server-deployment.md) -- selectors.DefaultSelector resolves to the
# epoll-backed selector there. It would fall back to a plain
# SelectSelector (same FD_SETSIZE ceiling this fix exists to remove) only
# on a platform lacking epoll/kqueue/poll, which this project does not
# target; not handled here as out of scope.
#
# Messi Rule #14 (anti-unbounded-loop) exception, documented per that
# rule's own "Event / Message Loop" carve-out: this is a blocking I/O
# consumer loop, not an optimistic/unproven loop. Its termination is NOT
# a static iteration count but IS a provable bound: `sel.select()` itself
# is now bounded by POLL_INTERVAL_SECONDS (code review finding -- an
# unbounded `sel.select()` could block forever if a monitored fd was
# closed out from under this thread, since epoll -- unlike select.select()
# -- gives no readiness event for that; the old select.select()-based code
# instead raised OSError immediately and was caught below), and each
# timeout tick re-checks two well-defined external signals: natural EOF
# on `data_fd` (the last write-end holder closed it) or an explicit byte
# actually OBSERVED as readable on `shutdown_r` (the main thread's
# shutdown hook writes it once the child has exited -- see the `finally`
# block in _run_with_popen_progress_impl, which unconditionally writes
# that byte on every exit path). Only those two OBSERVED events count as
# clean termination; a fd found closed via the liveness probe below
# WITHOUT having been observed that way first is treated as abnormal
# (logged + reader_failed) even though this loop still has to stop
# either way, because it can no longer prove the shutdown was genuinely
# intentional versus some fd having gone away for an unrelated reason.
#
# C2 fix (preserved): the data fd is checked before the shutdown fd so a
# same-cycle shutdown never drops trailing bytes.
def _read_available_bytes(
    data_fd: int,
    shutdown_r: int,
    error_label: str,
    stream_name: str,
    reader_failed: threading.Event,
):
    """Yield raw byte chunks from `data_fd` until natural EOF or a
    shutdown signal on `shutdown_r`. On any exception, log it (with a
    full traceback), set `reader_failed`, and stop -- the caller's
    post-loop code (sentinel/flush) still always runs, since it lives
    outside this generator's own try/except.
    """
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(data_fd, selectors.EVENT_READ)
            sel.register(shutdown_r, selectors.EVENT_READ)
            while True:
                ready_fds = {
                    key.fd for key, _ in sel.select(timeout=POLL_INTERVAL_SECONDS)
                }
                if data_fd in ready_fds:
                    chunk = os.read(data_fd, READ_BUFFER_SIZE)
                    if not chunk:
                        return  # EOF: all write-end holders closed their copy.
                    yield chunk
                    continue  # re-select; drain before honouring shutdown
                if shutdown_r in ready_fds:
                    return  # Shutdown signalled — data fd not ready, safe to stop.
                if ready_fds:
                    continue
                stale_data_fd = not _fd_is_open(data_fd)
                stale_shutdown_r = not _fd_is_open(shutdown_r)
                if stale_data_fd or stale_shutdown_r:
                    # Bug #1774 (code review finding): a fd closed WITHOUT
                    # ever being observed as readable first (normal EOF on
                    # data_fd, or the shutdown byte actually seen on
                    # shutdown_r) is NOT treated as clean termination --
                    # it cannot be proven this was the genuine, intentional
                    # shutdown sequence versus some fd having gone away for
                    # an unrelated reason. Stopping is still correct
                    # (polling a fd that will never become ready again is
                    # pointless), but it is flagged as abnormal so this is
                    # diagnosable rather than silently indistinguishable
                    # from success.
                    logger.warning(
                        "run_with_popen_progress: %s reader thread for %s "
                        "observed %s closed without a corresponding "
                        "EOF/shutdown-readable event -- stopping and "
                        "flagging as abnormal (fd=%d)",
                        stream_name,
                        error_label,
                        "data_fd" if stale_data_fd else "shutdown_r",
                        data_fd,
                    )
                    reader_failed.set()
                    return
    except Exception:  # noqa: BLE001 - Bug #1774: widened from OSError
        logger.exception(
            "run_with_popen_progress: %s reader thread failed unexpectedly "
            "for %s (fd=%d) -- reader exiting; partial data still flushed",
            stream_name,
            error_label,
            data_fd,
        )
        reader_failed.set()
        return


# Bug #1774: extracted to module scope (previously a closure nested
# inside _run_with_popen_progress_impl) so it can be unit-tested
# directly against a crafted fd. Always puts None as a sentinel when
# done -- including when _read_available_bytes was interrupted by an
# unexpected exception, or when PROCESSING a chunk it yielded raises
# (decode/split/put) -- so the main loop can always detect reader
# completion without polling thread liveness. Every step of the final
# flush/sentinel sequence is individually try/except-protected so a
# failure in one (e.g. the buffer flush) can never suppress an attempt
# at the other (the sentinel push), and each failure is itself logged
# and flagged via `reader_failed`.
def _stdout_reader_loop(
    stdout_fd: int,
    shutdown_r: int,
    line_queue: "queue.Queue[Optional[str]]",
    error_label: str,
    reader_failed: threading.Event,
) -> None:
    """Read stdout_fd via _read_available_bytes; put decoded lines on
    line_queue.
    """
    buf = b""
    try:
        for chunk in _read_available_bytes(
            stdout_fd, shutdown_r, error_label, "stdout", reader_failed
        ):
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line_queue.put(raw.decode("utf-8", errors="replace") + "\n")
    except Exception:  # noqa: BLE001 - Bug #1774: caller-side processing too
        logger.exception(
            "run_with_popen_progress: stdout reader thread failed "
            "unexpectedly for %s while processing captured data",
            error_label,
        )
        reader_failed.set()
    finally:
        try:
            # Flush any partial line remaining in the buffer.
            if buf:
                line_queue.put(buf.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - Bug #1774
            logger.exception(
                "run_with_popen_progress: stdout reader thread failed "
                "unexpectedly for %s while flushing the final partial line",
                error_label,
            )
            reader_failed.set()
        try:
            # Sentinel: signals main loop that no more lines are coming.
            # Attempted independently of the flush above -- one failing
            # must never stop this from being attempted too.
            line_queue.put(None)
        except Exception:  # noqa: BLE001 - Bug #1774
            logger.exception(
                "run_with_popen_progress: stdout reader thread failed "
                "unexpectedly for %s while pushing the completion "
                "sentinel -- the main loop's own reader-thread liveness "
                "check is the only remaining signal this reader is done",
                error_label,
            )
            reader_failed.set()


# Bug #1774: mirrors _stdout_reader_loop, adapted for stderr's simpler
# "accumulate raw text" contract (no line-splitting, no progress parsing,
# no sentinel -- stderr completion is detected via thread join, not a
# queue sentinel).
def _stderr_reader_loop(
    stderr_fd: int,
    shutdown_r: int,
    stderr_lines: List[str],
    error_label: str,
    reader_failed: threading.Event,
) -> None:
    """Read stderr_fd via _read_available_bytes; accumulate in stderr_lines."""
    if stderr_fd < 0:
        return
    buf = b""
    try:
        for chunk in _read_available_bytes(
            stderr_fd, shutdown_r, error_label, "stderr", reader_failed
        ):
            buf += chunk
    except Exception:  # noqa: BLE001 - Bug #1774: caller-side processing too
        logger.exception(
            "run_with_popen_progress: stderr reader thread failed "
            "unexpectedly for %s while processing captured data",
            error_label,
        )
        reader_failed.set()
    finally:
        try:
            if buf:
                stderr_lines.append(buf.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - Bug #1774
            logger.exception(
                "run_with_popen_progress: stderr reader thread failed "
                "unexpectedly for %s while flushing captured stderr",
                error_label,
            )
            reader_failed.set()


def run_with_popen_progress(
    command: List[str],
    phase_name: str,
    allocator,
    progress_callback: Optional[Callable],
    all_stdout: List[str],
    all_stderr: List[str],
    cwd: Optional[str],
    error_label: Optional[str] = None,
    last_reported: Optional[int] = None,
    env: Optional[dict] = None,
    orphan_event_callback: Optional[Callable[[str], None]] = None,
    stale_activity_timeout_seconds: Optional[float] = None,
    heartbeat_dir: Optional[str] = None,
) -> int:
    """Arm the Issue #1530 watchdog (when requested), then run the command.

    This frame is the ONLY generator of a heartbeat path, and it removes
    that file in a `finally` -- so cleanup covers every exit path, not just
    the two success/failure returns (an exception escaping the progress
    loop leaked one file per invocation before). With
    `stale_activity_timeout_seconds=None` (the default) nothing here has
    any effect and behavior is byte-identical for existing callers.

    See `_run_with_popen_progress_impl` for the progress/error semantics
    and the return value, which is passed straight through.
    """
    _validate_stale_activity_timeout(stale_activity_timeout_seconds)
    heartbeat_path = _prepare_heartbeat_path(
        stale_activity_timeout_seconds, heartbeat_dir
    )
    if heartbeat_path is not None:
        # Copy-never-mutate idiom (matches temporal_child_wiring.py's
        # build_temporal_child_env): never touch os.environ in place, and
        # never clobber what the caller already put in `env`.
        env = dict(env) if env is not None else dict(os.environ)
        env[ACTIVITY_HEARTBEAT_PATH_ENV] = heartbeat_path

    # Filled by the impl right after Popen so this frame can stop a
    # still-running child before removing its heartbeat file.
    process_holder: List["subprocess.Popen"] = []
    try:
        return _run_with_popen_progress_impl(
            command=command,
            phase_name=phase_name,
            allocator=allocator,
            progress_callback=progress_callback,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=cwd,
            error_label=error_label,
            last_reported=last_reported,
            env=env,
            orphan_event_callback=orphan_event_callback,
            stale_activity_timeout_seconds=stale_activity_timeout_seconds,
            heartbeat_path=heartbeat_path,
            process_holder=process_holder,
        )
    finally:
        _terminate_and_delete_heartbeat(heartbeat_path, process_holder)


def _run_with_popen_progress_impl(
    command: List[str],
    phase_name: str,
    allocator,
    progress_callback: Optional[Callable],
    all_stdout: List[str],
    all_stderr: List[str],
    cwd: Optional[str],
    error_label: Optional[str] = None,
    last_reported: Optional[int] = None,
    env: Optional[dict] = None,
    orphan_event_callback: Optional[Callable[[str], None]] = None,
    stale_activity_timeout_seconds: Optional[float] = None,
    heartbeat_path: Optional[str] = None,
    process_holder: Optional[List["subprocess.Popen"]] = None,
) -> int:
    """
    Run a command with Popen, reading JSON progress lines from stdout.

    JSON progress lines ({"current": N, "total": M, "info": "..."}) are parsed
    and forwarded to progress_callback as globally-mapped phase percentages via
    the allocator.  Non-JSON lines are accumulated in all_stdout for error
    reporting but not parsed.  Stderr is captured for error details.

    On non-zero exit, raises IndexingSubprocessError with captured stderr.

    This is the shared implementation extracted from golden_repo_manager.py
    (PATH B) for reuse in all indexing paths (PATH A, C, D, E).

    Monotonic guard: if last_reported is provided, any computed progress value
    that is strictly lower than last_reported is suppressed (not forwarded to
    progress_callback). This prevents visible progress regressions in the UI
    when a new phase starts at a lower global percentage than the previous
    phase ended at.

    No whole-job timeout is applied (Bug #1218): the only legitimate timeout is
    the per-request outbound embedding-provider HTTP call, which is handled
    inside the embedding providers themselves.

    Args:
        command: Command list to execute via subprocess.Popen
        phase_name: Phase name in the allocator (e.g., "semantic", "temporal")
        allocator: ProgressPhaseAllocator with calculate_weights already called
        progress_callback: Optional callable(pct, phase=..., detail=...) for updates
        all_stdout: Mutable list — accumulated stdout lines are appended here
        all_stderr: Mutable list — accumulated stderr lines are appended here
        cwd: Working directory for the subprocess (None = inherit)
        error_label: Human-readable label for error messages (defaults to phase_name)
        last_reported: Optional monotonic high-water mark from previous calls.
                       Any value below this will be suppressed. Defaults to None
                       (no suppression). Returns the highest value reported this call.
        env: Optional environment dict passed to subprocess.Popen. If None,
             the subprocess inherits the current process environment.
        orphan_event_callback: Optional callable(line: str) for Bug #1388
             HNSW orphan-repair marker lines. This is a channel entirely
             separate from progress_callback/_emit -- it is NEVER subject
             to the monotonic high-water-mark suppression described above,
             because that percentage channel silently drops a same-moment
             total=0 event once the phase is nearly complete (the root
             cause of the first, rejected #1388 fix attempt). The
             subprocess's captured stderr is scanned for
             HNSW_ORPHAN_REPAIR_MARKER-prefixed lines after it exits, and
             each matching line is forwarded verbatim to this callback.
             Defaults to None (no-op; most callers don't care about this
             event).

    Returns:
        The highest progress value reported during this call (or last_reported if
        nothing higher was emitted). Callers can pass this as last_reported to
        the next call to enforce monotonic progress across phases.
    """
    from code_indexer.services.progress_phase_allocator import parse_progress_line

    if error_label is None:
        error_label = phase_name

    # Monotonic high-water mark: never report below this value
    high_water: int = last_reported if last_reported is not None else 0

    def _emit(pct: int, phase: str, detail: str) -> None:
        """Emit progress only if it does not regress below the high-water mark."""
        nonlocal high_water
        if pct < high_water:
            return
        high_water = pct
        if progress_callback is not None:
            progress_callback(pct, phase=phase, detail=detail)

    # Issue #1530: the watchdog is armed purely by the caller-supplied
    # threshold; the heartbeat path and its env injection are owned by
    # run_with_popen_progress (the single generator), never re-derived here.
    watchdog_enabled = stale_activity_timeout_seconds is not None
    spawn_monotonic = time.monotonic()

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        # start_new_session places child (and any grandchildren it spawns) in a
        # new process group / session.  Combined with the poll-aware read loop
        # below, this prevents grandchildren that inherit the stdout PIPE
        # write-end from blocking the parent indefinitely after the child exits.
        # The shutdown-pipe signal (see below) is what makes termination fast.
        start_new_session=True,
        close_fds=True,
    )
    if process_holder is not None:
        # Publish immediately: from here on, any exit path (including an
        # exception) must be able to reach this child for cleanup.
        process_holder.append(process)

    # Report phase start (coarse marker before any lines arrive)
    global_start = int(allocator.phase_start(phase_name))
    _emit(global_start, phase=phase_name, detail=f"{phase_name}: starting...")

    # Read stdout line by line
    if process.stdout is None:
        raise IndexingSubprocessError(
            f"Failed to {error_label}: subprocess stdout pipe was not created"
        )

    # Detect whether stdout exposes a real OS file descriptor.
    # Real subprocess PIPE streams always do; mocked/StringIO streams do not.
    # The fallback path uses simple line-iteration (safe for mocks, no wedge
    # protection needed).  The real-fd path uses the select/shutdown-pipe reader
    # (BUG1/C1/C2 wedge protection — preserved completely unchanged).
    stdout_fd = _get_fd(process.stdout)

    if stdout_fd is None:
        # --- Fallback path: no real OS fd (mocked / StringIO stdout) --------
        # Simple line-iteration — identical progress/error semantics to the
        # real-fd path but without the select machinery (a mock can't wedge).
        for raw_line in process.stdout:
            all_stdout.append(raw_line)
            parsed = parse_progress_line(raw_line)
            if parsed is not None:
                global_pct = int(
                    allocator.map_phase_progress(
                        phase_name, parsed["current"], parsed["total"]
                    )
                )
                _emit(global_pct, phase=phase_name, detail=parsed.get("info", ""))

        # Drain stderr from mock stream if present.
        # Use .readlines() rather than direct iteration: the test mocks configure
        # stderr via mock.stderr.readlines.return_value, and real subprocess PIPE
        # streams also support .readlines() — so this is correct for both paths.
        stderr_output = ""
        if process.stderr is not None:
            stderr_output = "".join(process.stderr.readlines())
        all_stderr.append(stderr_output)
        _forward_hnsw_orphan_events(stderr_output, orphan_event_callback)

        process.wait()

        if process.returncode != 0:
            stdout_output = "".join(all_stdout)
            if process.returncode is not None and process.returncode < 0:
                signal_str = f"Exit code {process.returncode}"
                detail = stderr_output or stdout_output or ""
                error_details = (
                    f"{signal_str}. {detail}".rstrip(". ") if detail else signal_str
                )
            else:
                error_details = (
                    stderr_output or stdout_output or f"Exit code {process.returncode}"
                )
            # Heartbeat cleanup is the wrapper's `finally` (Issue #1530):
            # one owner, covering this raise and every other exit path.
            raise IndexingSubprocessError(f"Failed to {error_label}: {error_details}")

        return high_water
    # --- End fallback path ---------------------------------------------------

    # Shared constants and shutdown pipe for both stdout and stderr readers.
    #
    # Both reader threads select on their respective pipe fd AND shutdown_r.
    # When the child exits, the main loop writes a byte to shutdown_w, which
    # immediately unblocks both select() calls so both threads exit — without
    # waiting for grandchildren that inherited the pipe write-ends to close
    # them. (READ_BUFFER_SIZE / POLL_INTERVAL_SECONDS are module-level
    # constants -- Bug #1774 promoted them so the reader loops below could be
    # extracted to module scope and unit-tested directly.)

    stderr_fd = _get_fd(process.stderr) if process.stderr else -1
    if stderr_fd is None:
        stderr_fd = -1
    shutdown_r, shutdown_w = os.pipe()

    # Thread-safe queue: stdout reader puts decoded lines (str) or None (sentinel).
    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    # Stderr is accumulated in a plain list; the stderr reader thread is the
    # only writer, so no lock is needed (main thread reads only after join).
    stderr_lines: List[str] = []

    # Bug #1774: set either inside _read_available_bytes'/
    # _stdout_reader_loop's/_stderr_reader_loop's own except-Exception
    # branches, or by _join_reader_threads_before_closing_shutdown_pipe
    # on a timed-out join (round 3) -- never on a clean EOF/shutdown
    # finish. Checked (and surfaced as a WARNING, then a raised error via
    # _raise_if_reader_failed) once both readers are confirmed done, so
    # an abnormal reader termination is never silently indistinguishable
    # from a clean finish. Deliberately does NOT change what the main
    # loop does with the child process -- see this module's Bug #1774
    # comments.
    reader_failed = threading.Event()

    # Bug #1774: both reader loops are now module-level functions (see
    # _stdout_reader_loop / _stderr_reader_loop / _read_available_bytes
    # above) using selectors.DefaultSelector instead of select.select(),
    # with a broadened `except Exception` so an internal reader failure is
    # always logged (with a full traceback) and the sentinel/flush is
    # always still reached -- instead of a fd >= 1024 raising an uncaught
    # ValueError that used to kill the thread silently.
    stderr_thread = threading.Thread(
        target=_stderr_reader_loop,
        args=(stderr_fd, shutdown_r, stderr_lines, error_label, reader_failed),
        daemon=True,
    )
    stderr_thread.start()

    # Poll-aware read loop — the core fix for the grandchild fd-wedge problem.
    #
    # The old approach (`for line in process.stdout:`) blocks until the pipe's
    # write-end is closed by ALL holders, including grandchildren that inherit
    # the fd.  Even after the direct child exits, a grandchild sleeping with
    # the write-end open keeps the pipe alive and the loop blocked.
    #
    # Fix: both the stdout and stderr reader threads use selectors on their
    # respective fd AND a shared shutdown notification pipe.  The main loop
    # checks process.poll() every POLL_INTERVAL_SECONDS; when the child has
    # exited it writes a byte to shutdown_w, which immediately unblocks both
    # reader threads — without waiting for pipe EOF from a grandchild.
    #
    # start_new_session=True on the Popen places the child + grandchildren in a
    # new process group.  It does NOT prevent grandchildren from inheriting pipe
    # fds; the shutdown-pipe signal is what makes termination fast.
    stdout_reader_thread = threading.Thread(
        target=_stdout_reader_loop,
        args=(stdout_fd, shutdown_r, line_queue, error_label, reader_failed),
        daemon=True,
    )
    stdout_reader_thread.start()

    def _process_stdout_line(raw_line: str) -> None:
        """Append line to all_stdout and forward any parsed progress event."""
        all_stdout.append(raw_line)
        parsed = parse_progress_line(raw_line)
        if parsed is not None:
            global_pct = int(
                allocator.map_phase_progress(
                    phase_name, parsed["current"], parsed["total"]
                )
            )
            _emit(global_pct, phase=phase_name, detail=parsed.get("info", ""))

    def _drain_line_queue() -> bool:
        """Drain all currently available lines from line_queue.

        Returns True if the sentinel (None) was encountered, meaning the
        reader thread has finished and no more lines will arrive.
        """
        while True:
            try:
                item = line_queue.get_nowait()
            except queue.Empty:
                return False
            if item is None:
                return True  # sentinel: reader thread is done
            _process_stdout_line(item)

    # Main loop: drain the queue and check process.poll() every
    # POLL_INTERVAL_SECONDS.  When the child exits, signal the reader thread.
    # Issue #1530: when armed, also checks the heartbeat file for staleness
    # every _WATCHDOG_CHECK_INTERVAL_SECONDS -- driven by this SAME
    # process.poll()-based loop, independent of the stdout reader's own
    # sentinel/completion state (design point 8: the watchdog must not
    # inherit the stdout-reader's early-exit blind spot).
    watchdog_verdict = None
    last_watchdog_check = spawn_monotonic
    # Issue #1530 design point 8: the stdout reader finishing must not end
    # the watchdog's observation while the child is still alive. Breaking
    # out on reader completion dropped the parent into an unbounded
    # process.wait() in which a wedged child that had closed its stdout was
    # never detected at all. With the watchdog armed the loop instead keeps
    # polling the child; with it disarmed both breaks fire exactly as before.
    reader_finished = False
    try:
        while True:
            if _drain_line_queue():
                # Sentinel received — reader is done.
                reader_finished = True
                if not watchdog_enabled:
                    break

            if process.poll() is not None:
                # Child has exited — signal reader thread to stop immediately.
                try:
                    os.write(shutdown_w, b"x")
                except OSError as exc:
                    logger.warning(
                        "run_with_popen_progress: could not signal shutdown "
                        "pipe for %s: %s",
                        error_label,
                        exc,
                    )
                # Wait for reader to finish, then drain remaining lines.
                stdout_reader_thread.join(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
                _drain_line_queue()
                break

            if watchdog_enabled and (
                time.monotonic() - last_watchdog_check
                >= _WATCHDOG_CHECK_INTERVAL_SECONDS
            ):
                last_watchdog_check = time.monotonic()
                assert heartbeat_path is not None  # watchdog_enabled implies this
                assert stale_activity_timeout_seconds is not None
                verdict = check_and_terminate_if_stale(
                    process,
                    heartbeat_path,
                    threshold_seconds=stale_activity_timeout_seconds,
                    spawn_monotonic=spawn_monotonic,
                )
                if verdict is not None:
                    # Subprocess has already been terminated+reaped by
                    # check_and_terminate_if_stale. Shut the reader down
                    # the same way the process.poll() branch above does.
                    watchdog_verdict = verdict
                    try:
                        os.write(shutdown_w, b"x")
                    except OSError as exc:
                        logger.warning(
                            "run_with_popen_progress: could not signal "
                            "shutdown pipe for %s: %s",
                            error_label,
                            exc,
                        )
                    stdout_reader_thread.join(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
                    _drain_line_queue()
                    break

            if reader_finished:
                # Reader is done but the child is still alive (watchdog
                # armed). Wait on the CHILD rather than joining an already-
                # dead thread, which would spin: this blocks for one poll
                # interval, or returns early the moment the child exits --
                # the next iteration then takes the process.poll() branch.
                try:
                    process.wait(timeout=POLL_INTERVAL_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                continue

            # Brief sleep before next poll — bounded, not unbounded.
            stdout_reader_thread.join(timeout=POLL_INTERVAL_SECONDS)
            if not stdout_reader_thread.is_alive():
                # Reader finished on its own (natural EOF before child exited).
                _drain_line_queue()
                if not watchdog_enabled:
                    break
                reader_finished = True
    finally:
        # C1 fix: signal shutdown on EVERY exit path (natural EOF, poll-detected
        # child exit, and exception).  Write the shutdown byte BEFORE closing
        # the fds so both reader threads' selector.select() calls are woken up.
        # Idempotent: if the byte was already written by the poll branch above,
        # this is a no-op (level-triggered selector; both readers still wake).
        try:
            os.write(shutdown_w, b"x")
        except OSError as exc:
            # Explicit, intentional discard: shutdown_w may already be
            # closed, or the byte may already have been written by one of
            # the poll branches above -- both are fine and expected, but
            # logged (not a bare `pass`) per anti-silent-failure.
            logger.debug(
                "run_with_popen_progress: shutdown-pipe write for %s "
                "skipped (already closed or already written): %s",
                error_label,
                exc,
            )

        # Bug #1774 rounds 2/3/4: the shutdown pipe fds must never be
        # closed until BOTH reader threads have actually joined (or a
        # reaper thread takes over that responsibility on a timed-out
        # join). See _join_reader_threads_before_closing_shutdown_pipe's
        # docstring/preceding comment for the full, canonical rationale
        # -- not duplicated here.
        _join_reader_threads_before_closing_shutdown_pipe(
            stdout_reader_thread,
            stderr_thread,
            reader_failed,
            error_label,
            GIT_COMMAND_TIMEOUT_SECONDS,
            shutdown_r,
            shutdown_w,
        )

        # Close the shutdown pipe fds to avoid fd leaks -- only now that
        # both reader threads are confirmed done. On a timed-out join,
        # _join_reader_threads_before_closing_shutdown_pipe raises before
        # this point is ever reached -- ownership of eventually closing
        # these fds passes to its reaper thread instead.
        for _fd in (shutdown_r, shutdown_w):
            try:
                os.close(_fd)
            except OSError as exc:
                logger.warning(
                    "run_with_popen_progress: could not close shutdown pipe "
                    "fd %d for %s: %s",
                    _fd,
                    error_label,
                    exc,
                )

    process.wait()

    if reader_failed.is_set():
        # Bug #1774 (code review finding): surface an abnormal reader
        # termination distinctly from a clean finish. process.wait()
        # above already ran unconditionally, exactly as it always has --
        # this log line, plus the raise below (once the more specific
        # watchdog-kill / non-zero-exit checks have had first priority),
        # makes a truncated stdout/stderr capture an explicit, loud
        # failure instead of being silently indistinguishable from
        # success. The underlying failure was already logged (with a
        # full traceback) by whichever reader loop set this flag.
        logger.warning(
            "run_with_popen_progress: %s -- one or more reader threads "
            "terminated abnormally; captured stdout/stderr may be "
            "truncated (see the prior ERROR/WARNING log entries for the "
            "underlying cause)",
            error_label,
        )

    stderr_output = "".join(stderr_lines)
    all_stderr.append(stderr_output)
    _forward_hnsw_orphan_events(stderr_output, orphan_event_callback)

    # Heartbeat cleanup is the wrapper's `finally` (Issue #1530): one
    # owner, covering all raises below and the successful return.
    if watchdog_verdict is not None:
        raise IndexingWatchdogKillError(
            f"Failed to {error_label}: watchdog killed subprocess "
            f"(pid={watchdog_verdict.pid}, reason={watchdog_verdict.reason}, "
            f"stuck_label={watchdog_verdict.label}, "
            f"age={watchdog_verdict.age_seconds}s, "
            f"threshold={stale_activity_timeout_seconds}s) -- no forward "
            f"progress detected"
        )

    if process.returncode != 0:
        stdout_output = "".join(all_stdout)
        if process.returncode < 0:
            # Signal-terminated process: always lead with the signal code so that
            # callers such as refresh_scheduler.py can match "Exit code -15" for
            # SIGTERM routing.  The banner/stderr text is appended as context.
            signal_str = f"Exit code {process.returncode}"
            detail = stderr_output or stdout_output or ""
            error_details = (
                f"{signal_str}. {detail}".rstrip(". ") if detail else signal_str
            )
        else:
            error_details = (
                stderr_output or stdout_output or f"Exit code {process.returncode}"
            )
        raise IndexingSubprocessError(f"Failed to {error_label}: {error_details}")

    # Bug #1774 round 2 (Codex finding): reached only after the
    # watchdog-kill and non-zero-exit checks above, so a process that
    # both failed AND had a reader failure still raises the more
    # specific/definitive error first. See _raise_if_reader_failed's
    # docstring for the full rationale.
    _raise_if_reader_failed(reader_failed, error_label)

    return high_water


def _raise_if_reader_failed(reader_failed: threading.Event, error_label: str) -> None:
    """Bug #1774 round 2 (Codex finding): a reader thread failing
    abnormally must not be silently treated as a successful run just
    because the subprocess itself exited 0 and the watchdog didn't kill
    it -- captured stdout/stderr may be truncated or incomplete, and a
    caller (e.g. golden-repo indexing) must not treat that as a
    complete, trustworthy index. `reader_failed` is set exclusively by
    `_read_available_bytes`'/`_stdout_reader_loop`'s/`_stderr_reader_loop`'s
    own except-Exception branches (never on a clean EOF/shutdown finish),
    so this never fires for a healthy job. No-op when `reader_failed` is
    clear.
    """
    if reader_failed.is_set():
        raise IndexingSubprocessError(
            f"Failed to {error_label}: reader thread(s) terminated "
            f"abnormally -- captured stdout/stderr may be truncated or "
            f"incomplete even though the subprocess itself exited "
            f"successfully (see the prior ERROR/WARNING log entries for "
            f"the underlying cause)"
        )


# Bug #1774 rounds 2/3/4 -- canonical explanation (the finally block
# above points here rather than duplicating this):
#
# Round 2 (Codex and Claude independently converged): the shutdown pipe
# fds must NEVER be closed until BOTH reader threads have actually
# joined. stdout_reader_thread was usually already joined by one of the
# main loop's branches, but stderr_thread was joined only much later --
# AFTER the finally block had already closed shutdown_r/shutdown_w (and,
# on the exception exit path, was never joined at all). That let the
# stderr reader still be alive, with those fds registered in its own
# selector, at the exact moment they got closed and their numbers freed
# for reuse: (a) a healthy stderr reader that hadn't yet observed the
# shutdown byte hit the fd-closed staleness path and got falsely flagged
# as reader_failed, and (b) once a closed fd number got reused elsewhere
# in a busy process, `_fd_is_open()` reported "still open" for a
# completely different file description, so the staleness probe never
# fired and the reader spun forever, leaking the thread. Joining BOTH
# threads before either fd is closed closes both holes at once.
#
# Round 3 (Codex finding): a join that actually times out must NEVER
# fall through to closing the shutdown fds anyway -- that would
# reintroduce the exact hazard round 2 fixed, precisely when it matters
# most (a reader thread confirmed still running). This join is bounded
# cleanup synchronization for reader threads the caller itself spawned
# -- NOT a subprocess/job clock -- so raising on timeout does not
# reintroduce a Bug #1218 timeout; the CHILD's own runtime remains
# completely unbounded regardless of this outcome.
#
# Round 4 (Codex finding, endorsed over Claude's "acceptable leak" call
# on this specific project): simply raising and permanently abandoning
# the fds on a timeout is itself a resource leak -- exactly the failure
# shape (small leaks compounding over weeks of server uptime) that
# motivated this whole investigation (see sibling Bug #1775). Fixed with
# a small, bounded reaper: a fire-and-forget daemon thread
# (`_reap_stuck_readers_and_close_shutdown_pipe`, spawned by
# `_spawn_reaper_and_raise`) that finishes joining the still-alive
# reader(s) with NO timeout, then closes the shutdown fds once that join
# actually completes. The reaper never blocks the raise -- the job still
# fails loudly and promptly. If a reader is truly permanently wedged
# (not just transiently stalled), the reaper also never completes and
# the fds still leak -- an accepted residual (Python cannot forcibly
# kill a thread), but this closes the much more likely "transient
# stall" case a 30s timeout is actually catching.
def _join_reader_threads_before_closing_shutdown_pipe(
    stdout_reader_thread: threading.Thread,
    stderr_thread: threading.Thread,
    reader_failed: threading.Event,
    error_label: str,
    timeout_seconds: float,
    shutdown_r: int,
    shutdown_w: int,
) -> None:
    """Join both reader threads with `timeout_seconds`. On a timed-out
    join, hands off to `_spawn_reaper_and_raise` (starts a reaper thread
    that will eventually close shutdown_r/shutdown_w, then raises
    IndexingSubprocessError). See the comment block above for the full
    round 2/3/4 rationale.
    """
    timed_out_stream_name = None
    stuck_threads: List[threading.Thread] = []
    for reader_thread, stream_name in (
        (stdout_reader_thread, "stdout"),
        (stderr_thread, "stderr"),
    ):
        reader_thread.join(timeout=timeout_seconds)
        if reader_thread.is_alive():
            reader_failed.set()
            stuck_threads.append(reader_thread)
            if timed_out_stream_name is None:
                timed_out_stream_name = stream_name
            logger.error(
                "run_with_popen_progress: %s reader thread for %s did "
                "not join within %.2fs after shutdown was signalled -- "
                "the shutdown pipe will NOT be closed while this reader "
                "may still reference it; a reaper thread will close it "
                "once the reader eventually joins",
                stream_name,
                error_label,
                timeout_seconds,
            )

    if timed_out_stream_name is not None:
        _spawn_reaper_and_raise(
            stuck_threads,
            shutdown_r,
            shutdown_w,
            error_label,
            timed_out_stream_name,
            timeout_seconds,
        )


def _spawn_reaper_and_raise(
    stuck_threads: List[threading.Thread],
    shutdown_r: int,
    shutdown_w: int,
    error_label: str,
    timed_out_stream_name: str,
    timeout_seconds: float,
    start_reaper_thread: Callable[[threading.Thread], None] = threading.Thread.start,
) -> None:
    """Start the round-4 reaper thread (fire-and-forget -- does not wait
    for it), then raise loudly. Never returns normally.

    Bug #1774 round 5 (Codex/Claude finding): `Thread.start()` can itself
    raise `RuntimeError` under genuine thread/resource exhaustion --
    precisely the degraded state this whole bug is about. If that
    happens, no reaper exists to eventually close the fds (a leak in
    that narrow sub-case -- not a regression versus round 3, which
    leaked on every timeout, not just this one), but the exception TYPE
    contract must still hold: callers that catch IndexingSubprocessError
    specifically (e.g. golden_repo_manager.py, translating it to
    GitOperationError) must never see a raw RuntimeError escape instead.

    `start_reaper_thread` is a testable seam (defaults to the real
    `threading.Thread.start`, so production behavior is unchanged) that
    lets tests inject a genuine start failure without monkeypatching any
    process-wide thread behavior.
    """
    reaper = threading.Thread(
        target=_reap_stuck_readers_and_close_shutdown_pipe,
        args=(stuck_threads, shutdown_r, shutdown_w, error_label),
        daemon=True,
    )
    try:
        start_reaper_thread(reaper)
    except Exception as exc:  # noqa: BLE001 - Bug #1774 round 5
        logger.warning(
            "run_with_popen_progress: could not start reaper thread for "
            "%s -- the shutdown pipe will leak (thread/resource "
            "exhaustion likely already in progress): %s",
            error_label,
            exc,
        )
    raise IndexingSubprocessError(
        f"Failed to {error_label}: {timed_out_stream_name} reader "
        f"could not be reaped within {timeout_seconds:.2f}s -- refusing "
        f"to close the shutdown pipe while it may still be in use "
        f"(a background reaper will close it once the reader "
        f"eventually joins)"
    )


def _reap_stuck_readers_and_close_shutdown_pipe(
    stuck_threads: List[threading.Thread],
    shutdown_r: int,
    shutdown_w: int,
    error_label: str,
) -> None:
    """Bug #1774 round 4: fire-and-forget daemon reaper for the
    timed-out-join case. Finishes joining each still-alive reader thread
    with NO timeout (unlike the bounded join above, these are expected to
    eventually finish once whatever transient stall resolves), then
    closes the shutdown pipe fds once that join actually completes.
    Runs entirely on its own thread -- never blocks the caller's raise.
    """
    # Messi Rule #14 (anti-unbounded-loop) exception, the same carve-out
    # already documented for _read_available_bytes's selector loop above:
    # this join is bounded by an external event (the reader thread
    # eventually finishing its own work), not an iteration count -- a
    # permanently wedged reader is the sole, explicitly accepted
    # residual (Python cannot forcibly kill a thread).
    for reader_thread in stuck_threads:
        reader_thread.join()
    for _fd in (shutdown_r, shutdown_w):
        try:
            os.close(_fd)
        except OSError as exc:
            logger.warning(
                "run_with_popen_progress: reaper thread could not close "
                "shutdown pipe fd %d for %s: %s",
                _fd,
                error_label,
                exc,
            )
    logger.info(
        "run_with_popen_progress: reaper thread for %s finished joining "
        "previously-stuck reader thread(s) and closed the shutdown pipe",
        error_label,
    )
