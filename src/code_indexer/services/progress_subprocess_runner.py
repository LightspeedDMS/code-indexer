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
import select
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
    # waiting for grandchildren that inherited the pipe write-ends to close them.
    READ_BUFFER_SIZE = 4096
    POLL_INTERVAL_SECONDS = 0.05

    stderr_fd = _get_fd(process.stderr) if process.stderr else -1
    if stderr_fd is None:
        stderr_fd = -1
    shutdown_r, shutdown_w = os.pipe()

    # Thread-safe queue: stdout reader puts decoded lines (str) or None (sentinel).
    import queue as _queue_mod

    line_queue: "_queue_mod.Queue[Optional[str]]" = _queue_mod.Queue()

    # Stderr is accumulated in a plain list; the stderr reader thread is the
    # only writer, so no lock is needed (main thread reads only after join).
    stderr_lines: List[str] = []

    def _stderr_reader() -> None:
        """Read raw bytes from stderr_fd via select; accumulate in stderr_lines.

        Exits when shutdown_r is signalled (child exited) or natural EOF.
        A grandchild holding the stderr write-end is bypassed by the shutdown
        signal — stderr content written before child exit is still captured.

        C2 fix: check the DATA fd first so that when both stderr_fd and
        shutdown_r are ready in the same select cycle, we drain the data
        before honouring the shutdown — preventing dropped final error bytes.
        """
        if stderr_fd < 0:
            return
        buf = b""
        try:
            while True:
                rlist, _, _ = select.select([stderr_fd, shutdown_r], [], [])
                if stderr_fd in rlist:
                    chunk = os.read(stderr_fd, READ_BUFFER_SIZE)
                    if not chunk:
                        break  # natural EOF
                    buf += chunk
                    continue  # re-select; drain before honouring shutdown
                if shutdown_r in rlist:
                    # Shutdown signalled — data fd not ready, safe to stop.
                    break
        except OSError as exc:
            logger.warning(
                "run_with_popen_progress: stderr reader OSError for %s: %s",
                error_label,
                exc,
            )
        if buf:
            stderr_lines.append(buf.decode("utf-8", errors="replace"))

    stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
    stderr_thread.start()

    # Poll-aware read loop — the core fix for the grandchild fd-wedge problem.
    #
    # The old approach (`for line in process.stdout:`) blocks until the pipe's
    # write-end is closed by ALL holders, including grandchildren that inherit
    # the fd.  Even after the direct child exits, a grandchild sleeping with
    # the write-end open keeps the pipe alive and the loop blocked.
    #
    # Fix: both the stdout and stderr reader threads use select.select() on
    # their respective fd AND a shared shutdown notification pipe.  The main
    # loop checks process.poll() every POLL_INTERVAL_SECONDS; when the child
    # has exited it writes a byte to shutdown_w, which immediately unblocks
    # both reader threads — without waiting for pipe EOF from a grandchild.
    #
    # start_new_session=True on the Popen places the child + grandchildren in a
    # new process group.  It does NOT prevent grandchildren from inheriting pipe
    # fds; the shutdown-pipe signal is what makes termination fast.

    def _stdout_reader() -> None:
        """Read raw bytes from stdout_fd via select; put lines on line_queue.

        Exits when a shutdown signal arrives on shutdown_r (main thread writes
        after child exit) or when the stdout fd reaches natural EOF.
        Always puts None as a sentinel when done so the main loop can detect
        reader completion without polling thread liveness.

        C2 fix: check the DATA fd first so that when both stdout_fd and
        shutdown_r are ready in the same select cycle, we drain the data
        before honouring the shutdown — preventing dropped final progress lines.
        """
        buf = b""
        try:
            while True:
                rlist, _, _ = select.select([stdout_fd, shutdown_r], [], [])
                if stdout_fd in rlist:
                    chunk = os.read(stdout_fd, READ_BUFFER_SIZE)
                    if not chunk:
                        # EOF: all write-end holders have closed their copy.
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line_queue.put(raw.decode("utf-8", errors="replace") + "\n")
                    continue  # re-select; drain before honouring shutdown
                if shutdown_r in rlist:
                    # Shutdown signalled — data fd not ready, safe to stop.
                    break
        except OSError as exc:
            logger.warning(
                "run_with_popen_progress: stdout reader OSError for %s: %s",
                error_label,
                exc,
            )
        # Flush any partial line remaining in the buffer.
        if buf:
            line_queue.put(buf.decode("utf-8", errors="replace"))
        # Sentinel: signals main loop that no more lines are coming.
        line_queue.put(None)

    stdout_reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
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
            except _queue_mod.Empty:
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
        # the fds so both reader threads' select.select() calls are woken up.
        # Idempotent: if the byte was already written by the poll branch above,
        # this is a no-op (level-triggered select; both readers still wake).
        try:
            os.write(shutdown_w, b"x")
        except OSError:
            pass  # already closed or already written — both are fine
        # Close the shutdown pipe fds to avoid fd leaks.
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
    # Both reader threads exit promptly when shutdown_w is signalled (on child
    # exit) — no need for a short timeout workaround here.
    stderr_thread.join(timeout=GIT_COMMAND_TIMEOUT_SECONDS)

    stderr_output = "".join(stderr_lines)
    all_stderr.append(stderr_output)
    _forward_hnsw_orphan_events(stderr_output, orphan_event_callback)

    # Heartbeat cleanup is the wrapper's `finally` (Issue #1530): one
    # owner, covering both raises below and the successful return.
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

    return high_water
