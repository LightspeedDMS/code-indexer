"""
Unit tests for host-aware WriteLockManager extension (Story #877 Phase 1).

Tests verify:
- Local-host locks include hostname in metadata
- PID liveness check applied only when hostname matches local host
- Foreign-host locks use TTL-only staleness (never evict based on PID)
- Backward compat: locks without hostname field treated as local
"""

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from code_indexer.global_repos.write_lock_manager import WriteLockManager

# ---------------------------------------------------------------------------
# Module-level constants — no magic numbers in test bodies
# ---------------------------------------------------------------------------
DEAD_PID = 999999999  # No real process will ever have this PID
DEFAULT_TTL_SECONDS = 3600  # 1 hour — standard lock TTL
EXPIRED_AGE_SECONDS = 7200  # 2 hours — guarantees TTL has passed
ACQUIRE_TTL_SECONDS = 60  # Short TTL used when acquiring in tests
FOREIGN_HOSTNAME = "other-node.example.com"  # Distinct from socket.gethostname()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_lock_file(
    lock_dir: Path,
    alias: str,
    owner: str,
    pid: int,
    hostname: Optional[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    acquired_at: Optional[datetime] = None,
) -> Path:
    """Write a lock metadata JSON file under lock_dir/.locks/{alias}.lock.

    Returns the Path of the written lock file.
    hostname=None omits the hostname key entirely (simulates pre-#877 format).
    """
    locks_dir = lock_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{alias}.lock"

    if acquired_at is None:
        acquired_at = datetime.now(timezone.utc)

    metadata: Dict[str, Any] = {
        "owner": owner,
        "pid": pid,
        "acquired_at": acquired_at.isoformat(),
        "ttl_seconds": ttl_seconds,
    }
    if hostname is not None:
        metadata["hostname"] = hostname

    lock_path.write_text(json.dumps(metadata))
    return lock_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_dir(tmp_path):
    """Temporary directory acting as the golden-repos root for lock testing."""
    d = tmp_path / "golden-repos"
    d.mkdir()
    return d


@pytest.fixture
def manager(lock_dir):
    """WriteLockManager instance rooted at lock_dir."""
    return WriteLockManager(lock_dir)


# ---------------------------------------------------------------------------
# Local-host lock behaviour
# ---------------------------------------------------------------------------


