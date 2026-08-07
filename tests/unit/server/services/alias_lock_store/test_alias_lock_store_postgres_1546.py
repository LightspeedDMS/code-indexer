"""Tests for PostgresAliasLockStore (Issue #1546 Phase 1): acquire/release
/renew basics, ownership loss, and crash recovery.

Skip-gated on TEST_POSTGRES_DSN, matching this project's established
live-PG test convention (see e.g.
tests/unit/server/services/test_bug1235_pg_duplicate_claim_race.py). Real
PostgreSQL only -- no mocking of the lock mechanism. Fixtures (``store``,
``unique_key``, ``pg_dsn``, the session-scoped real-migration bootstrap)
live in ``conftest.py``. Linearizability/contention-genuineness
concurrency tests live in
``test_alias_lock_store_postgres_concurrency_1546.py``.

Every test uses a fresh UUID-suffixed lock_key so concurrent test runs
against the same shared database never collide, and every acquired handle
is released in a guaranteed ``finally`` block. Crash/severed-connection
tests rely on PostgreSQL's own server-side rollback of the abandoned
session's uncommitted INSERT: no row from any test ever persists past
that test, so no separate teardown/cleanup fixture is required.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockHandle,
    AliasLockOwnershipLostError,
)

from .conftest import postgres_skip_marker

pytestmark = postgres_skip_marker

_GENEROUS_ACQUIRE_TIMEOUT_SECONDS = 5.0
_CRASH_RECOVERY_POLL_SLEEP_SECONDS = 0.05

# Fix #1: try_acquire() must resolve contention PROMPTLY (bounded by the
# store's own acquire-lock-timeout, never an indefinite block). This
# deadline is deliberately generous relative to the store's default
# acquire-lock-timeout so the test fails loudly (thread still alive) if a
# regression reintroduces indefinite PostgreSQL row-lock blocking.
_PROMPT_CONTENTION_DEADLINE_SECONDS = 3.0


def _acquire_until(store, lock_key, timeout_seconds):
    """Poll try_acquire() until it succeeds or timeout_seconds elapses.
    Used only in crash-recovery tests, where PostgreSQL's server-side
    detection of an abandoned session is not guaranteed instantaneous
    from another session's point of view."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        handle = store.try_acquire(lock_key, operation="op")
        if handle is not None:
            return handle
        time.sleep(_CRASH_RECOVERY_POLL_SLEEP_SECONDS)
    return None


