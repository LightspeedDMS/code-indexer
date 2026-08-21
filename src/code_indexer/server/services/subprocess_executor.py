"""
Subprocess Executor Service for async command execution with timeout protection.

Provides non-blocking subprocess execution with file-based output to prevent
memory exhaustion and FastAPI event loop blocking.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import asyncio
import logging
import selectors
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Issue #1601 remediation (Priority 1): a size-capped subprocess's stdout and
# stderr are BOTH piped and drained concurrently via a selectors-based event
# loop (see _wait_with_output_cap), rather than writing stdout straight to a
# file and only polling its on-disk size. Two independent defects this fixes:
#
# - (1a) the old poll-only implementation never read from the child's stderr
#   pipe while polling, so a child that wrote enough to stderr to fill the OS
#   pipe buffer (observed: 64 KiB on this machine) blocked forever inside its
#   own write() syscall -- process.poll() never returned, and the output
#   file's size stopped growing, so the loop spun until the full wall-clock
#   timeout even though the "search" itself was effectively instantaneous.
# - (1b) the byte ceiling was enforced by periodically checking the output
#   file's size, not by bounding the write itself -- a fast writer could
#   produce several multiples of the ceiling before the next poll caught it.
#
# _READ_CHUNK_BYTES bounds a single stdout read, so the worst-case overshoot
# above max_output_bytes is at most one chunk -- a precise, write-time bound
# instead of a poll-interval-dependent approximation.
_READ_CHUNK_BYTES = 64 * 1024  # 64 KiB

# Upper bound on how much stderr is retained for diagnostics/logging while
# concurrently draining it. Draining itself is unconditional (required to
# prevent the pipe-full deadlock); this only caps how much of it we keep.
_STDERR_CAPTURE_MAX_BYTES = 64 * 1024

# Bounds how long a single selectors.select() call blocks, so the wall-clock
# deadline is always re-checked promptly even if neither stream has data.
_SELECT_POLL_SECONDS = 0.5

# Grace period to reap the process after both stdout and stderr have hit
# EOF (meaning the child is exiting, or has already exited) -- should be
# near-instant in practice; this only guards against a pathological case
# where a process closes both its output streams without actually exiting.
_PROCESS_EXIT_GRACE_SECONDS = 5


class ExecutionStatus(str, Enum):
    """Status of command execution."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class SearchExecutionResult:
    """Result of a subprocess execution."""

    status: ExecutionStatus
    output_file: str
    exit_code: Optional[int] = None
    timed_out: bool = False
    timeout_seconds: Optional[int] = None
    error_message: Optional[str] = None
    stderr_output: Optional[str] = None
    # Issue #1601 (Fix direction 4a): True when this call's subprocess was
    # terminated early because its output file crossed max_output_bytes
    # WHILE STILL RUNNING -- distinct from timed_out (wall-clock) and from a
    # normal non-zero exit. Always False when max_output_bytes was not
    # supplied, or when the process finished naturally before crossing it.
    output_capped: bool = False