class TestLocalHostAcquire:
    """Locks acquired on the local host include hostname in metadata."""

    def test_acquire_writes_hostname_to_metadata(self, manager, lock_dir):
        """Acquired lock JSON must contain 'hostname' equal to socket.gethostname()."""
        result = manager.acquire("my-alias", "test-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is True

        lock_file = lock_dir / ".locks" / "my-alias.lock"
        assert lock_file.exists()
        content = json.loads(lock_file.read_text())

        assert "hostname" in content
        assert content["hostname"] == socket.gethostname()

    def test_acquire_writes_pid_to_metadata(self, manager, lock_dir):
        """Acquired lock JSON must still contain 'pid' field."""
        manager.acquire("alias-pid", "owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        lock_file = lock_dir / ".locks" / "alias-pid.lock"
        content = json.loads(lock_file.read_text())
        assert content["pid"] == os.getpid()

    def test_local_host_dead_pid_evicted(self, manager, lock_dir):
        """A lock with local hostname and a dead PID is treated as stale and evicted."""
        _write_lock_file(lock_dir, "dead-pid", "dead-owner", DEAD_PID, socket.gethostname())

        # Acquire should succeed by evicting the stale local lock
        result = manager.acquire("dead-pid", "new-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is True

    def test_local_host_live_pid_not_evicted(self, manager, lock_dir):
        """A lock with local hostname and a live PID (current process) is not evicted."""
        _write_lock_file(lock_dir, "live-pid", "live-owner", os.getpid(), socket.gethostname())

        # Acquire should fail because lock holder is alive
        result = manager.acquire("live-pid", "new-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is False


# ---------------------------------------------------------------------------
# Foreign-host lock behaviour
# ---------------------------------------------------------------------------


class TestForeignHostLock:
    """Foreign-host locks use TTL-only staleness — PID is never consulted."""

    def test_foreign_host_dead_pid_not_evicted_before_ttl(self, manager, lock_dir):
        """A lock with a foreign hostname and dead PID must NOT be evicted before TTL expires."""
        _write_lock_file(lock_dir, "foreign-dead", "foreign-owner", DEAD_PID, FOREIGN_HOSTNAME)

        # Must NOT evict — foreign host lock, TTL not expired
        result = manager.acquire("foreign-dead", "local-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is False, "Foreign-host lock with dead PID must not be evicted before TTL"

    def test_foreign_host_ttl_expired_is_evicted(self, manager, lock_dir):
        """A foreign-host lock with expired TTL is evicted via TTL-only staleness."""
        past_time = datetime.now(timezone.utc) - timedelta(seconds=EXPIRED_AGE_SECONDS)
        _write_lock_file(
            lock_dir,
            "foreign-expired",
            "foreign-owner",
            DEAD_PID,
            FOREIGN_HOSTNAME,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            acquired_at=past_time,
        )

        # TTL expired — should be evictable
        result = manager.acquire("foreign-expired", "local-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is True, "Foreign-host lock with expired TTL should be evicted"

    def test_foreign_host_live_ttl_not_evicted(self, manager, lock_dir):
        """A foreign-host lock within TTL window is respected even with dead local PID."""
        _write_lock_file(lock_dir, "foreign-live", "foreign-owner", DEAD_PID, FOREIGN_HOSTNAME)

        result = manager.acquire("foreign-live", "local-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is False

    def test_is_stale_foreign_host_dead_pid_not_stale_within_ttl(self, manager):
        """_is_stale must return False for foreign-host lock with dead PID but live TTL."""
        content = {
            "owner": "foreign-owner",
            "pid": DEAD_PID,
            "hostname": FOREIGN_HOSTNAME,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
        # Must NOT be considered stale (PID irrelevant for foreign host)
        assert manager._is_stale(content) is False

    def test_is_stale_foreign_host_expired_ttl_is_stale(self, manager):
        """_is_stale must return True for foreign-host lock when TTL has expired."""
        past_time = datetime.now(timezone.utc) - timedelta(seconds=EXPIRED_AGE_SECONDS)
        content = {
            "owner": "foreign-owner",
            "pid": DEAD_PID,
            "hostname": FOREIGN_HOSTNAME,
            "acquired_at": past_time.isoformat(),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
        assert manager._is_stale(content) is True

    def test_is_stale_local_host_dead_pid_is_stale(self, manager):
        """_is_stale must return True for local-host lock when PID is dead."""
        content = {
            "owner": "local-owner",
            "pid": DEAD_PID,
            "hostname": socket.gethostname(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
        assert manager._is_stale(content) is True


# ---------------------------------------------------------------------------
# Backward compatibility (pre-Story-#877 lock format — no hostname field)
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Locks without hostname field are treated as local (pre-Story-#877 format)."""

    def test_old_format_dead_pid_treated_as_local_evicted(self, manager, lock_dir):
        """Lock without hostname field with dead PID is treated as local — gets evicted."""
        # hostname=None omits the field entirely
        _write_lock_file(lock_dir, "old-format", "old-owner", DEAD_PID, hostname=None)

        # Should be evicted (treated as local — dead PID check applies)
        result = manager.acquire("old-format", "new-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is True, "Old-format lock with dead PID should be treated as local and evicted"

    def test_old_format_live_pid_not_evicted(self, manager, lock_dir):
        """Lock without hostname field with live PID is respected (treated as local)."""
        _write_lock_file(lock_dir, "old-live", "old-owner", os.getpid(), hostname=None)

        result = manager.acquire("old-live", "new-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        assert result is False, "Old-format lock with live PID must not be evicted"

    def test_is_stale_no_hostname_dead_pid_is_stale(self, manager):
        """_is_stale must treat no-hostname lock with dead PID as stale (backward compat)."""
        content = {
            "owner": "old-owner",
            "pid": DEAD_PID,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
        assert manager._is_stale(content) is True

    def test_is_stale_no_hostname_live_pid_not_stale(self, manager):
        """_is_stale must NOT mark no-hostname lock with live PID as stale."""
        content = {
            "owner": "old-owner",
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
        assert manager._is_stale(content) is False


# ---------------------------------------------------------------------------
# get_lock_info includes hostname
# ---------------------------------------------------------------------------


class TestGetLockInfoHostname:
    """get_lock_info returns hostname in the metadata dict when available."""

    def test_get_lock_info_includes_hostname(self, manager):
        """After acquiring, get_lock_info must include the hostname key."""
        manager.acquire("info-alias", "info-owner", ttl_seconds=ACQUIRE_TTL_SECONDS)
        info = manager.get_lock_info("info-alias")
        assert info is not None
        assert "hostname" in info
        assert info["hostname"] == socket.gethostname()

    def test_get_lock_info_old_format_no_hostname(self, manager, lock_dir):
        """get_lock_info for old-format lock (no hostname) returns dict without hostname key."""
        _write_lock_file(lock_dir, "old-info", "old-owner", os.getpid(), hostname=None)

        info = manager.get_lock_info("old-info")
        assert info is not None
        assert info["owner"] == "old-owner"
        # Old format has no hostname key — must not crash
        assert "hostname" not in info
