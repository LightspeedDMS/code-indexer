"""
Regression tests for Bug #1071: _pg_check_locked and _pg_record_failure crash
with KeyError when a pooled psycopg connection has its row_factory set to
dict_row by a prior code path.

Root cause: rate_limiter.py and oauth_rate_limiter.py called
conn.execute(...).fetchone() without pinning the cursor row_factory, so on a
pooled connection previously used with dict_row the result was a dict, causing
row[0] to raise KeyError: 0.

Fix: open an explicit cursor with row_factory=tuple_row so positional access
is deterministic regardless of the connection's ambient row_factory.

Test strategy (pure mock — no SQLite SQL to avoid %s / ON CONFLICT syntax
errors):
- Build a fake pool whose conn.execute() returns a dict-fetchone result
  (simulating ambient dict_row pollution — the OLD broken path).
- conn.cursor(row_factory=tuple_row) returns a context-manager cursor whose
  fetchone() returns a tuple (the FIXED pinned path).
- The fake cursor inspects the SQL to return the right controlled value:
    - "COUNT(*)"    -> (count_value,)
    - "locked_until" -> (locked_until_value,) or None
    - All INSERT/UPDATE/DELETE -> no fetchone needed; cursor is a no-op.
- conn.commit() is a no-op.

Tests use the REAL PasswordChangeRateLimiter and OAuthTokenRateLimiter classes
with set_connection_pool(fake_pool).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Generator, Optional


# ---------------------------------------------------------------------------
# Pure-mock fake pool infrastructure
# ---------------------------------------------------------------------------


class _FakeTupleCursor:
    """
    Context-manager cursor that returns controlled tuple rows.

    Inspects the SQL to determine which value to return:
    - SQL contains "COUNT(*)"    -> returns (count_value,)
    - SQL contains "locked_until" and is a SELECT -> returns locked_until_row
    - Otherwise (INSERT/UPDATE/DELETE) -> fetchone returns None
    """

    def __init__(self, count_value: int, locked_until_row: Optional[tuple]) -> None:
        self._count_value = count_value
        self._locked_until_row = locked_until_row
        self._last_sql: str = ""

    def execute(self, sql: str, params: Any = None) -> "_FakeTupleCursor":
        self._last_sql = sql
        return self

    def fetchone(self) -> Optional[tuple]:
        sql = self._last_sql
        if "COUNT(*)" in sql:
            return (self._count_value,)
        if "locked_until" in sql and sql.strip().upper().startswith("SELECT"):
            return self._locked_until_row
        return None

    def __enter__(self) -> "_FakeTupleCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeDictResult:
    """
    Simulates conn.execute().fetchone() returning a dict (polluted dict_row).
    This is the OLD broken path — the tests confirm the fixed code no longer
    calls this path for SELECT queries.
    """

    def fetchone(self) -> dict:
        # Returns a dict — positional access row[0] would raise KeyError: 0
        return {"locked_until": 99999999.0, "count": 5}


class _FakeConnection:
    """
    Fake psycopg connection simulating dict_row pollution:
    - conn.execute() returns dict rows (old broken path)
    - conn.cursor(row_factory=tuple_row) returns tuple cursor (fixed path)
    - conn.commit() is a no-op
    """

    def __init__(self, count_value: int, locked_until_row: Optional[tuple]) -> None:
        self._count_value = count_value
        self._locked_until_row = locked_until_row

    def execute(self, sql: str, params: Any = None) -> _FakeDictResult:
        """Returns dict rows — simulates polluted dict_row factory (old path)."""
        return _FakeDictResult()

    def cursor(self, row_factory: Any = None) -> _FakeTupleCursor:
        """Returns tuple cursor — simulates pinned row_factory=tuple_row (fix path)."""
        return _FakeTupleCursor(self._count_value, self._locked_until_row)

    def commit(self) -> None:
        pass


class _FakePool:
    """Pool that yields a _FakeConnection."""

    def __init__(self, count_value: int, locked_until_row: Optional[tuple]) -> None:
        self._count_value = count_value
        self._locked_until_row = locked_until_row

    @contextmanager
    def connection(self) -> Generator[_FakeConnection, None, None]:
        yield _FakeConnection(self._count_value, self._locked_until_row)


def _make_pool(
    count_value: int = 0,
    locked_until: Optional[float] = None,
) -> _FakePool:
    """
    Helper: build a fake pool.

    count_value   - what COUNT(*) queries return
    locked_until  - if not None, the locked_until timestamp returned by
                    SELECT locked_until ... queries; if None, fetchone returns None
    """
    locked_until_row: Optional[tuple] = (
        (locked_until,) if locked_until is not None else None
    )
    return _FakePool(count_value, locked_until_row)


# ---------------------------------------------------------------------------
# Tests for PasswordChangeRateLimiter (rate_limiter.py)
# ---------------------------------------------------------------------------


class TestPasswordChangeRateLimiterPgRowFactory:
    """
    Bug #1071 regression: PasswordChangeRateLimiter._pg_* methods must work
    when the pool uses dict_row ambient factory (only the pinned cursor path
    returns tuples now).
    """

    def _make_limiter(self, count_value: int = 0, locked_until: Optional[float] = None):
        from code_indexer.server.auth.rate_limiter import PasswordChangeRateLimiter

        limiter = PasswordChangeRateLimiter()
        limiter.set_connection_pool(_make_pool(count_value, locked_until))
        return limiter

    def test_pg_record_failure_below_threshold_returns_false(self) -> None:
        """
        _pg_record_failure: COUNT below max_attempts (5) → returns False, no crash.
        Before fix: conn.execute().fetchone() returned a dict → row[0] raised KeyError.
        After fix: cursor(row_factory=tuple_row) returns (count,) tuple → works.
        """
        # count=3 is below max_attempts=5
        limiter = self._make_limiter(count_value=3)
        result = limiter.record_failed_attempt("alice")
        assert result is False

    def test_pg_record_failure_at_threshold_returns_true(self) -> None:
        """
        _pg_record_failure: COUNT >= max_attempts (5) → returns True (locked out).
        """
        # count=5 equals max_attempts=5
        limiter = self._make_limiter(count_value=5)
        result = limiter.record_failed_attempt("alice")
        assert result is True

    def test_pg_check_locked_with_active_lockout_returns_message(self) -> None:
        """
        _pg_check_locked: active lockout row present → returns non-None message string.
        Before fix: conn.execute().fetchone() returned dict → row[0] raised KeyError.
        After fix: cursor(row_factory=tuple_row) returns (locked_until,) → works.
        """
        # locked_until is 60 seconds in the future
        future_ts = time.time() + 60.0
        limiter = self._make_limiter(locked_until=future_ts)
        result = limiter.check_rate_limit("alice")
        assert result is not None
        assert "Too many failed attempts" in result

    def test_pg_check_locked_with_no_lockout_returns_none(self) -> None:
        """
        _pg_check_locked: no lockout row (None) → returns None.
        """
        # locked_until=None means fetchone() returns None
        limiter = self._make_limiter(locked_until=None)
        result = limiter.check_rate_limit("alice")
        assert result is None


# ---------------------------------------------------------------------------
# Tests for OAuthTokenRateLimiter (oauth_rate_limiter.py)
# ---------------------------------------------------------------------------


class TestOAuthTokenRateLimiterPgRowFactory:
    """
    Bug #1071 regression: OAuthTokenRateLimiter._pg_* methods must work
    when the pool uses dict_row ambient factory.
    """

    def _make_limiter(self, count_value: int = 0, locked_until: Optional[float] = None):
        from code_indexer.server.auth.oauth_rate_limiter import OAuthTokenRateLimiter

        limiter = OAuthTokenRateLimiter()
        limiter.set_connection_pool(_make_pool(count_value, locked_until))
        return limiter

    def test_pg_record_failure_below_threshold_returns_false(self) -> None:
        """
        _pg_record_failure: COUNT below max_attempts (10) → returns False, no crash.
        """
        # count=7 is below max_attempts=10
        limiter = self._make_limiter(count_value=7)
        result = limiter.record_failed_attempt("client-xyz")
        assert result is False

    def test_pg_record_failure_at_threshold_returns_true(self) -> None:
        """
        _pg_record_failure: COUNT >= max_attempts (10) → returns True (locked out).
        """
        # count=10 equals max_attempts=10
        limiter = self._make_limiter(count_value=10)
        result = limiter.record_failed_attempt("client-xyz")
        assert result is True

    def test_pg_check_locked_with_active_lockout_returns_message(self) -> None:
        """
        _pg_check_locked: active lockout row present → returns non-None message string.
        """
        future_ts = time.time() + 300.0
        limiter = self._make_limiter(locked_until=future_ts)
        result = limiter.check_rate_limit("client-xyz")
        assert result is not None
        assert "Too many failed attempts" in result

    def test_pg_check_locked_with_no_lockout_returns_none(self) -> None:
        """
        _pg_check_locked: no lockout row (None) → returns None.
        """
        limiter = self._make_limiter(locked_until=None)
        result = limiter.check_rate_limit("client-xyz")
        assert result is None
