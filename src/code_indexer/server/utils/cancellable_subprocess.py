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
import os
import signal
import subprocess
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# How often the poll loop checks cancel_check() while waiting for the
# child to finish. Short enough that a cancel is noticed within a few
# seconds; this is NOT a wall-clock deadline on the subprocess itself.
SHORT_POLL_SECONDS = 2.0

# Grace period after SIGTERM before escalating to SIGKILL.
SIGTERM_GRACE_SECONDS = 2.0

# How long to wait for the stdout/stderr drain threads to finish after the
# child has been reaped. Generous but bounded — the pipes are already
# closed by the time we reach this join, so it should return almost
# immediately in practice.
_DRAIN_JOIN_TIMEOUT_SECONDS = 5.0


class SubprocessCancelledError(RuntimeError):
    """Raised when a cancellable subprocess is terminated due to job cancellation."""


def _terminate_process_group(proc: "subprocess.Popen[str]") -> None:
    """SIGTERM the process group, wait a grace period, escalate to SIGKILL.

    Always blocks until the child is reaped so proc.returncode is populated
    when this returns.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        # Process already gone.
        proc.wait()
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return

    try:
        proc.wait(timeout=SIGTERM_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _drain_stream(stream, chunks: List[str]) -> None:
    """Background-thread reader: drains a pipe line-by-line into chunks.

    Runs on its own thread so stdout/stderr are consumed concurrently and
    the child never blocks on a full pipe buffer while the poll loop is
    waiting on proc.wait() (deadlock avoidance).
    """
    try:
        for line in iter(stream.readline, ""):
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
        target=_drain_stream, args=(proc.stdout, stdout_chunks), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain_stream, args=(proc.stderr, stderr_chunks), daemon=True
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
    finally:
        stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)

    if cancelled:
        raise SubprocessCancelledError(
            f"Subprocess {args!r} cancelled during execution "
            "(job cancellation requested)"
        )
    if timed_out:
        # timed_out is only ever set True when deadline is not None, which
        # itself is only set when timeout is not None -- so timeout is
        # guaranteed a float here. The `or poll_interval` fallback exists
        # purely to satisfy mypy's Optional[float] narrowing; it is never
        # actually exercised.
        raise subprocess.TimeoutExpired(
            cmd=args, timeout=timeout if timeout is not None else poll_interval
        )

    return subprocess.CompletedProcess(
        args=args,
        returncode=proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )
