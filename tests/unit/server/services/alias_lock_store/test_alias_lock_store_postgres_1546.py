"""PostgreSQL-specific tests for PostgresAliasLockStore.

The backend-agnostic contract (acquire/release/renew, ownership loss, lease,
crash recovery) lives in ``test_alias_lock_store_contract.py``. This module
covers what only PostgreSQL has: every row-lock wait is bounded by
``lock_timeout``, and a node still on the previous release -- which holds its
lock as an UNCOMMITTED insert -- is still seen as the holder during a rolling
upgrade.

Skip-gated on TEST_POSTGRES_DSN (see conftest.py). Real PostgreSQL only.
"""

from __future__ import annotations

import threading
import time

import psycopg

from .conftest import postgres_skip_marker

pytestmark = postgres_skip_marker

_PROMPT_CONTENTION_DEADLINE_SECONDS = 3.0


def _legacy_uncommitted_holder(dsn: str, lock_key: str):
    """What a previous-release node does: INSERT and keep the transaction open."""
    conn = psycopg.connect(dsn)
    conn.execute(
        "INSERT INTO golden_repo_alias_locks (lock_key, owner_token, operation) "
        "VALUES (%s, %s, %s)",
        (lock_key, "legacy-token", "refresh_scheduler"),
    )
    return conn


class TestBoundedWaits:
    def test_contended_acquire_returns_none_promptly(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        result: dict = {}

        def attempt() -> None:
            result["handle"] = store.try_acquire(unique_key, operation="op")

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=_PROMPT_CONTENTION_DEADLINE_SECONDS)
        alive = thread.is_alive()
        store.release(handle)
        thread.join(timeout=_PROMPT_CONTENTION_DEADLINE_SECONDS)
        assert not alive, "try_acquire blocked on a held key"
        assert result["handle"] is None

    def test_free_key_acquires_quickly(self, store, unique_key):
        started = time.monotonic()
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        assert time.monotonic() - started < _PROMPT_CONTENTION_DEADLINE_SECONDS
        store.release(handle)


class TestRollingUpgradeLegacyHolder:
    def test_uncommitted_legacy_row_reads_as_held_promptly(
        self, store, pg_dsn, unique_key
    ):
        legacy = _legacy_uncommitted_holder(pg_dsn, unique_key)
        try:
            started = time.monotonic()
            assert store.try_acquire(unique_key, operation="op") is None
            assert store.is_held(unique_key) is True
            assert time.monotonic() - started < _PROMPT_CONTENTION_DEADLINE_SECONDS
        finally:
            legacy.rollback()
            legacy.close()
        assert store.is_held(unique_key) is False
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        store.release(handle)

    def test_is_held_probe_never_persists_a_row(self, store, pg_dsn, unique_key):
        assert store.is_held(unique_key) is False
        with psycopg.connect(pg_dsn) as conn:
            row = conn.execute(
                "SELECT count(*) FROM golden_repo_alias_locks WHERE lock_key = %s",
                (unique_key,),
            ).fetchone()
        assert row is not None and row[0] == 0
