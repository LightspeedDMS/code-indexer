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
            file_count = len([line for line in ls_result.stdout.splitlines() if line.strip()])
        else:
            logger.warning(
                "gather_repo_metrics: git ls-files failed in %s (exit %d): %s",
                repo_str,
                ls_result.returncode,
                ls_result.stderr.strip(),
            )
            file_count = 0
    except Exception as e:
        logger.warning("gather_repo_metrics: failed to count tracked files in %s: %s", repo_str, e)
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
        logger.warning("gather_repo_metrics: failed to count commits in %s: %s", repo_str, e)
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
) -> None:
    """
    Run a command with Popen, reading JSON progress lines from stdout.

    JSON progress lines ({"current": N, "total": M, "info": "..."}) are parsed
    and forwarded to progress_callback as globally-mapped phase percentages via
    the allocator.  Non-JSON lines are accumulated in all_stdout for error
    reporting but not parsed.  Stderr is captured for error details.

    On non-zero exit, raises GoldenRepoError with captured stderr.

    This is the shared implementation extracted from golden_repo_manager.py
    (PATH B) for reuse in all indexing paths (PATH A, C, D, E).

    Args:
        command: Command list to execute via subprocess.Popen
        phase_name: Phase name in the allocator (e.g., "semantic", "temporal")
        allocator: ProgressPhaseAllocator with calculate_weights already called
        progress_callback: Optional callable(pct, phase=..., detail=...) for updates
        all_stdout: Mutable list — accumulated stdout lines are appended here
        all_stderr: Mutable list — accumulated stderr lines are appended here
        cwd: Working directory for the subprocess (None = inherit)
        error_label: Human-readable label for error messages (defaults to phase_name)
    """
    from code_indexer.services.progress_phase_allocator import parse_progress_line

    if error_label is None:
        error_label = phase_name

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Report phase start (coarse marker before any lines arrive)
    if progress_callback is not None:
        global_start = int(allocator.phase_start(phase_name))
        progress_callback(
            global_start,
            phase=phase_name,
            detail=f"{phase_name}: starting...",
        )

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

    for line in process.stdout:
        all_stdout.append(line)
        parsed = parse_progress_line(line)
        if parsed is not None and progress_callback is not None:
            global_pct = int(
                allocator.map_phase_progress(
                    phase_name, parsed["current"], parsed["total"]
                )
            )
            progress_callback(
                global_pct,
                phase=phase_name,
                detail=parsed.get("info", ""),
            )
        # Non-JSON lines: already accumulated in all_stdout, skip parsing

    process.wait()
    stderr_thread.join(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    stderr_output = "".join(stderr_lines)
    all_stderr.append(stderr_output)

    if process.returncode != 0:
        error_details = stderr_output or f"Exit code {process.returncode}"
        raise IndexingSubprocessError(f"Failed to {error_label}: {error_details}")
