"""
Tests for the connection-level SQLite lock-contention fix (Bug #1766).

Bug #1766 is a follow-up to Bug #1758: TOTPService._get_conn
(server/auth/totp_service.py) has the identical unprotected raw
sqlite3.connect() pattern -- no explicit busy-wait timeout -- that #1758
fixed in TokenBlacklist (app.py), ElevatedSessionManager, and StateManager.
Without a matching mitigation, a lock held by a concurrent process (e.g. an
auto-updater-triggered server restart briefly overlapping a TOTP setup or
verification request) outlasts Python's own 5.0s sqlite3.connect() default
busy-timeout and raises 'database is locked' immediately.

The fix mirrors #1758 exactly: an explicit `timeout=_SQLITE_LOCK_TIMEOUT_SECONDS`
(30.0) kwarg on the one `sqlite3.connect()` call in `_get_conn`.

Requirement: a transient "database is locked" condition must be absorbed by
the connection's own busy-wait timeout on a SINGLE attempt -- no exception is
raised at all.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest


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


class TestTOTPServiceRealLockContention:
    def test_generate_secret_absorbs_real_lock_via_connection_timeout(
        self, tmp_path: Path
    ) -> None:
        """TOTPService.generate_secret() (_get_conn in totp_service.py)
        tolerates real contention on the TOTP setup write path.
        """
        from code_indexer.server.auth.totp_service import TOTPService
        from cryptography.fernet import Fernet

        db_path = str(tmp_path / "totp.db")
        key = Fernet.generate_key().decode()

        # Schema is created here (via _ensure_tables in __init__), before
        # the lock is taken below.
        service = TOTPService(db_path=db_path, mfa_encryption_key=key)

        thread, _acquired, released = _start_locker_thread(db_path, hold_seconds=6.0)
        try:
            secret = service.generate_secret("alice")
        finally:
            thread.join(timeout=20)

        assert released.is_set(), "locker thread must have completed"
        assert isinstance(secret, str) and secret

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT encrypted_secret FROM user_mfa WHERE user_id = ?",
                ("alice",),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, (
            "generate_secret() must have written the row despite the real "
            "lock -- proves _get_conn's connection absorbs contention via "
            "timeout=30.0"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
