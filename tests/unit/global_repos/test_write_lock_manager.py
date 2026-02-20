"""
Unit tests for WriteLockManager - file-based named write locks (Story #230).

Tests the core API: acquire, release, is_locked, get_lock_info.

TDD RED phase: Tests written BEFORE production code. All tests expected to FAIL
until WriteLockManager is implemented in
src/code_indexer/global_repos/write_lock_manager.py.
"""

import errno
import json
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_dir(tmp_path):
    """Provide a temporary directory for lock files."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def manager(lock_dir):
    """Create a fresh WriteLockManager for each test."""
    from code_indexer.global_repos.write_lock_manager import WriteLockManager

    return WriteLockManager(golden_repos_dir=lock_dir)


# ---------------------------------------------------------------------------
# AC1 — acquire creates lock file with correct JSON fields
# ---------------------------------------------------------------------------


class TestAcquireCreatesLockFile:
    """AC1: Lock file is created with owner metadata on acquire."""

    def test_acquire_creates_lock_file(self, manager, lock_dir):
        """acquire() creates a .lock file in golden_repos_dir/.locks/{alias}.lock."""
        result = manager.acquire("my-repo", "test-owner")

        assert result is True, "First acquire must succeed"

        lock_file = lock_dir / ".locks" / "my-repo.lock"
        assert lock_file.exists(), f"Lock file must exist at {lock_file}"

        manager.release("my-repo", "test-owner")

    def test_acquire_writes_correct_json_fields(self, manager, lock_dir):
        """Lock file JSON must contain owner, pid, acquired_at, ttl_seconds."""
        before = datetime.now(timezone.utc)
        result = manager.acquire("my-repo", "test-owner", ttl_seconds=3600)
        after = datetime.now(timezone.utc)

        assert result is True

        lock_file = lock_dir / ".locks" / "my-repo.lock"
        content = json.loads(lock_file.read_text())

        assert content["owner"] == "test-owner", (
            f"Lock file must record owner. Got: {content.get('owner')}"
        )
        assert content["pid"] == os.getpid(), (
            f"Lock file must record current PID. Got: {content.get('pid')}"
        )
        assert content["ttl_seconds"] == 3600, (
            f"Lock file must record ttl_seconds=3600. Got: {content.get('ttl_seconds')}"
        )

        acquired_at = datetime.fromisoformat(content["acquired_at"])
        assert before <= acquired_at <= after, (
            f"acquired_at ({acquired_at}) must be between before ({before}) and after ({after})"
        )

        manager.release("my-repo", "test-owner")

    def test_acquire_creates_locks_directory_if_missing(self, manager, lock_dir):
        """acquire() must create the .locks directory if it does not exist."""
        locks_dir = lock_dir / ".locks"
        assert not locks_dir.exists(), "Precondition: .locks dir must not exist yet"

        result = manager.acquire("my-repo", "test-owner")

        assert result is True
        assert locks_dir.exists(), ".locks directory must be created by acquire()"

        manager.release("my-repo", "test-owner")

    def test_acquire_default_ttl_is_3600(self, manager, lock_dir):
        """Default TTL must be 3600 seconds when not specified."""
        manager.acquire("my-repo", "test-owner")

        lock_file = lock_dir / ".locks" / "my-repo.lock"
        content = json.loads(lock_file.read_text())

        assert content["ttl_seconds"] == 3600, (
            f"Default TTL must be 3600. Got: {content.get('ttl_seconds')}"
        )

        manager.release("my-repo", "test-owner")


# ---------------------------------------------------------------------------
# AC2 — second acquire fails when lock is held by live owner
# ---------------------------------------------------------------------------


class TestSecondAcquireFails:
    """AC2: Second acquire attempt fails when lock is held by live owner."""

    def test_second_acquire_returns_false_when_held(self, manager):
        """acquire() returns False when the same alias is already locked."""
        first = manager.acquire("my-repo", "owner-A")
        assert first is True, "Precondition: first acquire must succeed"

        second = manager.acquire("my-repo", "owner-B")

        assert second is False, (
            "acquire() must return False when lock is already held by live PID"
        )

        manager.release("my-repo", "owner-A")

    def test_different_aliases_are_independent(self, manager):
        """Locks on different aliases do not interfere with each other."""
        first = manager.acquire("repo-A", "owner")
        second = manager.acquire("repo-B", "owner")

        assert first is True, "First alias must lock successfully"
        assert second is True, "Second alias must lock independently"

        manager.release("repo-A", "owner")
        manager.release("repo-B", "owner")

    def test_acquire_succeeds_after_release(self, manager):
        """After release, acquire() returns True for the same alias."""
        manager.acquire("my-repo", "owner-A")
        manager.release("my-repo", "owner-A")

        result = manager.acquire("my-repo", "owner-B")

        assert result is True, "acquire() must succeed after release()"

        manager.release("my-repo", "owner-B")


# ---------------------------------------------------------------------------
# AC3 — stale lock from dead PID is forcibly released on acquire
# ---------------------------------------------------------------------------


class TestStaleLockDeadPID:
    """AC3: Stale lock from dead PID is forcibly released on acquire."""

    def test_dead_pid_lock_is_evicted_and_new_lock_acquired(self, manager, lock_dir):
        """
        If LOCK_FILE references a dead PID, acquire() deletes it and creates a new one.

        Simulates dead PID by patching os.kill to raise OSError(errno.ESRCH).
        """
        dead_pid = 9999999
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "dead-process",
            "pid": dead_pid,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,
        }))

        def mock_os_kill(pid, sig):
            if pid == dead_pid:
                raise OSError(errno.ESRCH, "No such process")
            # Allow os.kill for other PIDs (e.g. current process)
            return None

        with patch("code_indexer.global_repos.write_lock_manager.os.kill", side_effect=mock_os_kill):
            result = manager.acquire("my-repo", "new-owner")

        assert result is True, (
            "acquire() must evict stale lock from dead PID and succeed"
        )

        content = json.loads(lock_file.read_text())
        assert content["owner"] == "new-owner", (
            f"New lock must have owner 'new-owner'. Got: {content.get('owner')}"
        )

        manager.release("my-repo", "new-owner")

    def test_live_pid_lock_is_not_evicted(self, manager, lock_dir):
        """
        If LOCK_FILE references a live PID (os.kill(pid, 0) succeeds), acquire() returns False.
        """
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "live-process",
            "pid": os.getpid(),  # current process — definitely alive
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,
        }))

        result = manager.acquire("my-repo", "intruder")

        assert result is False, (
            "acquire() must return False when lock is held by a live PID"
        )

        assert lock_file.exists(), "Lock file must not be deleted for live PID"


# ---------------------------------------------------------------------------
# AC4 — TTL-expired lock is forcibly released on acquire
# ---------------------------------------------------------------------------


class TestStaleLockTTLExpired:
    """AC4: TTL-expired lock is forcibly released on acquire."""

    def test_ttl_expired_lock_is_evicted_and_new_lock_acquired(self, manager, lock_dir):
        """
        If acquired_at + ttl_seconds < now, acquire() deletes it and creates a new one.
        """
        expired_time = datetime.now(timezone.utc) - timedelta(hours=2)
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "expired-process",
            "pid": os.getpid(),  # live PID but TTL expired
            "acquired_at": expired_time.isoformat(),
            "ttl_seconds": 3600,  # 1-hour TTL — expired 1 hour ago
        }))

        result = manager.acquire("my-repo", "new-owner")

        assert result is True, (
            "acquire() must evict TTL-expired lock and succeed"
        )

        content = json.loads(lock_file.read_text())
        assert content["owner"] == "new-owner", (
            f"New lock must have owner 'new-owner'. Got: {content.get('owner')}"
        )

        manager.release("my-repo", "new-owner")

    def test_non_expired_lock_is_not_evicted_by_ttl(self, manager, lock_dir):
        """
        If acquired_at + ttl_seconds > now, acquire() does NOT evict due to TTL.
        (PID is live too, so no staleness.)
        """
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "current-process",
            "pid": os.getpid(),  # live PID
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,  # non-expired
        }))

        result = manager.acquire("my-repo", "intruder")

        assert result is False, (
            "acquire() must not evict a non-expired, live-PID lock"
        )


# ---------------------------------------------------------------------------
# AC5 — release() refuses to delete lock owned by different caller
# ---------------------------------------------------------------------------


class TestRelease:
    """AC5: release() refuses to delete a lock owned by a different caller."""

    def test_release_with_wrong_owner_returns_false(self, manager, lock_dir):
        """release() must return False and leave file intact when owner doesn't match."""
        manager.acquire("my-repo", "correct-owner")

        result = manager.release("my-repo", "wrong-owner")

        assert result is False, (
            "release() must return False when caller is not the lock owner"
        )

        lock_file = lock_dir / ".locks" / "my-repo.lock"
        assert lock_file.exists(), "Lock file must NOT be deleted by wrong owner"

        manager.release("my-repo", "correct-owner")

    def test_release_with_correct_owner_returns_true(self, manager, lock_dir):
        """release() returns True and removes the lock file when owner matches."""
        manager.acquire("my-repo", "correct-owner")

        result = manager.release("my-repo", "correct-owner")

        assert result is True, "release() must return True when owner matches"

        lock_file = lock_dir / ".locks" / "my-repo.lock"
        assert not lock_file.exists(), "Lock file must be deleted after successful release()"

    def test_release_on_absent_file_returns_true(self, manager):
        """release() returns True (idempotent) when lock file does not exist."""
        result = manager.release("nonexistent-repo", "any-owner")

        assert result is True, (
            "release() must return True when lock file doesn't exist (idempotent)"
        )


