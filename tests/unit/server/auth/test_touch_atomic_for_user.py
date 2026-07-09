"""Tests for ElevatedSessionManager.touch_atomic_for_user (security fix).

AC1: touch_atomic_for_user with correct owner succeeds and returns the session (SQLite).
AC2: touch_atomic_for_user with wrong username returns None even when session exists
     and is valid (SQLite). This proves the cross-user bypass is closed.
AC3: touch_atomic_for_user with empty session_key raises ValueError.
AC4: touch_atomic_for_user with empty username raises ValueError.
AC5: touch_atomic_for_user with correct owner on expired session (idle timeout) returns None.
AC6: _PgBackend.touch_atomic_for_user sets conn.row_factory = dict_row before the
     RETURNING query — prevents TypeError when psycopg3 returns tuples by default.
AC7: _PgBackend.touch_atomic uses UPDATE...RETURNING in a single statement —
     eliminates the TOCTOU window present in the prior UPDATE + separate SELECT pattern.
"""

import time

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_IDLE = 300
_MAX_AGE = 1800
_SESSION_KEY = "session-key-owner-test"
_USERNAME_A = "admin_a"
_USERNAME_B = "admin_b"
_IP_LOCAL = "127.0.0.1"


@pytest.fixture
def manager(tmp_path):
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elev_touch_user.db"),
    )


# ---------------------------------------------------------------------------
# AC1: correct owner succeeds and returns the session
# ---------------------------------------------------------------------------


def test_ac1_correct_owner_succeeds(manager):
    """touch_atomic_for_user with the correct owner returns a valid ElevatedSession."""
    manager.create(_SESSION_KEY, _USERNAME_A, _IP_LOCAL, scope="full")

    result = manager.touch_atomic_for_user(_SESSION_KEY, _USERNAME_A)

    assert result is not None, (
        "touch_atomic_for_user should return ElevatedSession when owner matches"
    )
    assert result.session_key == _SESSION_KEY
    assert result.username == _USERNAME_A


# ---------------------------------------------------------------------------
# AC2: wrong username returns None (cross-user bypass closed)
# ---------------------------------------------------------------------------


def test_ac2_wrong_username_returns_none(manager):
    """touch_atomic_for_user with wrong username returns None and does NOT extend the victim's window.

    Two assertions are required:
    1. Return value is None (gate closes for wrong owner).
    2. last_touched_at is unchanged (UPDATE did not mutate the victim's row).

    A buggy implementation that filters only the SELECT by username while the
    UPDATE mutates unconditionally would pass assertion 1 but fail assertion 2.
    """
    manager.create(_SESSION_KEY, _USERNAME_A, _IP_LOCAL, scope="full")
    before = manager.get_status(_SESSION_KEY)
    assert before is not None

    before_touched = before.last_touched_at

    # Small sleep so that a spurious UPDATE would produce a different timestamp.
    time.sleep(0.05)

    result = manager.touch_atomic_for_user(_SESSION_KEY, _USERNAME_B)

    assert result is None, (
        "touch_atomic_for_user must return None when username does not match "
        "the session owner — cross-user bypass must be closed"
    )

    after = manager.get_status(_SESSION_KEY)
    assert after is not None, "victim session must still exist"
    assert after.last_touched_at == before_touched, (
        "wrong-owner touch must NOT advance last_touched_at on the victim's session"
    )


# ---------------------------------------------------------------------------
# AC3: empty session_key raises ValueError
# ---------------------------------------------------------------------------


def test_ac3_empty_session_key_raises():
    """touch_atomic_for_user with empty session_key raises ValueError."""
    mgr = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
    )
    with pytest.raises(ValueError, match="session_key"):
        mgr.touch_atomic_for_user("", _USERNAME_A)


# ---------------------------------------------------------------------------
# AC4: empty username raises ValueError
# ---------------------------------------------------------------------------


def test_ac4_empty_username_raises():
    """touch_atomic_for_user with empty username raises ValueError."""
    mgr = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
    )
    with pytest.raises(ValueError, match="username"):
        mgr.touch_atomic_for_user(_SESSION_KEY, "")


