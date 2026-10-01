"""
Unit tests for describe_lock_holder() (Bug #1842 AC4).

The write-lock contention diagnostics previously named only the FAILED
CALLER's own attempted owner_name (e.g. "(owner='lifecycle_writer')")
regardless of who actually held the lock. describe_lock_holder() reads
the REAL current holder via get_lock_info() and renders owner + hold
duration (when available) so the diagnostic is honest.

Uses a REAL WriteLockManager (file-based, Messi Rule #1 anti-mock) for
the file-backed cases, and a minimal fake object (a test double we
control, rank 3 in the mocking hierarchy) for the DB-backed shape
(AliasLockCoordinator's get_lock_info omits pid/acquired_at), since
constructing a real Postgres-backed AliasLockCoordinator would be
excessive for testing pure string-formatting logic.
"""

from datetime import datetime, timedelta, timezone

from code_indexer.global_repos.write_lock_manager import (
    WriteLockManager,
    describe_lock_holder,
)


class _FakeDbBackedLockManager:
    """Minimal stand-in for AliasLockCoordinator's DB-backed get_lock_info
    shape: owner + owner_token + db_backed, but NO pid/acquired_at."""

    def __init__(self, info):
        self._info = info

    def get_lock_info(self, alias: str):
        return self._info


class TestDescribeLockHolderNoHolder:
    def test_returns_no_holder_message_when_lock_not_held(self, tmp_path):
        manager = WriteLockManager(tmp_path)

        result = describe_lock_holder(manager, "cidx-meta")

        assert "no live holder" in result
        assert "cidx-meta" in result


class TestDescribeLockHolderFileBacked:
    def test_includes_real_owner_and_duration_and_pid(self, tmp_path):
        manager = WriteLockManager(tmp_path)
        acquired = manager.acquire(
            "cidx-meta", owner_name="dependency_map_service", ttl_seconds=3600
        )
        assert acquired is True
        try:
            result = describe_lock_holder(manager, "cidx-meta")

            # Must name the REAL holder, not a hardcoded caller identity.
            assert "dependency_map_service" in result
            assert "pid=" in result
            assert "for " in result
        finally:
            manager.release("cidx-meta", owner_name="dependency_map_service")

    def test_never_names_a_hardcoded_caller_identity_when_real_holder_differs(
        self, tmp_path
    ):
        """Discriminating case: a caller identifying itself as
        'lifecycle_writer' must see the ACTUAL holder
        ('dependency_map_service') in the description, proving the fix
        does not just echo the caller's own attempted owner_name."""
        manager = WriteLockManager(tmp_path)
        acquired = manager.acquire("cidx-meta", owner_name="dependency_map_service")
        assert acquired is True
        try:
            result = describe_lock_holder(manager, "cidx-meta")

            assert "lifecycle_writer" not in result
            assert "dependency_map_service" in result
        finally:
            manager.release("cidx-meta", owner_name="dependency_map_service")


class TestDescribeLockHolderDbBacked:
    def test_handles_missing_acquired_at_and_pid_gracefully(self):
        fake = _FakeDbBackedLockManager(
            {"owner": "dependency_map_service", "owner_token": "abc", "db_backed": True}
        )

        result = describe_lock_holder(fake, "cidx-meta")

        assert "dependency_map_service" in result
        assert "unknown" in result  # hold duration unknown, no acquired_at
        assert "pid=" not in result

    def test_handles_unparseable_acquired_at_gracefully(self):
        fake = _FakeDbBackedLockManager(
            {"owner": "dependency_map_service", "acquired_at": "not-a-timestamp"}
        )

        result = describe_lock_holder(fake, "cidx-meta")

        assert "dependency_map_service" in result
        assert "unknown" in result


class TestDescribeLockHolderDuration:
    def test_duration_reflects_elapsed_time_from_acquired_at(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=47)).isoformat()
        fake = _FakeDbBackedLockManager(
            {"owner": "dependency_map_service", "acquired_at": past, "pid": 12345}
        )

        result = describe_lock_holder(fake, "cidx-meta")

        assert "dependency_map_service" in result
        assert "pid=12345" in result
        import re

        match = re.search(r"for (\d+\.\d)s", result)
        assert match is not None, result
        elapsed = float(match.group(1))
        assert 46.0 <= elapsed <= 50.0
