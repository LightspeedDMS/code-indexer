"""
TDD tests for nfs_self_check.py — Story #877 Phase 1.

Tests validate that run_nfs_atomic_create_self_check correctly detects whether
the hosting filesystem honours O_CREAT|O_EXCL atomicity under concurrent contention.
"""

import os
import threading
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from code_indexer.server.startup.nfs_self_check import (
    NFSAtomicCreateSelfCheckError,
    run_nfs_atomic_create_self_check,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _assert_no_self_check_residue(directory: Path) -> None:
    """Assert that no .self-check* subdirectory remains under directory."""
    leftovers = list(directory.glob(".self-check*"))
    assert leftovers == [], f"Unexpected .self-check* residue: {leftovers}"


def _make_both_succeed_open() -> Callable:
    """Return a fake os.open side_effect where O_EXCL is ignored.

    Both racing threads receive a valid file descriptor for the SAME path,
    simulating a filesystem that does not honour atomic exclusive create.

    State is tracked per target path so the helper works correctly across
    multiple iterations: each new path gets its own first-open tracking, and
    no fd is cached across iterations (each caller opens the file fresh without
    O_EXCL and obtains its own independent descriptor).
    """
    real_open = os.open
    per_path_lock = threading.Lock()
    seen_paths: dict = {}

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        with per_path_lock:
            first = path not in seen_paths
            seen_paths[path] = True

        if first:
            # First caller: open normally but without O_EXCL so it never fails.
            return real_open(path, flags & ~os.O_EXCL, mode)
        else:
            # Second caller: reopen the already-created file for writing —
            # its own independent fd, no dup() and no cached fd across iterations.
            return real_open(path, os.O_WRONLY, mode)

    return fake_open


# ---------------------------------------------------------------------------
# Test 1: happy path — local tmpfs honours exclusive create
# ---------------------------------------------------------------------------


def test_happy_path_local_tmpfs(tmp_path: Path) -> None:
    """Local tmpfs honours O_CREAT|O_EXCL; self-check must complete silently."""
    run_nfs_atomic_create_self_check(tmp_path)


# ---------------------------------------------------------------------------
# Test 2: cleanup on success
# ---------------------------------------------------------------------------


def test_cleanup_removes_temp_subdirectory(tmp_path: Path) -> None:
    """After a successful run no .self-check* directory remains."""
    run_nfs_atomic_create_self_check(tmp_path)
    _assert_no_self_check_residue(tmp_path)


# ---------------------------------------------------------------------------
# Test 3: weak filesystem — raises and always cleans up (parametrized iterations)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("iterations", [3, 5])
def test_weak_filesystem_raises_and_cleans_up(tmp_path: Path, iterations: int) -> None:
    """When both threads get a file descriptor (weak filesystem), error is raised
    and the temp subdirectory is removed regardless of iteration count.
    """
    with patch("os.open", side_effect=_make_both_succeed_open()):
        with pytest.raises(NFSAtomicCreateSelfCheckError):
            run_nfs_atomic_create_self_check(tmp_path, iterations=iterations)

    _assert_no_self_check_residue(tmp_path)


# ---------------------------------------------------------------------------
# Test 4: non-EEXIST failure raises NFSAtomicCreateSelfCheckError
# ---------------------------------------------------------------------------


def test_non_eexist_failure_raises(tmp_path: Path) -> None:
    """When os.open raises PermissionError inside a thread the self-check wraps
    it as NFSAtomicCreateSelfCheckError and the message mentions the unexpected error.
    """
    real_open = os.open
    call_count: dict = {"n": 0}
    lock = threading.Lock()

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        with lock:
            call_count["n"] += 1
            n = call_count["n"]
        if n == 1:
            return real_open(path, flags, mode)
        raise PermissionError("permission denied")

    with patch("os.open", side_effect=fake_open):
        with pytest.raises(NFSAtomicCreateSelfCheckError) as exc_info:
            run_nfs_atomic_create_self_check(tmp_path, iterations=5)

    assert (
        "PermissionError" in str(exc_info.value)
        or "permission" in str(exc_info.value).lower()
    ), f"Message did not mention unexpected error: {exc_info.value}"


# ---------------------------------------------------------------------------
# Test 5: runs multiple iterations
# ---------------------------------------------------------------------------


def test_runs_multiple_iterations(tmp_path: Path) -> None:
    """Confirm that the inner race runs >= 5 iterations (N is an implementation
    detail but must be > 1 to reduce false negatives).
    """
    counter_lock = threading.Lock()
    iteration_count: dict = {"n": 0}
    real_open = os.open

    def counting_open(path: str, flags: int, mode: int = 0o777) -> int:
        if os.O_EXCL & flags:
            with counter_lock:
                iteration_count["n"] += 1
        return real_open(path, flags, mode)

    with patch("os.open", side_effect=counting_open):
        run_nfs_atomic_create_self_check(tmp_path, iterations=5)

    # Each iteration launches 2 threads each calling os.open once with O_EXCL.
    # Total calls = iterations * 2. We assert >= 10 calls (5 iterations * 2 threads).
    assert iteration_count["n"] >= 10, (
        f"Expected >= 10 O_EXCL os.open calls (5 iterations * 2 threads), "
        f"got {iteration_count['n']}"
    )


# ---------------------------------------------------------------------------
# Test 6: non-existent directory raises
# ---------------------------------------------------------------------------


def test_directory_does_not_exist_raises(tmp_path: Path) -> None:
    """Passing a path that does not exist must raise a clear error (either
    NFSAtomicCreateSelfCheckError or FileNotFoundError — either is acceptable,
    but the call must not silently succeed).
    """
    missing = tmp_path / "does_not_exist"
    with pytest.raises((NFSAtomicCreateSelfCheckError, FileNotFoundError, OSError)):
        run_nfs_atomic_create_self_check(missing)