# ---------------------------------------------------------------------------
# AC5: expired session (idle timeout) returns None even for correct owner
# ---------------------------------------------------------------------------


def test_ac5_expired_session_returns_none(tmp_path):
    """touch_atomic_for_user returns None when the session has exceeded idle timeout."""
    # Very short idle timeout so we can expire the session quickly
    short_manager = ElevatedSessionManager(
        idle_timeout_seconds=1,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elev_expired.db"),
    )
    short_manager.create(_SESSION_KEY, _USERNAME_A, _IP_LOCAL, scope="full")

    # Wait for idle timeout to expire
    time.sleep(1.1)

    result = short_manager.touch_atomic_for_user(_SESSION_KEY, _USERNAME_A)

    assert result is None, (
        "touch_atomic_for_user must return None when session has exceeded idle timeout, "
        "even when the owner matches"
    )


# ---------------------------------------------------------------------------
# Shared helper: build a tracking mock conn + cursor for PG backend tests
# ---------------------------------------------------------------------------


def _make_tracking_conn_and_cursor(fake_row: dict) -> tuple:
    """Build a mock connection and cursor for _PgBackend contract tests.

    The connection:
    - tracks the row_factory kwarg passed to conn.cursor(...)
    - records whether conn.row_factory was ever directly assigned
    - raises AssertionError if conn.execute is called (wrong code path)

    The cursor:
    - is returned from conn.cursor(...) as a context manager
    - counts calls to cur.execute(...)
    - returns fake_row from cur.fetchone()

    Returns (mock_conn, tracking_state) where tracking_state has keys:
      "cursor_row_factory"  : row_factory kwarg passed to conn.cursor()
      "conn_row_factory_set": True if conn.row_factory was directly assigned
      "cursor_execute_count": number of times cur.execute() was called
    """
    import contextlib

    state: dict = {
        "cursor_row_factory": None,
        "conn_row_factory_set": False,
        "cursor_execute_count": 0,
    }

    class _TrackingCursor:
        """Context-manager cursor whose execute counts calls."""

        def execute(self, sql: object, params: object = None) -> "_TrackingCursor":
            state["cursor_execute_count"] += 1
            return self

        def fetchone(self) -> dict:
            return fake_row

        def __enter__(self) -> "_TrackingCursor":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class _TrackingConn:
        """Connection that records cursor() kwarg and rejects row_factory mutation."""

        @property
        def row_factory(self) -> None:  # type: ignore[return]
            return None

        @row_factory.setter
        def row_factory(self, value: object) -> None:
            state["conn_row_factory_set"] = True

        def cursor(self, row_factory: object = None) -> "_TrackingCursor":
            state["cursor_row_factory"] = row_factory
            return _TrackingCursor()

        def execute(self, sql: object, params: object = None) -> None:
            raise AssertionError(
                "conn.execute() must NOT be called — production code uses "
                "conn.cursor(row_factory=dict_row) + cur.execute() instead"
            )

        def commit(self) -> None:
            pass

    class _TrackingPool:
        @contextlib.contextmanager  # type: ignore[misc]
        def connection(self):  # type: ignore[override]
            yield _TrackingConn()

    return _TrackingPool(), state


# ---------------------------------------------------------------------------
# AC6: PostgreSQL _PgBackend uses conn.cursor(row_factory=dict_row)
# ---------------------------------------------------------------------------


