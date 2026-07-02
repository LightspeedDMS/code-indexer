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
    )
"""

import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


class IndexingSubprocessError(Exception):
    """
    Raised by run_with_popen_progress when the subprocess exits non-zero.

    Callers that need a domain-specific error (e.g. GoldenRepoError,
    RuntimeError) should catch this and re-raise.  Using a local error type
    avoids importing from consumer modules (golden_repo_manager, etc.) which
    would create circular dependencies.
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
    timeout: Optional[float] = None,
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
        timeout: Optional timeout in seconds. A watchdog thread kills the process
                 after this many seconds and IndexingSubprocessError is raised.

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

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # Report phase start (coarse marker before any lines arrive)
    global_start = int(allocator.phase_start(phase_name))
    _emit(global_start, phase=phase_name, detail=f"{phase_name}: starting...")

    # Read stdout line by line
    if process.stdout is None:
        raise IndexingSubprocessError(
            f"Failed to {error_label}: subprocess stdout pipe was not created"
        )

    # Drain stderr in background thread to prevent deadlock if child
    # writes >64KB to stderr while parent is blocked reading stdout.
    stderr_lines: List[str] = []

    def _drain_stderr() -> None:
        if process.stderr:
            stderr_lines.extend(process.stderr.readlines())

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Watchdog thread enforces timeout independent of stdout line production.
    # This ensures the process is killed even if it produces no output.
    timed_out = threading.Event()

    if timeout is not None:

        def _watchdog() -> None:
            if not timed_out.wait(timeout=timeout):
                # Event was not set within timeout — process still running, kill it.
                timed_out.set()
                process.kill()

        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()
    else:
        watchdog_thread = None

    for line in process.stdout:
        all_stdout.append(line)
        parsed = parse_progress_line(line)
        if parsed is not None:
            global_pct = int(
                allocator.map_phase_progress(
                    phase_name, parsed["current"], parsed["total"]
                )
            )
            _emit(global_pct, phase=phase_name, detail=parsed.get("info", ""))
        # Non-JSON lines: already accumulated in all_stdout, skip parsing

    # Signal watchdog that process finished normally (prevents spurious kill).
    timed_out.set()

    process.wait()
    stderr_thread.join(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    if watchdog_thread is not None:
        watchdog_thread.join(timeout=GIT_COMMAND_TIMEOUT_SECONDS)

    stderr_output = "".join(stderr_lines)
    all_stderr.append(stderr_output)

    # Check for timeout after process.wait() — the watchdog may have killed it.
    if timeout is not None and process.returncode == -9:
        timeout_msg = f"Timed out after {timeout}s"
        all_stderr.append(timeout_msg)
        raise IndexingSubprocessError(f"Failed to {error_label}: {timeout_msg}")

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