class SubprocessExecutor:
    """
    Executes subprocess commands asynchronously with timeout protection.

    Features:
    - Async execution prevents FastAPI event loop blocking
    - File-based output prevents RAM exhaustion DURING SUBPROCESS EXECUTION
      ONLY (Issue #1601 correction to an earlier, overclaiming version of
      this docstring): stdout is redirected straight to a file rather than
      buffered in memory while the subprocess runs, but that says nothing
      about what the CALLER does with the finished file afterward -- a
      caller that then does an unconditional whole-file read defeats this
      guarantee entirely. Callers remain responsible for bounding their own
      read of the result UNLESS they pass ``max_output_bytes`` to
      ``execute_with_limits``/``_run_subprocess``, in which case the
      ceiling is enforced end-to-end: the still-running subprocess is
      terminated the moment its output crosses the ceiling (see
      ``_wait_with_output_cap``), not merely bounding a later read.
    - Thread pool for concurrent execution
    - Timeout protection with process termination
    - Partial output capture on timeout
    """

    def __init__(self, max_workers: int = 4):
        """
        Initialize subprocess executor.

        Args:
            max_workers: Maximum concurrent subprocess executions
        """
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._shutdown = False

    async def execute_with_limits(
        self,
        command: List[str],
        working_dir: str,
        timeout_seconds: int,
        output_file_path: str,
        max_output_bytes: Optional[int] = None,
    ) -> SearchExecutionResult:
        """
        Execute command asynchronously with timeout and file output.

        Args:
            command: Command and arguments to execute
            working_dir: Working directory for command execution
            timeout_seconds: Maximum execution time in seconds
            output_file_path: Path to file for capturing output
            max_output_bytes: Issue #1601 (Fix direction 4a). When set, the
                subprocess's output is bounded WHILE it runs (see
                ``_wait_with_output_cap``); the process is TERMINATED the
                moment its stdout crosses this many bytes, rather than
                being allowed to run to completion. Must be a positive
                integer when supplied. When None (default), behavior is
                unchanged from before this parameter existed.
                See ``SearchExecutionResult.output_capped``.

        Returns:
            SearchExecutionResult with execution status and output file path
        """
        if self._shutdown:
            raise RuntimeError("Executor has been shut down")
        self._validate_max_output_bytes(max_output_bytes)

        # Ensure output file directory exists
        output_path = Path(output_file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Run subprocess in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    self._run_subprocess,
                    command,
                    working_dir,
                    output_file_path,
                    timeout_seconds,
                    max_output_bytes,
                ),
                # Issue #1601 remediation round 5 (Priority 2): a flat
                # "+1s" does not cover the full worst-case synchronous
                # termination/cleanup sequence _wait_with_output_cap can
                # run through after its own internal deadline is reached
                # (see _TERMINATION_CLEANUP_BUDGET_SECONDS's docstring for
                # the exact worst-case path). Budget for that in full,
                # plus a small additional safety margin, so this outer
                # deadline is never the thing that fires first.
                timeout=timeout_seconds + self._TERMINATION_CLEANUP_BUDGET_SECONDS + 1,
            )
            return result

        except asyncio.TimeoutError:
            # Asyncio timeout exceeded (should not happen if subprocess timeout works)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-183",
                    f"Asyncio timeout exceeded for command: {' '.join(command)}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return SearchExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                output_file=output_file_path,
                timed_out=True,
                timeout_seconds=timeout_seconds,
                error_message="Command execution timed out",
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-184",
                    f"Unexpected error executing command: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file=output_file_path,
                error_message=str(e),
            )

    @staticmethod
    def _validate_max_output_bytes(max_output_bytes: Optional[int]) -> None:
        """Reject anything but a positive int or None for max_output_bytes."""
        if max_output_bytes is None:
            return
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
            raise TypeError(
                f"max_output_bytes must be an int or None, "
                f"got {type(max_output_bytes).__name__}"
            )
        if max_output_bytes <= 0:
            raise ValueError(
                f"max_output_bytes must be a positive integer or None, "
                f"got {max_output_bytes!r}"
            )

    # Priority 6 (Issue #1601): SIGTERM-then-SIGKILL escalation grace
    # periods -- either confirms the process reaped or logs unambiguously.
    _TERMINATE_GRACE_SECONDS = 2
    _KILL_GRACE_SECONDS = 5

    # Issue #1601 remediation round 5 (Priority 2): total worst-case
    # synchronous cleanup time ``_wait_with_output_cap``'s various paths
    # can spend AFTER their own internal deadline is reached, before
    # control returns to the caller. The natural-EOF reap wait
    # (``_PROCESS_EXIT_GRACE_SECONDS``) can itself time out and escalate
    # through the full SIGTERM+SIGKILL sequence (``_TERMINATE_GRACE_SECONDS``
    # + ``_KILL_GRACE_SECONDS``). ``execute_with_limits``'s outer
    # ``asyncio.wait_for`` deadline MUST budget for this in full -- a flat
    # "+1s" is not enough -- or it can time out and return to the caller
    # while this synchronous sequence is still running in the thread pool
    # underneath (see that method for how this is used).
    _TERMINATION_CLEANUP_BUDGET_SECONDS = (
        _PROCESS_EXIT_GRACE_SECONDS + _TERMINATE_GRACE_SECONDS + _KILL_GRACE_SECONDS
    )

    @staticmethod
    def _terminate_process(
        process: "subprocess.Popen",
        warn_log_id: str,
        error_log_id: str,
        warn_message: str,
        error_message: str,
    ) -> None:
        """Escalate SIGTERM -> SIGKILL (only if still alive) and
        guarantee reaping. Race exits (ProcessLookupError/OSError) are
        expected, logged at debug. error_message logs at ERROR only if
        still alive after both attempts."""
        logger.warning(
            format_error_log(
                warn_log_id,
                warn_message,
                extra={"correlation_id": get_correlation_id()},
            )
        )

        try:
            process.terminate()
            process.wait(timeout=SubprocessExecutor._TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        except (ProcessLookupError, OSError) as race_error:
            logger.debug("terminate() raced with process exit: %s", race_error)

        if process.poll() is None:
            try:
                process.kill()
            except (ProcessLookupError, OSError) as race_error:
                logger.debug("kill() raced with process exit: %s", race_error)

        try:
            process.wait(timeout=SubprocessExecutor._KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error(
                format_error_log(
                    error_log_id,
                    error_message,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    @staticmethod
    def _build_completion_result(
        process: "subprocess.Popen",
        output_file_path: str,
        stderr: Optional[str],
    ) -> SearchExecutionResult:
        """Build the SUCCESS/ERROR result for a process that has exited."""
        if process.returncode == 0:
            return SearchExecutionResult(
                status=ExecutionStatus.SUCCESS,
                output_file=output_file_path,
                exit_code=process.returncode,
                timed_out=False,
                stderr_output=stderr,
            )
        return SearchExecutionResult(
            status=ExecutionStatus.ERROR,
            output_file=output_file_path,
            exit_code=process.returncode,
            error_message=f"Command exited with code {process.returncode}",
            stderr_output=stderr,
        )

    @staticmethod
    def _build_timeout_result(
        output_file_path: str, timeout_seconds: int
    ) -> SearchExecutionResult:
        """Build the TIMEOUT result shared by both wait strategies."""
        return SearchExecutionResult(
            status=ExecutionStatus.TIMEOUT,
            output_file=output_file_path,
            timed_out=True,
            timeout_seconds=timeout_seconds,
            error_message=f"Command timed out after {timeout_seconds} seconds",
        )

    def _run_subprocess(
        self,
        command: List[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: int,
        max_output_bytes: Optional[int] = None,
    ) -> SearchExecutionResult:
        """
        Run subprocess synchronously in thread pool.

        This method runs in a thread pool thread, not the main event loop.

        Args:
            command: Command and arguments to execute
            working_dir: Working directory for command execution
            output_file_path: Path to file for capturing output
            timeout_seconds: Maximum execution time in seconds
            max_output_bytes: Issue #1601 (Fix direction 4a). When set,
                both stdout and stderr are piped (unbuffered, binary) and
                process supervision is delegated to
                ``_wait_with_output_cap`` instead of the communicate()-based
                wait below, so the byte ceiling can be enforced at
                write-time and stderr can be drained concurrently (see
                that method's docstring for why both matter).

        Returns:
            SearchExecutionResult with execution details
        """
        self._validate_max_output_bytes(max_output_bytes)
        try:
            if max_output_bytes is not None:
                # Both streams piped and unbuffered (bufsize=0) so reads
                # via selectors return exactly what the kernel has ready,
                # matching selector readiness semantics precisely -- no
                # Python-level buffering can mask that a stream is at EOF
                # or make a "ready" read block trying to fill a buffer.
                # Binary mode (no text=True): a distinct variable name
                # from the text-mode Popen below avoids a mypy
                # Popen[str]/Popen[bytes] redefinition conflict for the
                # same name in one function scope.
                binary_process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=working_dir,
                    bufsize=0,
                )
                return self._wait_with_output_cap(
                    binary_process,
                    command,
                    output_file_path,
                    timeout_seconds,
                    max_output_bytes,
                )

            # Open output file for writing stdout
            with open(output_file_path, "w") as output_file:
                # Start process with file output
                text_process = subprocess.Popen(
                    command,
                    stdout=output_file,
                    stderr=subprocess.PIPE,
                    cwd=working_dir,
                    text=True,
                )

                try:
                    # Wait for process with timeout
                    _, stderr = text_process.communicate(timeout=timeout_seconds)
                    return self._build_completion_result(
                        text_process, output_file_path, stderr if stderr else None
                    )

                except subprocess.TimeoutExpired:
                    self._terminate_process(
                        text_process,
                        "MCP-GENERAL-185",
                        "MCP-GENERAL-186",
                        f"Command timed out after {timeout_seconds}s: "
                        f"{' '.join(command)}",
                        "Failed to kill timed out process",
                    )
                    return self._build_timeout_result(output_file_path, timeout_seconds)

        except FileNotFoundError:
            return SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file=output_file_path,
                error_message=f"Command not found: {command[0]}",
            )

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-187",
                    f"Error running subprocess: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file=output_file_path,
                error_message=str(e),
            )

    def _wait_with_output_cap(
        self,
        process: "subprocess.Popen",
        command: List[str],
        output_file_path: str,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> SearchExecutionResult:
        """
        Supervise an already-started subprocess (stdout AND stderr both
        piped), terminating it early if its stdout crosses
        ``max_output_bytes`` WHILE IT IS STILL RUNNING.

        Issue #1601 remediation (Priority 1, replacing the earlier Fix
        direction 4a poll-only implementation): a ``selectors``-based event
        loop concurrently drains BOTH stdout and stderr, writing stdout
        chunks to ``output_file_path`` as they arrive and counting bytes
        directly against the ceiling. This fixes two compounding defects in
        the poll-only predecessor:

        - Never draining stderr meant a child that wrote enough to fill the
          OS pipe buffer (observed: 64 KiB) blocked forever inside its own
          stderr write() syscall -- indistinguishable, from the poll loop's
          point of view, from a process still legitimately running. Here,
          stderr is read on every loop iteration it has data, so the child
          can never block on a full stderr pipe.
        - Polling the output FILE's size only bounded the write
          approximately (up to however much a fast writer produced in one
          poll interval). Here, the ceiling is checked after every
          individual stdout chunk read (each capped at
          ``_READ_CHUNK_BYTES``), bounding the overshoot to at most one
          chunk -- a write-time guarantee, not a poll-interval
          approximation.
        """
        deadline = time.monotonic() + timeout_seconds
        bytes_written = 0
        stderr_chunks: List[bytes] = []
        stderr_bytes_kept = 0
        capped = False
        hit_deadline = False

        # This method is only ever called with a process constructed via
        # Popen(..., stdout=PIPE, stderr=PIPE) (see _run_subprocess) --
        # both streams are always real pipes, never None. Asserting this
        # (rather than a silent Optional-tolerant check) is a defensive
        # invariant per this project's own standards, and also narrows
        # the type for the selector registration below.
        assert process.stdout is not None, "stdout must be piped for output capping"
        assert process.stderr is not None, "stderr must be piped for output capping"

        # Issue #1601 remediation round 5 (Priority 3, both round-4
        # reviewers independently found this same 3-line issue):
        # selector construction and registration used to happen BEFORE
        # this try block -- ``DefaultSelector()`` allocates a real epoll
        # fd and ``register()`` can genuinely fail (e.g. under fd
        # exhaustion / EMFILE, exactly the condition the Priority-1
        # fd-leak fix exists to guard against). A failure there escaped
        # straight to the caller with the already-spawned child NEVER
        # terminated and NEVER reaped -- the one gap the "guaranteed
        # reaping on all paths" claim from earlier rounds did not cover.
        # ``sel`` is pre-declared (typed Optional) so the ``finally``
        # below can safely skip closing it if construction itself never
        # succeeded.
        sel: Optional[selectors.BaseSelector] = None
        try:
            sel = selectors.DefaultSelector()
            sel.register(process.stdout, selectors.EVENT_READ, "stdout")
            sel.register(process.stderr, selectors.EVENT_READ, "stderr")
            open_streams = {"stdout", "stderr"}

            with open(output_file_path, "wb") as out_f:
                while open_streams and not capped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        hit_deadline = True
                        break

                    for key, _ in sel.select(
                        timeout=min(remaining, _SELECT_POLL_SECONDS)
                    ):
                        stream_name = key.data
                        chunk = key.fileobj.read(_READ_CHUNK_BYTES)  # type: ignore[union-attr]
                        if not chunk:
                            sel.unregister(key.fileobj)
                            open_streams.discard(stream_name)
                            continue
                        if stream_name == "stdout":
                            out_f.write(chunk)
                            bytes_written += len(chunk)
                            if bytes_written >= max_output_bytes:
                                capped = True
                                break
                        else:
                            if stderr_bytes_kept < _STDERR_CAPTURE_MAX_BYTES:
                                keep = chunk[
                                    : _STDERR_CAPTURE_MAX_BYTES - stderr_bytes_kept
                                ]
                                stderr_chunks.append(keep)
                                stderr_bytes_kept += len(keep)
        except BaseException:
            # Issue #1601 remediation round 4 (Priority 1): a failure
            # inside supervision itself -- opening output_file_path, or a
            # write raising mid-loop (e.g. ENOSPC, a live possibility now
            # that this path writes up to _MAX_READ_BYTES-scale output per
            # search under fan-out concurrency) -- used to escape straight
            # to the caller with the child NEVER terminated and NEVER
            # reaped, a real hole in the "guaranteed reaping on all paths"
            # claim from an earlier remediation round. Terminate here,
            # unconditionally, before letting the exception propagate.
            self._terminate_process(
                process,
                "MCP-GENERAL-194",
                "MCP-GENERAL-195",
                f"Supervision failed; terminating: {' '.join(command)}",
                "Failed to kill process after supervision failure",
            )
            raise
        finally:
            if sel is not None:
                sel.close()
            # Issue #1601 remediation round 4 (Priority 1): both reviewers
            # independently found -- and Claude's reviewer empirically
            # reproduced under `-W error::ResourceWarning` -- that this
            # method never closed process.stdout/process.stderr on ANY
            # path (capped, natural-EOF, or timeout alike), leaking two
            # open pipe FDs per call to GC finalization. Close both here,
            # unconditionally, regardless of how the try block above
            # exited.
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError as close_error:
                        # Best-effort close only: a stream that fails to
                        # close this way is, by definition, not an open
                        # FD we are leaking -- most likely already closed
                        # by a race with process teardown. Logged (not
                        # silently swallowed) at debug, matching
                        # _terminate_process's identical race-tolerance
                        # rationale for ProcessLookupError/OSError.
                        logger.debug(
                            "Error closing subprocess stream during "
                            "cleanup (likely already closed): %s",
                            close_error,
                        )

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace") or None

        if capped:
            # Deliberate early termination on a legitimate signal is a
            # SUCCESS with a partial result, never ExecutionStatus.ERROR
            # -- _build_completion_result must NOT be reused here since
            # it classifies by process.returncode == 0, and a killed
            # process's negative returncode would wrongly read as an
            # error, causing callers to discard valid partial output.
            self._terminate_process(
                process,
                "MCP-GENERAL-188",
                "MCP-GENERAL-189",
                f"Output cap ({max_output_bytes} bytes) exceeded while "
                f"running; terminating: {' '.join(command)}",
                "Failed to kill output-capped process",
            )
            return SearchExecutionResult(
                status=ExecutionStatus.SUCCESS,
                output_file=output_file_path,
                exit_code=process.returncode,
                timed_out=False,
                output_capped=True,
                stderr_output=stderr_text,
            )

        if hit_deadline:
            self._terminate_process(
                process,
                "MCP-GENERAL-190",
                "MCP-GENERAL-191",
                f"Command timed out after {timeout_seconds}s "
                f"(output-capped path): {' '.join(command)}",
                "Failed to kill timed out output-capped process",
            )
            return self._build_timeout_result(output_file_path, timeout_seconds)

        # Both stdout and stderr hit EOF naturally (open_streams emptied):
        # the process is exiting, or has already exited. Reap it to obtain
        # the real returncode -- near-instant in practice since both its
        # output streams are already closed.
        try:
            process.wait(timeout=_PROCESS_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._terminate_process(
                process,
                "MCP-GENERAL-192",
                "MCP-GENERAL-193",
                f"Process did not exit within {_PROCESS_EXIT_GRACE_SECONDS}s "
                f"after both stdout/stderr closed: {' '.join(command)}",
                "Failed to kill process stuck after stream EOF",
            )
            return self._build_timeout_result(output_file_path, timeout_seconds)

        return self._build_completion_result(process, output_file_path, stderr_text)

    def execute_with_limits_sync(
        self,
        command: List[str],
        working_dir: str,
        timeout_seconds: int,
        output_file_path: str,
    ) -> SearchExecutionResult:
        """
        Execute command synchronously with timeout and file output.

        Story #51: Sync version for use in sync handler contexts.
        This directly calls _run_subprocess() without asyncio wrapping.

        Args:
            command: Command and arguments to execute
            working_dir: Working directory for command execution
            timeout_seconds: Maximum execution time in seconds
            output_file_path: Path to file for capturing output

        Returns:
            SearchExecutionResult with execution status and output file path
        """
        if self._shutdown:
            raise RuntimeError("Executor has been shut down")

        # Ensure output file directory exists
        output_path = Path(output_file_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Run subprocess directly (no async wrapping needed)
        return self._run_subprocess(
            command,
            working_dir,
            output_file_path,
            timeout_seconds,
        )

    def shutdown(self, wait: bool = True, cancel_futures: bool = False):
        """
        Shutdown the executor and clean up resources.

        Args:
            wait: Wait for pending executions to complete
            cancel_futures: Cancel pending futures (Python 3.9+)
        """
        self._shutdown = True
        try:
            # Python 3.9+ supports cancel_futures
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        except TypeError:
            # Fallback for older Python versions
            self._executor.shutdown(wait=wait)

        logger.info(
            "SubprocessExecutor shutdown complete",
            extra={"correlation_id": get_correlation_id()},
        )
