"""
Tests for the connection-level SQLite lock-contention fix (Bug #1758).

token_blacklist, elevated_sessions, and oidc_state_tokens are all accessed via
a raw sqlite3.connect() call (TokenBlacklist._sqlite_add/_sqlite_contains/
_sqlite_prune in app.py, ElevatedSessionManager._get_conn,
StateManager._get_conn) that never set a busy-wait timeout -- unlike the
DatabaseConnectionManager-backed path used by the other five
DataRetentionScheduler tables, which sets `PRAGMA busy_timeout = 30000` so
SQLite's own internal wait absorbs a brief lock. Without a matching
mitigation, a lock held by a concurrent process (e.g. an auto-updater-
triggered server restart briefly overlapping a request or the scheduler's
tick) outlasts Python's own 5.0s sqlite3.connect() default busy-timeout and
raises `database is locked` immediately.

An earlier version of this fix added a scheduler-layer bounded retry
(`DataRetentionScheduler._call_with_lock_retry`) that only covered the three
background PRUNE calls. Code review found the SAME unprotected raw
connections are also used by user-facing WRITE paths that hit the identical
restart-window race and are more severe (a logout that fails to revoke a JWT
fails silently, per the existing try/except around blacklist writes from
Story #1163) -- namely TokenBlacklist.add()/contains() (app.py),
ElevatedSessionManager.create() (elevated_session_manager.py), and
StateManager.create_state() (oidc/state_manager.py).

The fix implemented here instead sets `timeout=30.0` directly on each of the
5 `sqlite3.connect(...)` call sites, matching
`database_manager.py:1854`'s 30s convention exactly. This is SQLite's own
built-in busy-wait timeout (the `timeout=` kwarg on `sqlite3.connect()`) --
it covers every caller through each connection uniformly, including the
prune path AND the write paths, with no separate retry-layer bookkeeping
needed. The scheduler-layer retry helper was therefore deleted as pure
duplication once this connection-level fix was in place (Anti-Duplication).

Requirement: a transient "database is locked" condition must be absorbed by
the connection's own busy-wait timeout on a SINGLE attempt -- no exception is
raised at all, and no scheduler-layer retry logic is involved.

IMPORTANT: Python's sqlite3.connect() has its OWN default `timeout=5.0` (it
calls sqlite3_busy_timeout() internally), so a connection created via a bare
sqlite3.connect(path) -- what the pre-fix production code did at all 5 sites
-- already retries silently for up to 5 seconds before ever raising
'database is locked'. This was confirmed empirically in the discriminating
end-to-end tests below: the lock is held for 6.0s (comfortably past that 5s
window) so the fix must be proven by an explicit timeout=30.0 kwarg, not by
Python's own default absorbing a short contention window.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_data_retention_per_table_isolation.py)
# ---------------------------------------------------------------------------


def _make_config() -> Any:
    ret_cfg = MagicMock()
    ret_cfg.operational_logs_retention_hours = 168
    ret_cfg.audit_logs_retention_hours = 720
    ret_cfg.sync_jobs_retention_hours = 168
    ret_cfg.dep_map_history_retention_hours = 720
    ret_cfg.background_jobs_retention_hours = 24
    ret_cfg.cleanup_interval_hours = 1

    config = MagicMock()
    config.data_retention_config = ret_cfg
    config.jwt_expiration_minutes = 10

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


def _make_scheduler(config_service: Any, tmp_path: Path) -> Any:
    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    return DataRetentionScheduler(
        log_db_path=tmp_path / "logs.db",
        main_db_path=tmp_path / "main.db",
        groups_db_path=tmp_path / "groups.db",
        config_service=config_service,
        storage_mode="sqlite",
    )


def _locked_error() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("database is locked")


def _make_blacklist_with_expired_row(db_path: str, jti: str = "expired-jti") -> Any:
    """Build a real TokenBlacklist backed by db_path, with one expired row."""
    from code_indexer.server.app import TokenBlacklist

    setup_conn = sqlite3.connect(db_path)
    try:
        setup_conn.execute(
            "CREATE TABLE token_blacklist (jti TEXT PRIMARY KEY, blacklisted_at REAL NOT NULL)"
        )
        old_time = time.time() - 10_000
        setup_conn.execute(
            "INSERT INTO token_blacklist (jti, blacklisted_at) VALUES (?, ?)",
            (jti, old_time),
        )
        setup_conn.commit()
    finally:
        setup_conn.close()

    blacklist = TokenBlacklist()
    blacklist.set_sqlite_path(db_path)
    return blacklist


def _row_exists(db_path: str, jti: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM token_blacklist WHERE jti = ?", (jti,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _hold_exclusive_lock(
    db_path: str,
    hold_seconds: float,
    lock_acquired: threading.Event,
    lock_released: threading.Event,
) -> None:
    """Hold a genuine SQLite EXCLUSIVE lock on db_path for hold_seconds.

    Runs entirely on a single background thread (the connection is opened,
    used, and released on that same thread -- sqlite3 connections are
    thread-affine by default). BEGIN EXCLUSIVE acquires the lock on the
    whole database file immediately in rollback-journal mode, so no write
    statement is required to block a would-be reader/writer.
    """
    locker_conn = None
    try:
        locker_conn = sqlite3.connect(db_path)
        locker_conn.execute("BEGIN EXCLUSIVE")
        lock_acquired.set()
        time.sleep(hold_seconds)
        locker_conn.commit()
    finally:
        if locker_conn is not None:
            locker_conn.close()
        lock_released.set()


def _start_locker_thread(
    db_path: str, hold_seconds: float = 6.0
) -> "tuple[threading.Thread, threading.Event, threading.Event]":
    """Start a real exclusive-lock holder thread and wait until it has the lock.

    hold_seconds defaults to 6.0 -- comfortably past Python's own 5.0s
    sqlite3.connect() default busy-timeout, so a passing test proves the
    connection-level timeout=30.0 fix, not Python's own default absorbing a
    short window.
    """
    lock_acquired = threading.Event()
    lock_released = threading.Event()
    thread = threading.Thread(
        target=_hold_exclusive_lock,
        args=(db_path, hold_seconds, lock_acquired, lock_released),
        daemon=True,
    )
    thread.start()
    assert lock_acquired.wait(timeout=15), (
        "locker thread must acquire the exclusive lock before the protected call starts"
    )
    return thread, lock_acquired, lock_released


# ---------------------------------------------------------------------------
# DataRetentionScheduler safe-wrapper behaviour that is independent of the
# fix mechanism: any exception (transient or not) is caught, recorded in
# failed_tables, and never aborts the cleanup cycle; a failed tick's row is
# still present and gets caught up on a later tick. This property does not
# depend on which layer absorbs a transient lock, so it stays true after the
# scheduler-level retry helper was removed.
# ---------------------------------------------------------------------------


class TestSafePrunePersistentFailureAndCatchUp:
    def test_safe_prune_token_blacklist_still_fails_when_lock_persists(
        self, tmp_path: Path
    ) -> None:
        """
        A genuine, unrecovered OperationalError from the underlying prune
        call must still be recorded as a per-table failure (the safe-wrapper
        try/except is unconditional) rather than crashing the whole cleanup
        cycle.
        """
        scheduler = _make_scheduler(_make_config(), tmp_path)

        fake_blacklist = MagicMock()
        fake_blacklist.prune_expired.side_effect = _locked_error()

        failed_tables: List[str] = []
        with patch(
            "code_indexer.server.app.get_token_blacklist",
            return_value=fake_blacklist,
        ):
            result = scheduler._safe_prune_token_blacklist(
                jwt_expiration_minutes=10, failed_tables=failed_tables
            )

        assert result == 0
        assert failed_tables == ["token_blacklist"]

    def test_failed_prune_is_caught_up_on_next_tick(self, tmp_path: Path) -> None:
        """
        Verify a failed prune tick is NOT permanently skipped: the expired
        row is still present after the failing tick, and a subsequent tick
        successfully prunes it -- no 'already pruned this window'
        bookkeeping exists that would cause a transient miss to be lost
        forever.

        Tick 1 patches get_token_blacklist() to simulate a persistent
        failure deterministically and fast. Tick 2 patches
        get_token_blacklist() to return the SAME real, uncontended
        TokenBlacklist instance built by _make_blacklist_with_expired_row
        (no simulated failure -- prune_expired() runs its real SQLite path).
        """
        db_path = str(tmp_path / "blacklist.db")
        blacklist = _make_blacklist_with_expired_row(db_path)

        scheduler = _make_scheduler(_make_config(), tmp_path)

        always_locked = MagicMock()
        always_locked.prune_expired.side_effect = _locked_error()
        failed_tables_tick1: List[str] = []
        with patch(
            "code_indexer.server.app.get_token_blacklist",
            return_value=always_locked,
        ):
            deleted_tick1 = scheduler._safe_prune_token_blacklist(
                jwt_expiration_minutes=10, failed_tables=failed_tables_tick1
            )

        assert deleted_tick1 == 0
        assert failed_tables_tick1 == ["token_blacklist"]
        # The row must still be present -- the failed tick did not delete it.
        assert _row_exists(db_path, "expired-jti"), (
            "expired row must still exist after the failed tick"
        )

        failed_tables_tick2: List[str] = []
        with patch(
            "code_indexer.server.app.get_token_blacklist",
            return_value=blacklist,
        ):
            deleted_tick2 = scheduler._safe_prune_token_blacklist(
                jwt_expiration_minutes=10, failed_tables=failed_tables_tick2
            )

        assert deleted_tick2 == 1, (
            "the previously-failed row must be caught up on the next tick"
        )
        assert failed_tables_tick2 == []
        assert not _row_exists(db_path, "expired-jti"), (
            "row must be gone after the successful catch-up tick"
        )


# ---------------------------------------------------------------------------
# Real SQLite lock contention: genuine, unmocked reproduction of the bug,
# proving the connection-level timeout=30.0 fix at each of the 5 raw
# sqlite3.connect() call sites. No unittest.mock.patch is used to simulate
# any SQLite behaviour in this section -- only a real second connection
# holding a genuine `BEGIN EXCLUSIVE` lock, exactly matching production's
# real connection setup. The single successful attempt (no retry needed)
# proves the fix operates at the connection layer, not a retry layer.
# ---------------------------------------------------------------------------


class TestTokenBlacklistRealLockContention:
    def test_prune_absorbs_real_lock_via_connection_timeout(
        self, tmp_path: Path
    ) -> None:
        """TokenBlacklist._sqlite_prune (app.py) tolerates real contention."""
        db_path = str(tmp_path / "blacklist_prune.db")
        blacklist = _make_blacklist_with_expired_row(db_path)

        thread, _acquired, released = _start_locker_thread(db_path, hold_seconds=6.0)
        try:
            scheduler = _make_scheduler(_make_config(), tmp_path)
            failed_tables: List[str] = []
            with patch(
                "code_indexer.server.app.get_token_blacklist",
                return_value=blacklist,
            ):
                deleted = scheduler._safe_prune_token_blacklist(
                    jwt_expiration_minutes=10, failed_tables=failed_tables
                )
        finally:
            thread.join(timeout=20)

        assert released.is_set(), "locker thread must have completed"
        assert deleted == 1, (
            "the real lock (held for 6.0s, past Python's own 5.0s connect() "
            "default) must be absorbed by the connection's own timeout=30.0 "
            "and the expired row pruned on the first attempt"
        )
        assert failed_tables == []

    def test_write_path_absorbs_real_lock_via_connection_timeout(
        self, tmp_path: Path
    ) -> None:
        """TokenBlacklist.add()/contains() (_sqlite_add/_sqlite_contains in
        app.py) tolerate real contention -- the more severe gap the review
        found: this is the JWT-revocation-on-logout write path, which had
        NO retry/timeout protection at all before this fix (the earlier
        scheduler-layer retry only covered the prune path).
        """
        from code_indexer.server.app import TokenBlacklist

        db_path = str(tmp_path / "blacklist_write.db")
        setup_conn = sqlite3.connect(db_path)
        try:
            setup_conn.execute(
                "CREATE TABLE token_blacklist "
                "(jti TEXT PRIMARY KEY, blacklisted_at REAL NOT NULL)"
            )
            setup_conn.commit()
        finally:
            setup_conn.close()

        blacklist = TokenBlacklist()
        blacklist.set_sqlite_path(db_path)

        # 1) _sqlite_add under real contention (logout JWT revocation).
        thread1, _acq1, released1 = _start_locker_thread(db_path, hold_seconds=6.0)
        try:
            blacklist.add("logout-jti")
        finally:
            thread1.join(timeout=20)

        assert released1.is_set()
        assert _row_exists(db_path, "logout-jti"), (
            "add() must have written the row despite the real lock -- proves "
            "_sqlite_add's connection absorbs contention via timeout=30.0"
        )

        # 2) _sqlite_contains under real contention, from a FRESH instance
        # (empty _local set) so the DB path is genuinely exercised --
        # queried on every authenticated request in production.
        fresh_view = TokenBlacklist()
        fresh_view.set_sqlite_path(db_path)

        thread2, _acq2, released2 = _start_locker_thread(db_path, hold_seconds=6.0)
        try:
            result = fresh_view.contains("logout-jti")
        finally:
            thread2.join(timeout=20)

        assert released2.is_set()
        assert result is True, (
            "contains() must succeed despite the real lock -- proves "
            "_sqlite_contains's connection absorbs contention via "
            "timeout=30.0"
        )


class TestElevatedSessionManagerRealLockContention:
    def test_create_absorbs_real_lock_via_connection_timeout(
        self, tmp_path: Path
    ) -> None:
        """ElevatedSessionManager.create() (_get_conn in
        elevated_session_manager.py) tolerates real contention on the TOTP
        step-up elevation write path.
        """
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        db_path = str(tmp_path / "elevated.db")
        # Schema is created here, before the lock is taken.
        manager = ElevatedSessionManager(db_path=db_path)

        thread, _acquired, released = _start_locker_thread(db_path, hold_seconds=6.0)
        try:
            manager.create(
                session_key="session-key-1",
                username="alice",
                elevated_from_ip="127.0.0.1",
                scope="full",
            )
        finally:
            thread.join(timeout=20)

        assert released.is_set(), "locker thread must have completed"

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT username FROM elevated_sessions WHERE session_key = ?",
                ("session-key-1",),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None and row[0] == "alice", (
            "create() must have written the row despite the real lock -- "
            "proves _get_conn's connection absorbs contention via "
            "timeout=30.0"
        )


class TestOidcStateManagerRealLockContention:
    def test_create_state_absorbs_real_lock_via_connection_timeout(
        self, tmp_path: Path
    ) -> None:
        """StateManager.create_state() (_get_conn in
        oidc/state_manager.py) tolerates real contention on the OIDC CSRF
        state-token write path.
        """
        from code_indexer.server.auth.oidc.state_manager import StateManager

        db_path = str(tmp_path / "oidc_state.db")
        manager = StateManager()
        # Redirects storage and creates the schema here, before the lock.
        manager.set_sqlite_path(db_path)

        thread, _acquired, released = _start_locker_thread(db_path, hold_seconds=6.0)
        try:
            state_token = manager.create_state({"code_verifier": "abc123"})
        finally:
            thread.join(timeout=20)

        assert released.is_set(), "locker thread must have completed"
        assert isinstance(state_token, str) and state_token

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT state_data FROM oidc_state_tokens WHERE state_token = ?",
                (state_token,),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, (
            "create_state() must have written the row despite the real "
            "lock -- proves _get_conn's connection absorbs contention via "
            "timeout=30.0"
        )


# ---------------------------------------------------------------------------
# Regression guard: _call_with_lock_retry / _is_transient_lock_error must no
# longer exist on DataRetentionScheduler -- the connection-level fix made
# that second lock-tolerance mechanism pure duplication (Anti-Duplication).
# ---------------------------------------------------------------------------


class TestRetryHelperRemoved:
    def test_scheduler_no_longer_has_lock_retry_helpers(self, tmp_path: Path) -> None:
        scheduler = _make_scheduler(_make_config(), tmp_path)
        assert not hasattr(scheduler, "_call_with_lock_retry")
        assert not hasattr(scheduler, "_is_transient_lock_error")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
