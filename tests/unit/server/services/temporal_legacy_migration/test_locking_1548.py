"""Issue #1548 blocker 4: refresh-safe write-lock guard tests."""

from typing import List

import pytest

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.temporal_legacy_migration.locking import (
    RefreshInProgressError,
    WriteLockHeldError,
    guarded_by_refresh_lock,
)


class _FakeWriteLockManager:
    def __init__(self):
        self.locked = set()
        self.acquire_calls = []
        self.release_calls = []

    def acquire(self, alias, *, owner_name):
        self.acquire_calls.append((alias, owner_name))
        if alias in self.locked:
            return False
        self.locked.add(alias)
        return True

    def release(self, alias, *, owner_name):
        self.release_calls.append((alias, owner_name))
        self.locked.discard(alias)
        return True


class _FakeRefreshScheduler:
    def __init__(self, *, refresh_in_progress: bool = False):
        self.write_lock_manager = _FakeWriteLockManager()
        self._refresh_in_progress = refresh_in_progress
        self.check_calls: List[str] = []

    def check_refresh_not_in_progress(self, alias):
        self.check_calls.append(alias)
        if self._refresh_in_progress:
            raise DuplicateJobError("global_repo_refresh", alias, "job-123")

    def release_write_lock(self, alias, *, owner_name):
        released = self.write_lock_manager.release(alias, owner_name=owner_name)
        assert released, f"release_write_lock: owner mismatch for {alias!r}"


def test_guard_acquires_and_releases_lock_on_success():
    scheduler = _FakeRefreshScheduler()
    entered = False
    with guarded_by_refresh_lock(scheduler, "demo"):
        entered = True
        assert "demo" in scheduler.write_lock_manager.locked
    assert entered
    assert "demo" not in scheduler.write_lock_manager.locked


def test_guard_releases_lock_even_when_body_raises():
    scheduler = _FakeRefreshScheduler()
    with pytest.raises(RuntimeError):
        with guarded_by_refresh_lock(scheduler, "demo"):
            raise RuntimeError("boom")
    assert "demo" not in scheduler.write_lock_manager.locked


def test_guard_raises_when_lock_already_held():
    scheduler = _FakeRefreshScheduler()
    scheduler.write_lock_manager.locked.add("demo")
    with pytest.raises(WriteLockHeldError):
        with guarded_by_refresh_lock(scheduler, "demo"):
            pass  # pragma: no cover -- must never be entered


def test_guard_raises_and_releases_when_refresh_in_progress():
    scheduler = _FakeRefreshScheduler(refresh_in_progress=True)
    with pytest.raises(RefreshInProgressError):
        with guarded_by_refresh_lock(scheduler, "demo"):
            pass  # pragma: no cover -- must never be entered
    assert "demo" not in scheduler.write_lock_manager.locked


def test_guard_rejects_none_scheduler():
    with pytest.raises(ValueError):
        with guarded_by_refresh_lock(None, "demo"):
            pass  # pragma: no cover -- must never be entered


@pytest.mark.parametrize("bad_alias", ["", "   ", None])
def test_guard_rejects_blank_alias(bad_alias):
    scheduler = _FakeRefreshScheduler()
    with pytest.raises(ValueError):
        with guarded_by_refresh_lock(scheduler, bad_alias):
            pass  # pragma: no cover -- must never be entered
