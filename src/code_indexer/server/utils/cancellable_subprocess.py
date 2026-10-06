"""
Cancellable subprocess execution for server background jobs (Bug #1342).

Cancelling a running `activate_repository` job used to be a no-op while the
worker was blocked inside a long `subprocess.run(...)` call (the CoW clone
via the clone backend, or the branch-delta `cidx index` reindex): the call
blocks unbounded, cancel only sets a flag nobody checks, and the work runs
to completion before the job record flips to CANCELLED (leaving artifacts
on disk — a split-brain: job says cancelled, workspace exists).

This module provides ONE shared implementation of the fix: spawn the child
in its own process session/group (`start_new_session=True`) and wait with a
SHORT poll timeout in a loop, checking an injected `cancel_check()` callable
on each timeout. If cancelled, kill the whole process group (SIGTERM, brief
grace period, escalate to SIGKILL) and raise SubprocessCancelledError.

Used by both:
- ActivatedRepoIndexManager._run_subprocess_with_telemetry (the `cidx index`
  branch-delta reindex subprocess)
- LocalCloneBackend.create_clone_at_path (the `cp --reflink=auto` CoW clone
  subprocess)

Bug #1218 invariant: the poll loop itself has NO wall-clock ceiling — the
`poll_interval` only controls how often `cancel_check()` is consulted, not
how long the subprocess is allowed to run. A caller MAY still enforce its
own overall deadline via the optional `timeout` kwarg (e.g. LocalCloneBackend
preserves its existing `cow_clone_timeout` from Bug #1285); when `timeout`
is None (the indexing-path default), the subprocess runs until it finishes
naturally or `cancel_check()` fires — never both a fixed deadline.
"""

import logging
import re
import subprocess
import threading
import time
from typing import Callable, List, Optional

# Bug #2012: the single, layer-neutral group-termination implementation
# (watches the whole group through the grace period, KILLs survivors, never
# signals the caller's own group) -- previously duplicated in this module.
from code_indexer.utils.process_group import (
    terminate_process_group as _terminate_process_group,
)

# Bug #2012 follow-up: an argv or captured output never leaves this module
# (exception messages, logs) without credential redaction.
from code_indexer.utils.credential_redaction import (
    redact_command,
    redact_command_output,
)

logger = logging.getLogger(__name__)

# Bug #1746 M2 (code review finding): a real-word-boundary match for a
# genuine "ERROR" log-level token -- e.g. "ERROR:module:message" or
# " - ERROR - message" -- that will NOT match inside a longer identifier
# like "ERROR_CODES.py" (a naive substring match did: "_" is a word
# character, so there is no boundary between "ERROR" and "_").
_ERROR_TOKEN_PATTERN = re.compile(r"\bERROR\b")

# How often the poll loop checks cancel_check() while waiting for the
# child to finish. Short enough that a cancel is noticed within a few
# seconds; this is NOT a wall-clock deadline on the subprocess itself.
SHORT_POLL_SECONDS = 2.0

# How long to wait for the stdout/stderr drain threads to finish after the
# child has been reaped. Generous but bounded — the pipes are already
# closed by the time we reach this join, so it should return almost
# immediately in practice.
_DRAIN_JOIN_TIMEOUT_SECONDS = 5.0


class SubprocessCancelledError(RuntimeError):
    """Raised when a cancellable subprocess is terminated due to job cancellation."""


def _drain_stream(
    stream, chunks: List[str], stream_label: str, subprocess_args: List[str]
) -> None:
    """Background-thread reader: drains a pipe line-by-line into chunks.

    Runs on its own thread so stdout/stderr are consumed concurrently and
    the child never blocks on a full pipe buffer while the poll loop is
    waiting on proc.wait() (deadlock avoidance).

    Bug #1746 Change 5: an ERROR-level line is also logged via THIS
    module's own logger AS IT ARRIVES -- not only after the child exits
    and the buffered chunks are assembled into the final CompletedProcess.
    This tees the line into the server's existing log-store pipeline
    (whatever already backs admin_logs_query for this process's own
    logger.error() calls) while the child is still running, closing the
    silent-failure window from the original incident (a hung/erroring
    child logged nothing visible to the parent for over two hours).
    """
    try:
        for line in iter(stream.readline, ""):
            if _ERROR_TOKEN_PATTERN.search(line):
                # Bug #2012 follow-up: argv and the echoed line are logged
                # redacted (URL userinfo, token-bearing option values).
                logger.error(
                    "Subprocess %s emitted an ERROR-level %s line while "
                    "still running: %s",
                    redact_command(subprocess_args),
                    stream_label,
                    redact_command_output(line.rstrip(), subprocess_args),
                )
            chunks.append(line)
    finally:
        stream.close()