# ---------------------------------------------------------------------------
# AC8 — is_locked() returns False when no file, True when live lock
# ---------------------------------------------------------------------------


class TestIsLocked:
    """AC8: is_locked returns False when no lock file exists, True when live lock present."""

    def test_is_locked_returns_false_when_no_file(self, manager):
        """is_locked() returns False when lock file does not exist."""
        result = manager.is_locked("nonexistent-repo")

        assert result is False, "is_locked() must return False when lock file is absent"

    def test_is_locked_returns_true_when_live_lock_present(self, manager):
        """is_locked() returns True when a live lock file exists."""
        manager.acquire("my-repo", "owner")

        result = manager.is_locked("my-repo")

        assert result is True, "is_locked() must return True when lock is held"

        manager.release("my-repo", "owner")

    def test_is_locked_returns_false_after_release(self, manager):
        """is_locked() returns False after the lock is released."""
        manager.acquire("my-repo", "owner")
        manager.release("my-repo", "owner")

        result = manager.is_locked("my-repo")

        assert result is False, "is_locked() must return False after release()"

    def test_is_locked_evicts_dead_pid_lock_and_returns_false(self, manager, lock_dir):
        """is_locked() evicts a dead-PID lock and returns False."""
        dead_pid = 9999999
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "dead-process",
            "pid": dead_pid,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,
        }))

        def mock_os_kill(pid, sig):
            if pid == dead_pid:
                raise OSError(errno.ESRCH, "No such process")
            return None

        with patch("code_indexer.global_repos.write_lock_manager.os.kill", side_effect=mock_os_kill):
            result = manager.is_locked("my-repo")

        assert result is False, "is_locked() must return False for dead-PID lock"
        assert not lock_file.exists(), "is_locked() must delete stale lock file"

    def test_is_locked_evicts_ttl_expired_lock_and_returns_false(self, manager, lock_dir):
        """is_locked() evicts a TTL-expired lock and returns False."""
        expired_time = datetime.now(timezone.utc) - timedelta(hours=2)
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "old-process",
            "pid": os.getpid(),  # live PID but TTL expired
            "acquired_at": expired_time.isoformat(),
            "ttl_seconds": 3600,
        }))

        result = manager.is_locked("my-repo")

        assert result is False, "is_locked() must return False for TTL-expired lock"
        assert not lock_file.exists(), "is_locked() must delete TTL-expired lock file"

    def test_is_locked_returns_false_for_corrupt_lock_no_pid_no_timestamp(self, manager, lock_dir):
        """
        is_locked() must treat a corrupt lock (no pid, no acquired_at) as stale and return False.

        Finding 2: _is_stale() used to skip both PID and TTL checks when neither field was
        present, returning False (not stale) — leaving the corrupt lock alive forever.
        A lock with only {"owner": "someone"} cannot be validated and must be evicted.
        """
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({"owner": "someone"}))  # no pid, no acquired_at

        result = manager.is_locked("my-repo")

        assert result is False, (
            "is_locked() must return False for corrupt lock with no pid and no acquired_at"
        )
        assert not lock_file.exists(), (
            "is_locked() must delete the corrupt lock file"
        )


