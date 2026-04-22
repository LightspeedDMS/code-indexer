"""
TDD tests for MemoryFileLockManager (Story #877 Phase 1b).

Per-memory file lock for cidx-meta/memories/{memory_id}.md writes.
Host-aware staleness: local host -> PID liveness + TTL; foreign host -> TTL only.
"""

import json
import os
import queue
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def locks_root(tmp_path: Path) -> Path:
    root = tmp_path / "locks"
    root.mkdir()
    return root


@pytest.fixture
def manager(locks_root: Path) -> MemoryFileLockManager:
    return MemoryFileLockManager(locks_root)


def _lock_path(locks_root: Path, memory_id: str) -> Path:
    return locks_root / "cidx-meta" / "memories" / f"{memory_id}.lock"


def _write_lock(locks_root: Path, memory_id: str, **overrides) -> None:
    """Preseed a lock file with given metadata for testing staleness scenarios."""
    meta = {
        "owner": "test-owner",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": 30,
    }
    meta.update(overrides)
    lock_path = _lock_path(locks_root, memory_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(meta))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAcquireOnCleanSlate:
    def test_acquire_on_clean_slate_succeeds(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        result = manager.acquire("mem-001", "writer-A")
        assert result is True
        lock_file = _lock_path(locks_root, "mem-001")
        assert lock_file.exists()
        meta = json.loads(lock_file.read_text())
        for key in ("owner", "pid", "hostname", "acquired_at", "ttl_seconds"):
            assert key in meta, f"Missing key: {key}"
        assert meta["owner"] == "writer-A"


class TestAcquireTwiceSameOwner:
    def test_acquire_twice_same_owner_returns_false(
        self, manager: MemoryFileLockManager
    ) -> None:
        assert manager.acquire("mem-002", "writer-A") is True
        # Second acquire on the same id — non-reentrant
        assert manager.acquire("mem-002", "writer-A") is False


class TestReleaseThenReacquire:
    def test_release_then_reacquire_succeeds(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        assert manager.acquire("mem-003", "writer-A") is True
        assert manager.release("mem-003", "writer-A") is True
        lock_file = _lock_path(locks_root, "mem-003")
        assert not lock_file.exists()

        assert manager.acquire("mem-003", "writer-B") is True
        assert lock_file.exists()
        meta = json.loads(lock_file.read_text())
        assert meta["owner"] == "writer-B"


class TestReleaseWrongOwner:
    def test_release_with_wrong_owner_refused(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        assert manager.acquire("mem-004", "writer-A") is True
        result = manager.release("mem-004", "writer-B")
        assert result is False
        # Lock must still exist
        assert _lock_path(locks_root, "mem-004").exists()


class TestReleaseIdempotent:
    def test_release_idempotent_when_file_missing(
        self, manager: MemoryFileLockManager
    ) -> None:
        result = manager.release("mem-never-acquired", "anyone")
        assert result is True


class TestIsLockedReflectsState:
    def test_is_locked_reflects_state(self, manager: MemoryFileLockManager) -> None:
        assert manager.is_locked("mem-005") is False
        assert manager.acquire("mem-005", "writer-A") is True
        assert manager.is_locked("mem-005") is True
        assert manager.release("mem-005", "writer-A") is True
        assert manager.is_locked("mem-005") is False


class TestStaleLocalDeadPid:
    def test_stale_local_dead_pid_evicted_and_acquired(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        # PID 999999999 is guaranteed dead
        _write_lock(
            locks_root,
            "mem-006",
            pid=999999999,
            hostname=socket.gethostname(),
        )
        result = manager.acquire("mem-006", "writer-new")
        assert result is True


class TestForeignHostDeadPidWithinTtl:
    def test_foreign_host_dead_pid_within_ttl_not_evicted(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        # Foreign host lock with dead PID but NOT expired — must NOT be evicted
        _write_lock(
            locks_root,
            "mem-007",
            pid=999999999,
            hostname="other-node",
            ttl_seconds=3600,
            acquired_at=datetime.now(timezone.utc).isoformat(),
        )
        result = manager.acquire("mem-007", "writer-new")
        assert result is False


class TestForeignHostTtlExpiredIsEvicted:
    def test_foreign_host_ttl_expired_is_evicted(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        _write_lock(
            locks_root,
            "mem-008",
            pid=999999999,
            hostname="other-node",
            ttl_seconds=30,
            acquired_at=expired_at,
        )
        result = manager.acquire("mem-008", "writer-new")
        assert result is True


class TestAcquireWritesHostname:
    def test_acquire_writes_hostname_field(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        assert manager.acquire("mem-009", "writer-A") is True
        meta = json.loads(_lock_path(locks_root, "mem-009").read_text())
        assert "hostname" in meta
        assert meta["hostname"] == socket.gethostname()


class TestLockPathLayout:
    def test_lock_path_layout(
        self, manager: MemoryFileLockManager, locks_root: Path
    ) -> None:
        assert manager.acquire("mem-010", "writer-A") is True
        expected = locks_root / "cidx-meta" / "memories" / "mem-010.lock"
        assert expected.exists()


class TestConcurrentThreadsExactlyOneWins:
    def test_concurrent_threads_exactly_one_wins(
        self, manager: MemoryFileLockManager
    ) -> None:
        barrier = threading.Barrier(2)
        results: queue.Queue[bool] = queue.Queue()

        def race() -> None:
            barrier.wait()
            results.put(manager.acquire("mem-concurrent", "racer"))

        t1 = threading.Thread(target=race)
        t2 = threading.Thread(target=race)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        collected = [results.get_nowait(), results.get_nowait()]
        assert collected.count(True) == 1
        assert collected.count(False) == 1


class TestGetLockInfoReturnsMetadata:
    def test_get_lock_info_returns_metadata(
        self, manager: MemoryFileLockManager
    ) -> None:
        assert manager.acquire("mem-011", "writer-A", ttl_seconds=60) is True
        info = manager.get_lock_info("mem-011")
        assert info is not None
        assert info["owner"] == "writer-A"
        assert info["pid"] == os.getpid()
        assert info["hostname"] == socket.gethostname()
        assert "acquired_at" in info
        assert info["ttl_seconds"] == 60


class TestGetLockInfoNoneWhenNotHeld:
    def test_get_lock_info_none_when_not_held(
        self, manager: MemoryFileLockManager
    ) -> None:
        assert manager.get_lock_info("mem-never") is None


class TestPathTraversalSecurity:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "../evil",
            "../../etc/passwd",
            "foo/bar",
            "foo\\bar",
            "foo\x00bar",
        ],
    )
    def test_acquire_rejects_unsafe_memory_id(
        self, manager: MemoryFileLockManager, bad_id: str
    ) -> None:
        with pytest.raises(ValueError):
            manager.acquire(bad_id, "attacker")

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../evil",
            "foo/bar",
            "foo\\bar",
        ],
    )
    def test_release_rejects_unsafe_memory_id(
        self, manager: MemoryFileLockManager, bad_id: str
    ) -> None:
        with pytest.raises(ValueError):
            manager.release(bad_id, "attacker")

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../evil",
            "foo/bar",
        ],
    )
    def test_is_locked_rejects_unsafe_memory_id(
        self, manager: MemoryFileLockManager, bad_id: str
    ) -> None:
        with pytest.raises(ValueError):
            manager.is_locked(bad_id)

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../evil",
            "foo/bar",
        ],
    )
    def test_get_lock_info_rejects_unsafe_memory_id(
        self, manager: MemoryFileLockManager, bad_id: str
    ) -> None:
        with pytest.raises(ValueError):
            manager.get_lock_info(bad_id)