def test_ac6_pg_backend_sets_dict_row_factory():
    """_PgBackend.touch_atomic_for_user creates a cursor with row_factory=dict_row.

    The new contract (Bug #1071 fix) is:
      with conn.cursor(row_factory=dict_row) as cur:
          cur.execute(...)
          row = cur.fetchone()
      conn.commit()

    Assertions:
    1. The row_factory kwarg passed to conn.cursor(...) IS dict_row.
    2. conn.row_factory is NEVER directly assigned (no pool pollution).
    3. The returned ElevatedSession is correctly populated from the fake row.
    """
    from code_indexer.server.auth.elevated_session_manager import (
        ElevatedSession,
        _PgBackend,
        dict_row,
    )

    now = time.time()
    fake_row = {
        "session_key": _SESSION_KEY,
        "username": _USERNAME_A,
        "elevated_at": now,
        "last_touched_at": now,
        "elevated_from_ip": _IP_LOCAL,
        "scope": "full",
    }

    mock_pool, state = _make_tracking_conn_and_cursor(fake_row)

    backend = _PgBackend(pool=mock_pool, idle_timeout=_IDLE, max_age=_MAX_AGE)
    result = backend.touch_atomic_for_user(_SESSION_KEY, _USERNAME_A)

    assert state["cursor_row_factory"] is dict_row, (
        "conn.cursor() must be called with row_factory=dict_row; "
        "default psycopg3 factory returns tuples, causing TypeError in "
        "_row_to_elevated_session which uses dict-style key access"
    )
    assert not state["conn_row_factory_set"], (
        "conn.row_factory must NEVER be directly assigned — "
        "that would pollute the shared pool connection (Bug #1071)"
    )
    assert result is not None, "Expected ElevatedSession from mocked RETURNING row"
    assert isinstance(result, ElevatedSession)
    assert result.session_key == _SESSION_KEY
    assert result.username == _USERNAME_A
    assert result.elevated_at == float(now)
    assert result.last_touched_at == float(now)
    assert result.elevated_from_ip == _IP_LOCAL
    assert result.scope == "full"


# ---------------------------------------------------------------------------
# AC7: PostgreSQL _PgBackend.touch_atomic uses UPDATE...RETURNING (no TOCTOU)
# ---------------------------------------------------------------------------


def test_ac7_pg_backend_touch_atomic_uses_returning():
    """_PgBackend.touch_atomic issues a single UPDATE...RETURNING via cur.execute().

    The prior two-step pattern (UPDATE then separate SELECT) had a TOCTOU window:
    a concurrent revoke_all_for_username() between the two statements could leave
    the session deleted while touch_atomic() still returned a valid row.

    Assertions:
    1. cur.execute() is called EXACTLY ONCE (single UPDATE...RETURNING statement).
    2. conn.cursor() is called with row_factory=dict_row (no conn.row_factory mutation).
    3. conn.row_factory is NEVER directly assigned (no pool pollution).
    4. The returned ElevatedSession is correctly populated from the fake row.
    """
    from code_indexer.server.auth.elevated_session_manager import (
        ElevatedSession,
        _PgBackend,
        dict_row,
    )

    now = time.time()
    fake_row = {
        "session_key": _SESSION_KEY,
        "username": _USERNAME_A,
        "elevated_at": now,
        "last_touched_at": now,
        "elevated_from_ip": _IP_LOCAL,
        "scope": "full",
    }

    mock_pool, state = _make_tracking_conn_and_cursor(fake_row)

    backend = _PgBackend(pool=mock_pool, idle_timeout=_IDLE, max_age=_MAX_AGE)
    result = backend.touch_atomic(_SESSION_KEY)

    assert state["cursor_execute_count"] == 1, (
        f"cur.execute() must be called exactly once (UPDATE...RETURNING); "
        f"got {state['cursor_execute_count']} — a second call means the old "
        "two-step UPDATE + SELECT pattern is still present, leaving the TOCTOU "
        "window open"
    )
    assert state["cursor_row_factory"] is dict_row, (
        "conn.cursor() must be called with row_factory=dict_row before the query"
    )
    assert not state["conn_row_factory_set"], (
        "conn.row_factory must NEVER be directly assigned (Bug #1071 anti-pollution)"
    )
    assert result is not None, "Expected ElevatedSession from mocked RETURNING row"
    assert isinstance(result, ElevatedSession)
    assert result.session_key == _SESSION_KEY
    assert result.username == _USERNAME_A
    assert result.elevated_at == float(now)
    assert result.last_touched_at == float(now)
    assert result.elevated_from_ip == _IP_LOCAL
    assert result.scope == "full"