class TestAcquireReleaseBasics:
    def test_acquire_returns_handle_with_expected_fields(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="add_golden_repo")
        assert handle is not None
        try:
            assert handle.lock_key == unique_key
            assert handle.operation == "add_golden_repo"
            assert handle.owner_token
        finally:
            store.release(handle)

    def test_acquire_with_explicit_owner_token_is_honored(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op", owner_token="tok-1")
        assert handle is not None
        try:
            assert handle.owner_token == "tok-1"
        finally:
            store.release(handle)

    def test_second_acquire_of_same_key_fails_while_first_is_held(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            competitor = store.try_acquire(unique_key, operation="op")
            try:
                assert competitor is None
            finally:
                if competitor is not None:
                    store.release(competitor)
        finally:
            store.release(handle)


def _run_worker_and_collect_promptness(
    store, unique_key: str, deadline_seconds: float
) -> tuple:
    """Run a single try_acquire() attempt on a background thread and
    report back (was_alive_at_deadline, thread, result_dict,
    worker_errors) -- extracted so the calling test stays under the
    project's per-method line budget."""
    result_lock = threading.Lock()
    result: dict = {}
    worker_errors: list = []

    def attempt() -> None:
        try:
            started = time.monotonic()
            acquired = store.try_acquire(unique_key, operation="op")
            elapsed = time.monotonic() - started
            with result_lock:
                result["handle"] = acquired
                result["elapsed"] = elapsed
        except BaseException as exc:  # noqa: BLE001
            with result_lock:
                worker_errors.append(exc)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=deadline_seconds)
    was_alive_at_deadline = thread.is_alive()
    return was_alive_at_deadline, thread, result, worker_errors, result_lock


class TestAcquireDoesNotBlockIndefinitely:
    """Fix #1 (CRITICAL): PostgreSQL's ``INSERT ... ON CONFLICT DO
    NOTHING`` blocks on the CONFLICTING row when that row belongs to an
    uncommitted transaction -- which, under this store's session-held
    -transaction design, is always true for a currently-held lock. Without
    an explicit acquire timeout, a second acquirer does not get a fast
    "conflict, return None" -- it blocks for as long as the first holder
    keeps the lock (proven empirically: an 8+ second block that only ends
    when the holder releases, after which the "blocked" call actually
    SUCCEEDS instead of ever returning None). This is the single most
    severe defect found in review: it turns a supposedly non-blocking
    try_acquire() into an unbounded wait tied to another operation's
    lifetime, which can legitimately span hours.
    """

    def test_try_acquire_resolves_contention_promptly_instead_of_blocking(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None

        (
            was_alive_at_deadline,
            thread,
            result,
            worker_errors,
            result_lock,
        ) = _run_worker_and_collect_promptness(
            store, unique_key, _PROMPT_CONTENTION_DEADLINE_SECONDS
        )

        # Release the holder and fully join the worker BEFORE any
        # assertion. This guarantees that even a (regressed) worker still
        # blocked on the row lock is unblocked and given a chance to
        # finish, so any handle it eventually obtains is deterministically
        # captured for cleanup below -- nothing is decided or released
        # based on a state that could still change out from under us.
        store.release(handle)
        thread.join(timeout=_GENEROUS_ACQUIRE_TIMEOUT_SECONDS)

        try:
            assert not thread.is_alive(), (
                "worker thread never terminated even after the holder was "
                "released -- try_acquire() appears to be stuck"
            )
            assert not worker_errors, f"worker thread raised: {worker_errors!r}"
            assert not was_alive_at_deadline, (
                "try_acquire() on an already-held key BLOCKED past the "
                f"{_PROMPT_CONTENTION_DEADLINE_SECONDS}s deadline instead of "
                "resolving promptly -- this is exactly the unbounded-block "
                "regression Fix #1 exists to prevent"
            )
            with result_lock:
                got_handle = result.get("handle")
            assert got_handle is None, (
                "try_acquire() on an already-held key must resolve to None "
                "(genuine contention), never silently succeed"
            )
        finally:
            with result_lock:
                competitor = result.get("handle")
            if competitor is not None:
                store.release(competitor)

    def test_try_acquire_of_free_key_still_succeeds_quickly(self, store, unique_key):
        """Sanity companion: the acquire-lock-timeout must not interfere
        with the ordinary, uncontended acquire path."""
        started = time.monotonic()
        handle = store.try_acquire(unique_key, operation="op")
        elapsed = time.monotonic() - started
        assert handle is not None
        try:
            assert elapsed < _PROMPT_CONTENTION_DEADLINE_SECONDS
        finally:
            store.release(handle)


class TestAcquireReleaseCycle:
    def test_after_release_a_new_acquire_of_same_key_succeeds(self, store, unique_key):
        handle1 = store.try_acquire(unique_key, operation="op")
        assert handle1 is not None
        store.release(handle1)

        handle2 = store.try_acquire(unique_key, operation="op")
        assert handle2 is not None
        try:
            pass
        finally:
            store.release(handle2)

    def test_renew_succeeds_and_lock_remains_held(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            competitor = None
            try:
                store.renew(handle)  # must not raise, must not release the lock

                competitor = store.try_acquire(unique_key, operation="op")
                assert competitor is None, "renew() must not release the lock"
            finally:
                if competitor is not None:
                    store.release(competitor)
        finally:
            store.release(handle)

    def test_different_lock_keys_are_independent_while_both_held(self, store):
        """Unlike SQLite's whole-file writer lock, PostgreSQL uses true
        per-row locking -- two DIFFERENT lock_keys must be independently
        acquirable at the same time."""
        key_a = f"test-alias-lock-a-{uuid.uuid4().hex[:12]}"
        key_b = f"test-alias-lock-b-{uuid.uuid4().hex[:12]}"
        handle_a = store.try_acquire(key_a, operation="op")
        assert handle_a is not None
        try:
            handle_b = None
            try:
                handle_b = store.try_acquire(key_b, operation="op")
                assert handle_b is not None, (
                    "expected key_b's acquire to succeed independently of "
                    "key_a (true per-row locking)"
                )
            finally:
                if handle_b is not None:
                    store.release(handle_b)
        finally:
            store.release(handle_a)


class TestOwnershipLossViaWrongToken:
    """Zero-rows-affected DELETE path: forcing a token mismatch on the
    SAME still-open connection must raise, never silently succeed.

    release() with a wrong token still rolls back the WHOLE transaction
    (including the original, never-committed INSERT) and closes the
    connection -- this is release()'s existing, accepted terminal
    behavior: release() ends the lock's lifecycle regardless of whether
    the token was correct, so after a failed release() the lock is
    genuinely free again.

    renew() is different (Fix #3): it is diagnostic-only and must NEVER
    release the lock on a wrong-token call -- see
    TestRenewNeverReleasesTheLockOnFailure below, which supersedes the
    old (incorrect) "renew with wrong token frees the lock" expectation.
    """

    def test_release_with_wrong_token_raises_and_frees_the_lock(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            handle.owner_token = "not-the-real-token"

            with pytest.raises(AliasLockOwnershipLostError):
                store.release(handle)
        finally:
            # release()'s failure path already rolled back and closed
            # the connection on the expected path; this guards against
            # any code path that left it open (nothing to lose either
            # way -- closing an already-closed connection is a no-op).
            if not handle._connection.closed:
                handle._connection.close()

        # The original uncommitted INSERT was discarded by the rollback
        # above -- the row is gone, so a fresh acquire must succeed.
        new_handle = store.try_acquire(unique_key, operation="op")
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)


class TestRenewNeverReleasesTheLockOnFailure:
    """Fix #3: renew() is a diagnostic-only checkpoint (Phase 2 will use
    it as an ownership-loss checkpoint during long-running operations).
    A failed renew() -- e.g. a caller passing a stale/incorrect token by
    mistake -- must raise WITHOUT releasing the underlying transaction:
    the lock stays held, and only the renew-scoped diagnostic UPDATE
    fails. Releasing the real lock on a bad renew() call would recreate
    exactly the "successor takes over while the original holder is still
    active" hazard this whole story exists to eliminate.

    This supersedes the old (incorrect)
    ``test_renew_with_wrong_token_raises_and_frees_the_lock`` expectation.
    """

    def test_renew_with_wrong_token_raises_but_lock_remains_held(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            real_token = handle.owner_token
            handle.owner_token = "not-the-real-token"

            with pytest.raises(AliasLockOwnershipLostError):
                store.renew(handle)

            # The lock must STILL be held: a competing acquire of the
            # same key must fail, proving the original transaction was
            # never rolled back by the failed renew() call.
            competitor = store.try_acquire(unique_key, operation="op")
            try:
                assert competitor is None, (
                    "renew() with a wrong token must NOT release the lock "
                    "-- the original transaction must remain open"
                )
            finally:
                if competitor is not None:
                    store.release(competitor)

            # Restore the real token so the outer finally can release
            # cleanly, proving the connection/transaction is still fully
            # usable after the failed renew() call.
            handle.owner_token = real_token
        finally:
            store.release(handle)


class TestOwnershipLossViaSeveredConnection:
    """Simulate a crash: close the holder's connection out from under it,
    then attempt release()/renew(). Must fail cleanly and loudly rather
    than silently succeeding. There is no store-level pool or shared
    resource to tear down afterward: PostgresAliasLockStore holds no
    persistent connection of its own beyond schema initialization (closed
    immediately in __init__) -- every acquire opens and owns exactly one
    dedicated connection, which in these tests IS the resource being
    (deliberately) severed. The finally blocks below guard against a
    close() failure leaving the connection in an ambiguous state."""

    def test_release_after_severed_connection_raises_loudly(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            handle._connection.close()  # simulate crash
            with pytest.raises(AliasLockOwnershipLostError):
                store.release(handle)
        finally:
            if not handle._connection.closed:
                handle._connection.close()

    def test_renew_after_severed_connection_raises_loudly(self, store, unique_key):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            handle._connection.close()  # simulate crash
            with pytest.raises(AliasLockOwnershipLostError):
                store.renew(handle)
        finally:
            if not handle._connection.closed:
                handle._connection.close()


class TestOwnershipLossViaServerSideDisconnect:
    """Fix #4: a LOCALLY-closed connection (``conn.closed is True``) was
    already correctly normalized to AliasLockOwnershipLostError. A
    SERVER-SIDE disconnect (the backend process is killed out from under
    a still-open client connection, e.g. an admin ``pg_terminate_backend``
    or an idle-in-transaction timeout) is a DIFFERENT failure mode:
    ``conn.closed`` stays False locally, and the next operation on the
    connection raises a raw ``psycopg.errors.AdminShutdown`` (a subclass
    of ``psycopg.OperationalError``) instead. Both release() and renew()
    must normalize this the same way as a locally-closed connection --
    never leak the raw psycopg exception. The finally blocks guard
    against the (unexpected) case where the connection is somehow still
    usable after the backend was terminated."""

    @staticmethod
    def _terminate_backend_server_side(dsn: str, handle: AliasLockHandle) -> None:
        """Use a SEPARATE connection to kill, by EXACT backend_pid, the
        PostgreSQL backend process owning `handle`'s connection --
        simulating a server-side disconnect without touching
        conn.closed locally. Uses the handle's own connection.info
        .backend_pid rather than any query-text match, since
        pg_stat_activity.query holds the parameterized SQL statement
        text, never the bound lock_key value."""
        import psycopg

        backend_pid = handle._connection.info.backend_pid
        with psycopg.connect(dsn) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
            killer.commit()

    @staticmethod
    def _poll_for_ownership_lost(operation_fn, deadline_seconds: float) -> None:
        """Poll operation_fn() while it keeps silently succeeding (the
        server-side termination has not taken effect on this connection
        YET -- a genuine, expected race with pg_terminate_backend), but
        fail IMMEDIATELY the moment it raises anything other than
        AliasLockOwnershipLostError. This is deliberately intolerant of
        raw exceptions on retries: empirically, psycopg leaves
        `conn.closed` False DURING the failing call and only flips it to
        True as a side effect AFTERWARD, so a version of this helper
        that tolerated (and retried past) a raw exception would silently
        mask Fix #4's real defect -- catching it only on a SECOND
        attempt via the connection's now-flipped `.closed`, never on the
        first attempt where the normalization is actually supposed to
        happen."""
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            try:
                operation_fn()
            except AliasLockOwnershipLostError:
                return  # correctly normalized -- test passes
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    "operation leaked a RAW exception instead of "
                    "AliasLockOwnershipLostError after a server-side "
                    f"disconnect: {exc!r}"
                ) from exc
            else:
                time.sleep(_CRASH_RECOVERY_POLL_SLEEP_SECONDS)
                continue  # backend not yet terminated -- try again
        raise AssertionError(
            "operation never raised AliasLockOwnershipLostError after a "
            f"server-side disconnect within {deadline_seconds}s"
        )

    def test_release_after_server_side_disconnect_raises_ownership_lost(
        self, store, unique_key, pg_dsn
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            self._terminate_backend_server_side(pg_dsn, handle)
            self._poll_for_ownership_lost(
                lambda: store.release(handle), _GENEROUS_ACQUIRE_TIMEOUT_SECONDS
            )
        finally:
            if not handle._connection.closed:
                handle._connection.close()

    def test_renew_after_server_side_disconnect_raises_ownership_lost(
        self, store, unique_key, pg_dsn
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            self._terminate_backend_server_side(pg_dsn, handle)
            self._poll_for_ownership_lost(
                lambda: store.renew(handle), _GENEROUS_ACQUIRE_TIMEOUT_SECONDS
            )
        finally:
            if not handle._connection.closed:
                handle._connection.close()


class TestCrashRecovery:
    def test_new_acquisition_succeeds_after_holder_connection_dies(
        self, store, unique_key
    ):
        """No TTL: killing the holder's connection must eventually free
        the lock (PostgreSQL rolls back the abandoned session's
        transaction), and a new acquire must then succeed."""
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        competitor = None
        try:
            competitor = store.try_acquire(unique_key, operation="op")
            assert competitor is None
        finally:
            if competitor is not None:
                store.release(competitor)
            handle._connection.close()  # simulate crash

        new_handle = _acquire_until(
            store, unique_key, _GENEROUS_ACQUIRE_TIMEOUT_SECONDS
        )
        assert new_handle is not None, (
            "expected crash recovery to free the lock within "
            f"{_GENEROUS_ACQUIRE_TIMEOUT_SECONDS}s"
        )
        try:
            pass
        finally:
            store.release(new_handle)

    def test_uncommitted_insert_never_becomes_visible_after_crash(
        self, store, unique_key
    ):
        handle = store.try_acquire(unique_key, operation="op")
        assert handle is not None
        try:
            pass
        finally:
            handle._connection.close()  # crash before commit -> server rollback

        new_handle = _acquire_until(
            store, unique_key, _GENEROUS_ACQUIRE_TIMEOUT_SECONDS
        )
        assert new_handle is not None
        try:
            pass
        finally:
            store.release(new_handle)
