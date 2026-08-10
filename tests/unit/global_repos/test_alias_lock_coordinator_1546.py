"""Tests for AliasLockCoordinator (Issue #1546 Phase 2).

AliasLockCoordinator is the drop-in replacement for the raw
``WriteLockManager`` instance installed as ``RefreshScheduler.write_lock_manager``.
It preserves WriteLockManager's exact bool-based public API (acquire/release/
renew/is_locked/get_lock_info) so every one of the ~8 real call sites that
already go through ``scheduler.acquire_write_lock``/``release_write_lock``/
``is_write_locked`` or ``scheduler.write_lock_manager.*`` directly is rewired
onto the DB-backed mechanism for free, with zero call-site changes, purely by
installing this coordinator in place of the bare WriteLockManager.

Dispatch rule: `db_backed_enabled_getter()` is consulted only at `acquire()`
time. Which mechanism `release()`/`renew()`/`is_locked()`/`get_lock_info()`
use for a given alias is determined by HOW that alias's lock was acquired
(tracked in a private per-coordinator handle dict, popped on release), never
by re-reading the live flag -- this is deliberate: an operator flipping the
flag mid-hold must never cause a release to go through the wrong mechanism
and leak the lock. Once an alias's handle is popped (already released), a
SECOND release()/renew()/get_lock_info() call for that same alias has
nothing left to dispatch on and falls through to the file manager's own
idempotent "not held" contract -- exactly as if the DB-backed acquire had
never happened, never a crash.

Also covers AC4 (concurrent-acquisition mutual exclusion through the
coordinator itself, not just the underlying store -- proving the handle
-tracking bookkeeping introduces no new race) and AC6 (crash recovery via
connection-death rollback, no TTL).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List, Tuple

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_DEFAULT_TTL_SECONDS = 3600
_STORE_BUSY_TIMEOUT_SECONDS = 5.0
_RACE_NUM_THREADS = 6
_RACE_ACQUISITIONS_PER_THREAD = 8
_RACE_ATTEMPT_BUDGET_MULTIPLIER = 50
_RACE_HOLD_TIME_SCALE_SECONDS = 0.001
_RACE_HOLD_TIME_MODULUS = 3
_RACE_THREAD_JOIN_TIMEOUT_SECONDS = 60


def _file_mode_coordinator(golden_repos_dir: Path) -> AliasLockCoordinator:
    """No getter/resolver at all -- must behave byte-identically to a bare
    WriteLockManager (the CLI/solo/not-yet-wired default)."""
    return AliasLockCoordinator(golden_repos_dir=golden_repos_dir)


def _db_mode_coordinator(
    golden_repos_dir: Path, store: SqliteAliasLockStore
) -> AliasLockCoordinator:
    return AliasLockCoordinator(
        golden_repos_dir=golden_repos_dir,
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )


class TestFileModeDelegation:
    """db_backed_enabled_getter absent or False -> byte-identical to the
    legacy file-based WriteLockManager (AC7's mixed-fleet default)."""

    def test_acquire_release_round_trip_uses_file_lock(self, tmp_path):
        coordinator = _file_mode_coordinator(tmp_path)
        assert coordinator.acquire("myalias", owner_name="tester") is True
        # Lock file must actually exist on disk -- proves the file path ran.
        assert (tmp_path / ".locks" / "myalias.lock").exists()
        assert coordinator.release("myalias", owner_name="tester") is True
        assert not (tmp_path / ".locks" / "myalias.lock").exists()

    def test_second_acquire_while_held_returns_false(self, tmp_path):
        coordinator = _file_mode_coordinator(tmp_path)
        assert coordinator.acquire("myalias", owner_name="a") is True
        assert coordinator.acquire("myalias", owner_name="b") is False
        assert coordinator.release("myalias", owner_name="a") is True

    def test_is_locked_reflects_file_state(self, tmp_path):
        coordinator = _file_mode_coordinator(tmp_path)
        assert coordinator.is_locked("myalias") is False
        assert coordinator.acquire("myalias", owner_name="a") is True
        assert coordinator.is_locked("myalias") is True
        assert coordinator.release("myalias", owner_name="a") is True
        assert coordinator.is_locked("myalias") is False

    def test_renew_returns_bool_and_extends_lock(self, tmp_path):
        coordinator = _file_mode_coordinator(tmp_path)
        assert (
            coordinator.acquire(
                "myalias", owner_name="a", ttl_seconds=_DEFAULT_TTL_SECONDS
            )
            is True
        )
        assert (
            coordinator.renew(
                "myalias", owner_name="a", ttl_seconds=_DEFAULT_TTL_SECONDS
            )
            is True
        )
        assert coordinator.release("myalias", owner_name="a") is True

    def test_get_lock_info_returns_file_metadata(self, tmp_path):
        coordinator = _file_mode_coordinator(tmp_path)
        assert coordinator.acquire("myalias", owner_name="a") is True
        info = coordinator.get_lock_info("myalias")
        assert info is not None
        assert info["owner"] == "a"
        assert coordinator.release("myalias", owner_name="a") is True

    def test_db_backed_enabled_getter_false_uses_file_lock_when_no_resolver_configured(
        self, tmp_path
    ):
        """No store_resolver at all -- the CLI/solo default. There is
        nothing to cross-check against, so the flag-OFF path must skip
        straight to the file lock, byte-identical to a bare
        WriteLockManager."""
        coordinator = AliasLockCoordinator(
            golden_repos_dir=tmp_path,
            db_backed_enabled_getter=lambda: False,
        )
        assert coordinator.acquire("myalias", owner_name="a") is True
        assert (tmp_path / ".locks" / "myalias.lock").exists()
        assert coordinator.release("myalias", owner_name="a") is True


class TestCrossMechanismConflictDetection:
    """Codex Fix 2: the coordinator's per-alias threading.Lock only
    serializes within ONE process -- it cannot close a cross-process
    window between the two independent mechanisms (a file lock and a DB
    row). Two concrete gaps, both reported by Codex:

    - Flag OFF: acquire() used to check only the process-local `_handles`
      dict (which can NEVER see another process's DB-acquired lock) before
      granting the file lock. Fixed by also consulting the store's
      authoritative `is_held()` whenever a store_resolver is configured,
      independent of the live flag (mirrors is_locked()/get_lock_info()'s
      existing "check both sources whenever possible" rule).
    - Flag ON: acquire() checked file_manager.is_locked() BEFORE acquiring
      the DB lock, but never re-checked AFTER -- so a file-mode acquire
      landing in that exact window went undetected. Fixed by re-checking
      immediately after the DB insert and rolling back if a file lock is
      now present. This NARROWS the race window (to roughly one extra
      filesystem stat) but does not eliminate it -- see
      AliasLockCoordinator's module docstring for the honest, explicitly
      accepted residual.
    """

    def test_flag_off_acquire_refuses_when_db_lock_held_by_another_process(
        self, tmp_path
    ):
        """Process A acquires the DB lock directly on the store (mirrors
        a DIFFERENT process/coordinator instance -- this coordinator's own
        `_handles` dict can never see it). Process B's coordinator has the
        flag OFF but a store_resolver IS configured (mirrors production:
        the resolver always exists once wired, only the flag toggles).
        Process B must NOT be granted the file lock."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        other_process_handle = store.try_acquire("myalias", operation="other-proc")
        try:
            assert other_process_handle is not None

            coordinator_b = AliasLockCoordinator(
                golden_repos_dir=tmp_path,
                db_backed_enabled_getter=lambda: False,
                store_resolver=lambda: store,
            )

            assert coordinator_b.acquire("myalias", owner_name="b") is False
            assert not (tmp_path / ".locks" / "myalias.lock").exists()
        finally:
            # AliasLockStore.release() (base.py Protocol) returns None and
            # RAISES AliasLockOwnershipLostError on failure -- it is not a
            # bool-returning method, so a genuine cleanup failure here
            # surfaces as an exception, not a missed assertion. Guarded
            # on not-None so a failed acquisition above doesn't try to
            # release a handle that was never obtained.
            if other_process_handle is not None:
                store.release(other_process_handle)

    def test_flag_off_acquire_succeeds_via_file_lock_when_store_reports_not_held(
        self, tmp_path
    ):
        """A store_resolver IS configured (production shape once wired)
        but nobody holds the DB lock for this alias -- the flag-OFF path
        must still succeed via the file lock exactly as before."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")

        coordinator = AliasLockCoordinator(
            golden_repos_dir=tmp_path,
            db_backed_enabled_getter=lambda: False,
            store_resolver=lambda: store,
        )

        assert coordinator.acquire("myalias", owner_name="a") is True
        assert (tmp_path / ".locks" / "myalias.lock").exists()
        assert coordinator.release("myalias", owner_name="a") is True

    def test_flag_on_acquire_rolls_back_db_lock_if_file_lock_appears_during_acquire(
        self, tmp_path, monkeypatch
    ):
        """Simulates process A's file-based acquire landing in the exact
        window between this coordinator's pre-check and its DB insert
        completing. Interleaving is forced deterministically by
        monkeypatching the STORE's try_acquire (a collaborator
        dependency, not the coordinator under test) to acquire the file
        lock as a side effect of the DB call -- the real race is
        cross-process timing that cannot be reproduced reliably in a
        single-process unit test any other way. Fixed behavior: the DB
        lock just acquired is detected as conflicting and rolled back;
        acquire() reports False."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        original_try_acquire = store.try_acquire

        def _try_acquire_with_interleaved_file_lock(
            lock_key, operation, owner_token=None
        ):
            handle = original_try_acquire(lock_key, operation, owner_token)
            acquired = coordinator._file_manager.acquire(
                lock_key, owner_name="other-proc"
            )
            assert acquired is True, "simulated interleaved file-lock acquire failed"
            return handle

        monkeypatch.setattr(
            store, "try_acquire", _try_acquire_with_interleaved_file_lock
        )

        try:
            assert coordinator.acquire("myalias", owner_name="b") is False
            assert coordinator._handles.get("myalias") is None
            assert store.is_held("myalias") is False
        finally:
            # WriteLockManager.release() DOES return bool (unlike
            # AliasLockStore.release() above, which returns None/raises)
            # -- assert cleanup actually succeeded.
            assert (
                coordinator._file_manager.release("myalias", owner_name="other-proc")
                is True
            )


class TestDbBackedModeDispatch:
    """db_backed_enabled_getter() returns True -> dispatches to the
    AliasLockStore, never touches the file mechanism at all."""

    def test_acquire_uses_store_and_never_creates_a_lock_file(self, tmp_path):
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert coordinator.acquire("myalias", owner_name="tester") is True
        assert not (tmp_path / ".locks" / "myalias.lock").exists()
        assert coordinator.release("myalias", owner_name="tester") is True

    def test_second_acquire_while_held_returns_false(self, tmp_path):
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert coordinator.acquire("myalias", owner_name="a") is True
        assert coordinator.acquire("myalias", owner_name="b") is False
        assert coordinator.release("myalias", owner_name="a") is True

    def test_is_locked_reflects_store_state(self, tmp_path):
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert coordinator.is_locked("myalias") is False
        assert coordinator.acquire("myalias", owner_name="a") is True
        assert coordinator.is_locked("myalias") is True
        assert coordinator.release("myalias", owner_name="a") is True
        assert coordinator.is_locked("myalias") is False

    def test_release_of_untracked_alias_falls_through_to_idempotent_file_path(
        self, tmp_path
    ):
        """After release(), the alias's handle is popped -- nothing left to
        dispatch on. A SECOND release() call for the same alias therefore
        falls through to the file manager's own idempotent "lock file
        does not exist -> True" contract, exactly as if the DB-backed
        acquire had never happened. Never raises."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert coordinator.acquire("myalias", owner_name="a") is True
        assert coordinator.release("myalias", owner_name="a") is True
        assert coordinator.release("myalias", owner_name="a") is True

    def test_get_lock_info_reports_held_without_leaking_metadata_contract(
        self, tmp_path
    ):
        """DB-backed mode cannot observe another holder's identity (the
        row is invisible while the transaction is open, see base.py's
        docstring) -- get_lock_info() must still report SOMETHING truthy
        when held (files.py's write-mode caller does
        `info.get("owner", "unknown")`), never crash or return a dict
        indistinguishable from "not held"."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert coordinator.get_lock_info("myalias") is None
        assert coordinator.acquire("myalias", owner_name="a") is True
        info = coordinator.get_lock_info("myalias")
        assert info is not None
        assert "owner" in info
        assert coordinator.release("myalias", owner_name="a") is True
        assert coordinator.get_lock_info("myalias") is None


class TestOwnershipLossPropagation:
    """AC5: AliasLockOwnershipLostError from renew() must reach callers
    unwrapped -- e.g. temporal_legacy_migration/locking.py's heartbeat
    thread, which catches `except Exception` generically and marks the
    lock lost."""

    def test_renew_success_returns_true_and_keeps_lock_held(self, tmp_path):
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert (
            coordinator.acquire("myalias", owner_name="a", owner_token="tok-1") is True
        )
        assert coordinator.renew("myalias", owner_name="a", owner_token="tok-1") is True
        # Still held -- a second acquire by someone else must still fail.
        assert coordinator.acquire("myalias", owner_name="b") is False
        assert (
            coordinator.release("myalias", owner_name="a", owner_token="tok-1") is True
        )

    def test_renew_after_release_falls_through_to_file_path_returns_false(
        self, tmp_path
    ):
        """Nothing tracked anymore for this alias after release() -- falls
        through to the file manager, whose renew() returns False (no lock
        file present) rather than raising. The exception-propagation
        contract for a genuinely severed connection is proven separately
        below, where the handle is still tracked."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert (
            coordinator.acquire("myalias", owner_name="a", owner_token="tok-1") is True
        )
        assert (
            coordinator.release("myalias", owner_name="a", owner_token="tok-1") is True
        )
        assert (
            coordinator.renew("myalias", owner_name="a", owner_token="tok-1") is False
        )

    def test_renew_on_severed_connection_raises_ownership_lost(self, tmp_path):
        """Simulates a crash mid-hold (the connection dies but the
        in-process handle is still tracked) -- renew() must raise
        AliasLockOwnershipLostError, not return False, not swallow it."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert (
            coordinator.acquire("myalias", owner_name="a", owner_token="tok-1") is True
        )
        handle = coordinator._handles["myalias"]
        handle._connection.close()  # simulate the connection dying

        with pytest.raises(AliasLockOwnershipLostError):
            coordinator.renew("myalias", owner_name="a", owner_token="tok-1")


class TestCrashRecovery:
    """AC6: a process/connection dying while holding the lock is
    IMMEDIATELY recoverable via connection-death rollback -- no TTL, no
    reaper, no clock comparison."""

    def test_closing_the_connection_immediately_frees_the_lock(self, tmp_path):
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        coordinator = _db_mode_coordinator(tmp_path, store)

        assert coordinator.acquire("myalias", owner_name="a") is True
        handle = coordinator._handles["myalias"]
        handle._connection.close()  # simulate a hard crash

        # A fresh acquisition must succeed immediately -- no TTL wait.
        assert coordinator.acquire("myalias", owner_name="b") is True
        assert coordinator.release("myalias", owner_name="b") is True


def _run_coordinator_racing_worker(
    coordinator: AliasLockCoordinator,
    lock_key: str,
    thread_id: int,
    acquisitions_per_thread: int,
    max_attempts: int,
    events: List[Tuple[float, float, int]],
    events_lock: threading.Lock,
) -> None:
    """One thread's full acquire/hold/release loop through the COORDINATOR
    (not the bare store), repeated until `acquisitions_per_thread`
    successful cycles complete, bounded by `max_attempts`."""
    completed = 0
    attempts = 0
    while completed < acquisitions_per_thread:
        attempts += 1
        if attempts > max_attempts:
            raise AssertionError(f"thread {thread_id} exceeded attempt budget")
        owner_token = f"tok-{thread_id}-{attempts}"
        owner_name = f"t{thread_id}"
        if not coordinator.acquire(
            lock_key, owner_name=owner_name, owner_token=owner_token
        ):
            continue
        start = time.monotonic()
        time.sleep(
            _RACE_HOLD_TIME_SCALE_SECONDS * (thread_id % _RACE_HOLD_TIME_MODULUS)
        )
        end = time.monotonic()
        released = coordinator.release(
            lock_key, owner_name=owner_name, owner_token=owner_token
        )
        if not released:
            raise AssertionError(
                f"thread {thread_id} failed to release its own just-acquired lock"
            )
        with events_lock:
            events.append((start, end, thread_id))
        completed += 1


def _assert_no_overlapping_intervals(events: List[Tuple[float, float, int]]) -> None:
    events_sorted = sorted(events, key=lambda e: e[0])
    for i in range(1, len(events_sorted)):
        prev_start, prev_end, prev_tid = events_sorted[i - 1]
        cur_start, cur_end, cur_tid = events_sorted[i]
        assert cur_start >= prev_end, (
            f"overlap detected: thread {prev_tid} held [{prev_start:.6f}, "
            f"{prev_end:.6f}] and thread {cur_tid} held [{cur_start:.6f}, "
            f"{cur_end:.6f}] -- mutual exclusion violated"
        )


class TestConcurrentAcquisition:
    """AC4: many real threads racing for the SAME alias through the
    COORDINATOR (not the bare store) -- exactly one wins per round, and no
    two winners' hold intervals ever overlap, proving the coordinator's own
    handle-tracking bookkeeping introduces no new race on top of the
    store's already-proven atomic guarantee (see the exhaustive store-level
    proof in test_alias_lock_store_sqlite_concurrency_1546.py)."""

    def test_many_threads_racing_through_coordinator_never_overlap(self, tmp_path):
        store = SqliteAliasLockStore(
            tmp_path / "alias_locks", busy_timeout_seconds=_STORE_BUSY_TIMEOUT_SECONDS
        )
        coordinator = _db_mode_coordinator(tmp_path, store)
        lock_key = "racing-alias"
        max_attempts = _RACE_ACQUISITIONS_PER_THREAD * _RACE_ATTEMPT_BUDGET_MULTIPLIER

        events: List[Tuple[float, float, int]] = []
        events_lock = threading.Lock()
        errors: List[BaseException] = []

        def worker(thread_id: int) -> None:
            try:
                _run_coordinator_racing_worker(
                    coordinator,
                    lock_key,
                    thread_id,
                    _RACE_ACQUISITIONS_PER_THREAD,
                    max_attempts,
                    events,
                    events_lock,
                )
            except BaseException as exc:  # noqa: BLE001
                with events_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(_RACE_NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_RACE_THREAD_JOIN_TIMEOUT_SECONDS)
        for t in threads:
            assert not t.is_alive(), "worker thread failed to terminate"

        assert not errors, f"worker thread(s) raised: {errors!r}"
        assert len(events) == _RACE_NUM_THREADS * _RACE_ACQUISITIONS_PER_THREAD
        _assert_no_overlapping_intervals(events)