# ---------------------------------------------------------------------------
# get_lock_info() — returns None when unlocked, dict when locked
# ---------------------------------------------------------------------------


class TestGetLockInfo:
    """get_lock_info() returns None when unlocked, dict with lock metadata when locked."""

    def test_get_lock_info_returns_none_when_no_lock(self, manager):
        """get_lock_info() returns None when lock file does not exist."""
        result = manager.get_lock_info("nonexistent-repo")

        assert result is None, "get_lock_info() must return None when lock file is absent"

    def test_get_lock_info_returns_dict_when_locked(self, manager):
        """get_lock_info() returns a dict with lock metadata when lock is held."""
        manager.acquire("my-repo", "my-owner", ttl_seconds=1800)

        info = manager.get_lock_info("my-repo")

        assert info is not None, "get_lock_info() must return dict when lock is held"
        assert info["owner"] == "my-owner", (
            f"get_lock_info() owner must be 'my-owner'. Got: {info.get('owner')}"
        )
        assert info["pid"] == os.getpid(), (
            f"get_lock_info() pid must be current PID. Got: {info.get('pid')}"
        )
        assert info["ttl_seconds"] == 1800, (
            f"get_lock_info() ttl_seconds must be 1800. Got: {info.get('ttl_seconds')}"
        )
        assert "acquired_at" in info, "get_lock_info() must include acquired_at"

        manager.release("my-repo", "my-owner")

    def test_get_lock_info_returns_none_after_release(self, manager):
        """get_lock_info() returns None after the lock is released."""
        manager.acquire("my-repo", "my-owner")
        manager.release("my-repo", "my-owner")

        result = manager.get_lock_info("my-repo")

        assert result is None, "get_lock_info() must return None after release()"

    def test_get_lock_info_returns_none_for_stale_lock(self, manager, lock_dir):
        """get_lock_info() returns None and evicts stale (dead-PID) lock."""
        dead_pid = 9999999
        locks_dir = lock_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / "my-repo.lock"
        lock_file.write_text(json.dumps({
            "owner": "dead-process",
            "pid": dead_pid,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 3600,
        }))

        def mock_os_kill(pid, sig):
            if pid == dead_pid:
                raise OSError(errno.ESRCH, "No such process")
            return None

        with patch("code_indexer.global_repos.write_lock_manager.os.kill", side_effect=mock_os_kill):
            result = manager.get_lock_info("my-repo")

        assert result is None, "get_lock_info() must return None for dead-PID lock"