def run_cancellable_subprocess(
    args: List[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    poll_interval: float = SHORT_POLL_SECONDS,
    timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run args as a subprocess, cooperatively cancellable via cancel_check().

    The child runs in its own process session (start_new_session=True) so
    the ENTIRE process group can be killed on cancellation, not just the
    immediate child (covers e.g. a shell that forks a grandchild).

    Args:
        args: Command and arguments (passed to subprocess.Popen).
        cwd: Working directory for the subprocess. None (the default)
            inherits the calling process's cwd, matching subprocess.run's
            own default when cwd is omitted.
        env: Environment dict, or None to inherit the parent's.
        cancel_check: Zero-arg callable returning True when the owning job
            has been cancelled. Checked once per poll_interval while the
            child is running. None disables cancellation (equivalent to a
            plain blocking subprocess.run wait).
        poll_interval: Seconds between cancel_check() polls. Bug #1218:
            this is NOT a wall-clock deadline on the subprocess -- the loop
            waits indefinitely (bar `timeout`) for the child to finish or
            be cancelled.
        timeout: Optional caller-enforced wall-clock deadline (seconds) for
            the WHOLE subprocess. When exceeded, the process group is
            killed and subprocess.TimeoutExpired is raised, mirroring
            subprocess.run(timeout=...) semantics for callers (e.g. the CoW
            clone step, Bug #1285) that still want a deadline. None means
            no deadline (the Bug #1218 default for the indexing path).

    Returns:
        subprocess.CompletedProcess with returncode/stdout/stderr populated.

    Raises:
        SubprocessCancelledError: cancel_check() returned True.
        subprocess.TimeoutExpired: the optional `timeout` deadline elapsed.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None

    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )

    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    stdout_thread = threading.Thread(
        target=_drain_stream,
        args=(proc.stdout, stdout_chunks, "stdout", args),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(proc.stderr, stderr_chunks, "stderr", args),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    cancelled = False
    timed_out = False
    try:
        while True:
            wait_for = poll_interval
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                wait_for = min(poll_interval, remaining)
            try:
                proc.wait(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    break
                continue

        if cancelled or timed_out:
            _terminate_process_group(proc)
    except BaseException:
        # Bug #2012: anything escaping the loop (e.g. a cancel_check that
        # raises) must never leave the child's process group running.
        if proc.poll() is None:
            _terminate_process_group(proc)
        raise
    finally:
        stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)

    if cancelled:
        raise SubprocessCancelledError(
            f"Subprocess {redact_command(args)!r} cancelled during execution "
            "(job cancellation requested)"
        )
    if timed_out:
        # timed_out is only ever set True when deadline is not None, which
        # itself is only set when timeout is not None -- so timeout is
        # guaranteed a float here. The `or poll_interval` fallback exists
        # purely to satisfy mypy's Optional[float] narrowing; it is never
        # actually exercised.
        raise subprocess.TimeoutExpired(
            cmd=redact_command(args),
            timeout=timeout if timeout is not None else poll_interval,
            output=redact_command_output("".join(stdout_chunks), args),
            stderr=redact_command_output("".join(stderr_chunks), args),
        )

    return subprocess.CompletedProcess(
        args=args,
        returncode=proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )


# run_with_cancel can honour exactly these subprocess.run arguments.
_RUN_WITH_CANCEL_ARGS = frozenset(
    {"cwd", "env", "timeout", "check", "capture_output", "text"}
)


def run_with_cancel(
    args: List[str],
    cancel_check: Optional[Callable[[], bool]],
    **run_kwargs,
) -> "subprocess.CompletedProcess[str]":
    """Bug #2012: drop-in for ``subprocess.run(args, **run_kwargs)`` that the
    owning job can cancel.

    With ``cancel_check=None`` (no owning job, e.g. CLI mode) this IS
    ``subprocess.run(args, **run_kwargs)`` -- same call, same behaviour.
    With a check, the child runs through ``run_cancellable_subprocess`` in
    its own process group: on cancel the whole group is terminated and
    ``SubprocessCancelledError`` is raised; ``timeout`` and ``check=True``
    keep their ``subprocess.run`` meaning (output is always captured as
    text). Arguments that cannot be honoured raise ``TypeError``.
    """
    if cancel_check is None:
        # Bug #2012: the errors leave this module redacted on this path too;
        # `from None` drops the unredacted original from the traceback.
        try:
            return subprocess.run(args, **run_kwargs)
        except subprocess.CalledProcessError as exc:
            raise subprocess.CalledProcessError(
                exc.returncode,
                redact_command(exc.cmd),
                output=redact_command_output(exc.output, exc.cmd),
                stderr=redact_command_output(exc.stderr, exc.cmd),
            ) from None
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired(
                redact_command(exc.cmd),
                exc.timeout,
                output=redact_command_output(exc.output, exc.cmd),
                stderr=redact_command_output(exc.stderr, exc.cmd),
            ) from None

    unsupported = set(run_kwargs) - _RUN_WITH_CANCEL_ARGS
    if unsupported:
        raise TypeError(
            f"run_with_cancel cannot honour {sorted(unsupported)} "
            f"for {redact_command(args)!r}"
        )
    result = run_cancellable_subprocess(
        args,
        cwd=run_kwargs.get("cwd"),
        env=run_kwargs.get("env"),
        cancel_check=cancel_check,
        timeout=run_kwargs.get("timeout"),
    )
    if run_kwargs.get("check") and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            redact_command(args),
            output=redact_command_output(result.stdout, args),
            stderr=redact_command_output(result.stderr, args),
        )
    return result
