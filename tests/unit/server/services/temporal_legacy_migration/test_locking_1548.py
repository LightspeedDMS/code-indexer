"""Issue #1548 blocker 4: refresh-safe write-lock guard tests."""

import threading
import time
from typing import List

import pytest

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.temporal_legacy_migration import (
    locking as locking_mod,
)
from code_indexer.server.services.temporal_legacy_migration.locking import (
    MIGRATION_OWNER_NAME,
    TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS,
    RefreshInProgressError,
    WriteLockHeldError,
    guarded_by_refresh_lock,
)

# Named timing constants for the heartbeat tests below -- avoids magic
# numbers scattered across the test bodies. The interval is monkeypatched
# far below any real production value purely to keep these tests fast;
# the body-sleep durations are chosen as small multiples of the interval
# so the assertions are robust against normal test-runner jitter.
_TEST_HEARTBEAT_INTERVAL_SECONDS = 0.02
_LONG_BODY_SLEEP_SECONDS = _TEST_HEARTBEAT_INTERVAL_SECONDS * 8
_SHORT_BODY_SLEEP_SECONDS = _TEST_HEARTBEAT_INTERVAL_SECONDS * 2
_POST_EXIT_WAIT_SECONDS = _TEST_HEARTBEAT_INTERVAL_SECONDS * 8


class _FakeWriteLockManager:
    """Thread-safe fake: renew() is called from the guard's background
    heartbeat thread while the test thread concurrently reads recorded
    calls, so ALL shared state is accessed exclusively through the
    lock-protected helper methods below -- callers never touch
    ``locked``/``acquire_calls``/``release_calls``/``renew_calls``
    directly.
    """

    def __init__(self):
        self._state_lock = threading.Lock()
        self.locked = set()
        self.acquire_calls = []
        self.release_calls = []
        self.renew_calls = []

    def acquire(self, alias, *, owner_name, ttl_seconds=3600):
        with self._state_lock:
            self.acquire_calls.append((alias, owner_name, ttl_seconds))
            if alias in self.locked:
                return False
            self.locked.add(alias)
            return True

    def release(self, alias, *, owner_name):
        with self._state_lock:
            self.release_calls.append((alias, owner_name))
            self.locked.discard(alias)
            return True

    def renew(self, alias, *, owner_name, ttl_seconds=3600):
        with self._state_lock:
            self.renew_calls.append((alias, owner_name, ttl_seconds))
            return alias in self.locked

    def add_lock(self, alias):
        """Test-only seam: seed the fake as already holding *alias*'s
        lock, without going through acquire().
        """
        with self._state_lock:
            self.locked.add(alias)

    def is_locked(self, alias):
        with self._state_lock:
            return alias in self.locked

    def snapshot_acquire_calls(self):
        with self._state_lock:
            return list(self.acquire_calls)

    def snapshot_release_calls(self):
        with self._state_lock:
            return list(self.release_calls)

    def snapshot_renew_calls(self):
        with self._state_lock:
            return list(self.renew_calls)


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
        assert scheduler.write_lock_manager.is_locked("demo")
    assert entered
    assert not scheduler.write_lock_manager.is_locked("demo")


def test_guard_releases_lock_even_when_body_raises():
    scheduler = _FakeRefreshScheduler()
    with pytest.raises(RuntimeError):
        with guarded_by_refresh_lock(scheduler, "demo"):
            raise RuntimeError("boom")
    assert not scheduler.write_lock_manager.is_locked("demo")


def test_guard_raises_when_lock_already_held():
    scheduler = _FakeRefreshScheduler()
    scheduler.write_lock_manager.add_lock("demo")
    with pytest.raises(WriteLockHeldError):
        with guarded_by_refresh_lock(scheduler, "demo"):
            pass  # pragma: no cover -- must never be entered


def test_guard_raises_and_releases_when_refresh_in_progress():
    scheduler = _FakeRefreshScheduler(refresh_in_progress=True)
    with pytest.raises(RefreshInProgressError):
        with guarded_by_refresh_lock(scheduler, "demo"):
            pass  # pragma: no cover -- must never be entered
    assert not scheduler.write_lock_manager.is_locked("demo")


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


# ---------------------------------------------------------------------------
# Issue #1548 round-7 fix — long TTL + periodic heartbeat renewal.
#
# Codex reproduced this as a NORMAL OPERATIONAL bug, not an exotic attack:
# the default 3600s WriteLockManager TTL is far too short for a migration
# pass that can legitimately run for hours (this codebase's own
# indexing-path invariant). Once the TTL elapses, a second, legitimate
# acquire (e.g. a concurrent refresh) takes ownership WHILE the original
# migration is still running -- no attacker required. Mirrors
# fleet_migration/orchestrator.py's own AC8 fix: an explicit, generous TTL
# (not the 3600s default) PLUS periodic renewal so an even-longer-running
# migration never depends solely on a long-but-finite TTL.
# ---------------------------------------------------------------------------


def test_guard_acquires_with_long_ttl_not_the_3600s_default():
    scheduler = _FakeRefreshScheduler()
    with guarded_by_refresh_lock(scheduler, "demo"):
        pass
    assert scheduler.write_lock_manager.snapshot_acquire_calls() == [
        ("demo", MIGRATION_OWNER_NAME, TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS)
    ]
    assert TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS != 3600


def test_guard_renews_the_lock_periodically_while_body_runs(monkeypatch):
    monkeypatch.setattr(
        locking_mod, "_HEARTBEAT_INTERVAL_SECONDS", _TEST_HEARTBEAT_INTERVAL_SECONDS
    )
    scheduler = _FakeRefreshScheduler()
    with guarded_by_refresh_lock(scheduler, "demo"):
        time.sleep(_LONG_BODY_SLEEP_SECONDS)
    renew_calls = scheduler.write_lock_manager.snapshot_renew_calls()
    assert len(renew_calls) >= 2, (
        "expected multiple heartbeat renewals during a long-running body"
    )
    for alias, owner, ttl in renew_calls:
        assert alias == "demo"
        assert owner == MIGRATION_OWNER_NAME
        assert ttl == TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS


def test_guard_stops_heartbeat_renewal_after_the_body_exits(monkeypatch):
    monkeypatch.setattr(
        locking_mod, "_HEARTBEAT_INTERVAL_SECONDS", _TEST_HEARTBEAT_INTERVAL_SECONDS
    )
    scheduler = _FakeRefreshScheduler()
    with guarded_by_refresh_lock(scheduler, "demo"):
        time.sleep(_SHORT_BODY_SLEEP_SECONDS)
    count_at_exit = len(scheduler.write_lock_manager.snapshot_renew_calls())
    time.sleep(_POST_EXIT_WAIT_SECONDS)
    assert len(scheduler.write_lock_manager.snapshot_renew_calls()) == count_at_exit, (
        "heartbeat renewal must stop firing once the guarded body has exited"
    )
