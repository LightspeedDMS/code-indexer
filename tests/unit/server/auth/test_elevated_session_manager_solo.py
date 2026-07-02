"""
Tests for ElevatedSessionManager - SQLite (solo) backend (Story #923 AC1).
Also covers cluster-mode wiring: set_connection_pool(), data-dir path, PG backend.
"""

import sqlite3
import time

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager

# Named constants shared across test sections
_DEFAULT_IDLE_TIMEOUT = 300
_DEFAULT_MAX_AGE = 1800
_ZERO_TIMEOUT = 0
_NEGATIVE_MAX_AGE = -1
_LARGE_IDLE = 600
_SMALLER_MAX_AGE = 300  # less than _LARGE_IDLE - triggers the invariant

# Test data constants
_SESSION_KEY_A = "session_abc"
_SESSION_KEY_B = "session_def"
_USERNAME_ADMIN = "admin"
_IP_LOCAL = "127.0.0.1"
_SCOPE_FULL = "full"
_SCOPE_REPAIR = "totp_repair"


@pytest.fixture
def manager(tmp_path):
    return ElevatedSessionManager(
        idle_timeout_seconds=_DEFAULT_IDLE_TIMEOUT,
        max_age_seconds=_DEFAULT_MAX_AGE,
        db_path=str(tmp_path / "elevated_sessions.db"),
    )


def test_init_rejects_zero_idle_timeout():
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        ElevatedSessionManager(
            idle_timeout_seconds=_ZERO_TIMEOUT,
            max_age_seconds=_DEFAULT_MAX_AGE,
        )


def test_init_rejects_negative_max_age():
    with pytest.raises(ValueError, match="max_age_seconds"):
        ElevatedSessionManager(
            idle_timeout_seconds=_DEFAULT_IDLE_TIMEOUT,
            max_age_seconds=_NEGATIVE_MAX_AGE,
        )


def test_init_rejects_max_age_less_than_idle():
    with pytest.raises(ValueError, match="max_age_seconds.*>="):
        ElevatedSessionManager(
            idle_timeout_seconds=_LARGE_IDLE,
            max_age_seconds=_SMALLER_MAX_AGE,
        )


# ------------------------------------------------------------------
# create() - behavior
# ------------------------------------------------------------------


def test_create_stores_session(manager):
    now = time.time()
    manager.create(
        session_key=_SESSION_KEY_A,
        username=_USERNAME_ADMIN,
        elevated_from_ip=_IP_LOCAL,
    )
    session = manager.get_status(_SESSION_KEY_A)
    assert session is not None
    assert session.session_key == _SESSION_KEY_A
    assert session.username == _USERNAME_ADMIN
    assert session.elevated_from_ip == _IP_LOCAL
    assert session.scope == _SCOPE_FULL
    assert session.elevated_at >= now
    assert session.last_touched_at >= now


def test_create_default_scope_is_full(manager):
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    session = manager.get_status(_SESSION_KEY_A)
    assert session is not None
    assert session.scope == _SCOPE_FULL


def test_create_custom_scope(manager):
    manager.create(_SESSION_KEY_B, _USERNAME_ADMIN, _IP_LOCAL, scope=_SCOPE_REPAIR)
    session = manager.get_status(_SESSION_KEY_B)
    assert session is not None
    assert session.scope == _SCOPE_REPAIR


# Additional constants for re-elevation and input-validation tests
_SLEEP_FOR_REELEVATION = 0.05
_IP_REMOTE = "10.0.0.1"
_EMPTY_SESSION_KEY = ""
_EMPTY_USERNAME = ""
_ERR_SESSION_KEY = "session_key"
_ERR_USERNAME = "username"
_INVALID_SCOPE = "not_a_valid_scope"
_ERR_SCOPE = "scope"
_ERR_ELEVATED_FROM_IP = "elevated_from_ip"
_INVALID_IP_TYPE = 12345  # non-string, non-None


def test_create_rejects_invalid_scope(manager):
    with pytest.raises(ValueError, match=_ERR_SCOPE):
        manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL, scope=_INVALID_SCOPE)


def test_create_rejects_non_string_elevated_from_ip(manager):
    with pytest.raises(ValueError, match=_ERR_ELEVATED_FROM_IP):
        manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _INVALID_IP_TYPE)


def test_touch_rejects_empty_session_key(manager):
    with pytest.raises(ValueError, match=_ERR_SESSION_KEY):
        manager.touch_atomic(_EMPTY_SESSION_KEY)


def test_revoke_rejects_empty_session_key(manager):
    with pytest.raises(ValueError, match=_ERR_SESSION_KEY):
        manager.revoke(_EMPTY_SESSION_KEY)


def test_revoke_all_rejects_empty_username(manager):
    with pytest.raises(ValueError, match=_ERR_USERNAME):
        manager.revoke_all_for_username(_EMPTY_USERNAME)


def test_create_reelevation_is_atomic(manager):
    """Re-elevating the same key atomically replaces elevated_at and source IP."""
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL, scope=_SCOPE_FULL)
    s1 = manager.get_status(_SESSION_KEY_A)
    assert s1 is not None
    t1 = s1.elevated_at

    time.sleep(_SLEEP_FOR_REELEVATION)
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_REMOTE, scope=_SCOPE_FULL)
    s2 = manager.get_status(_SESSION_KEY_A)
    assert s2 is not None
    assert s2.elevated_at >= t1
    assert s2.elevated_from_ip == _IP_REMOTE


def test_create_rejects_empty_session_key(manager):
    with pytest.raises(ValueError, match=_ERR_SESSION_KEY):
        manager.create(_EMPTY_SESSION_KEY, _USERNAME_ADMIN, _IP_LOCAL)


def test_create_rejects_empty_username(manager):
    with pytest.raises(ValueError, match=_ERR_USERNAME):
        manager.create(_SESSION_KEY_A, _EMPTY_USERNAME, _IP_LOCAL)


# ------------------------------------------------------------------
# get_status() - read-only behavior
# ------------------------------------------------------------------


def test_get_status_does_not_touch(manager):
    """Multiple reads must not advance last_touched_at."""
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    s1 = manager.get_status(_SESSION_KEY_A)
    assert s1 is not None
    t1 = s1.last_touched_at

    s2 = manager.get_status(_SESSION_KEY_A)
    assert s2 is not None
    assert s2.last_touched_at == t1


def test_get_status_returns_none_for_missing_key(manager):
    assert manager.get_status(_SESSION_KEY_A) is None


def test_get_status_rejects_empty_key(manager):
    with pytest.raises(ValueError, match=_ERR_SESSION_KEY):
        manager.get_status(_EMPTY_SESSION_KEY)


# ------------------------------------------------------------------
# ElevatedSession dataclass
# ------------------------------------------------------------------

from code_indexer.server.auth.elevated_session_manager import ElevatedSession  # noqa: E402


def test_elevated_session_dataclass_fields():
    now = time.time()
    session = ElevatedSession(
        session_key=_SESSION_KEY_A,
        username=_USERNAME_ADMIN,
        elevated_at=now,
        last_touched_at=now,
        elevated_from_ip=_IP_LOCAL,
        scope=_SCOPE_FULL,
    )
    assert session.session_key == _SESSION_KEY_A
    assert session.username == _USERNAME_ADMIN
    assert session.elevated_at == now
    assert session.last_touched_at == now
    assert session.elevated_from_ip == _IP_LOCAL
    assert session.scope == _SCOPE_FULL


def test_elevated_session_optional_ip():
    now = time.time()
    session = ElevatedSession(
        session_key=_SESSION_KEY_B,
        username=_USERNAME_ADMIN,
        elevated_at=now,
        last_touched_at=now,
    )
    assert session.elevated_from_ip is None
    assert session.scope == _SCOPE_FULL


# ------------------------------------------------------------------
# touch_atomic() - fixture and constants
# ------------------------------------------------------------------

_SHORT_IDLE_TIMEOUT = 2  # seconds - for idle-expiry tests
_SHORT_MAX_AGE = 4  # seconds - must be >= _SHORT_IDLE_TIMEOUT
_SLEEP_FOR_IDLE_EXPIRY = 2.5  # > _SHORT_IDLE_TIMEOUT; margin for loaded CI
_SLEEP_BETWEEN_TOUCHES = (
    1.2  # < _SHORT_IDLE_TIMEOUT (2s); 4 * 1.2 = 4.8 > _SHORT_MAX_AGE (4s)
)
_ABS_EXPIRY_TOUCH_COUNT = 4  # total touches; last one fires after ~4.8 s > max_age (4s)


@pytest.fixture
def short_manager(tmp_path):
    """Manager with short timeouts for expiry tests."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_SHORT_IDLE_TIMEOUT,
        max_age_seconds=_SHORT_MAX_AGE,
        db_path=str(tmp_path / "short_elevated_sessions.db"),
    )


def test_touch_advances_last_touched(manager):
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    s1 = manager.get_status(_SESSION_KEY_A)
    assert s1 is not None
    old_touched = s1.last_touched_at

    time.sleep(_SLEEP_FOR_REELEVATION)
    result = manager.touch_atomic(_SESSION_KEY_A)
    assert result is not None
    assert result.last_touched_at > old_touched


def test_touch_returns_none_for_missing_key(manager):
    assert manager.touch_atomic(_SESSION_KEY_A) is None


@pytest.mark.slow
def test_touch_returns_none_after_idle_expiry(short_manager):
    short_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    time.sleep(_SLEEP_FOR_IDLE_EXPIRY)
    assert short_manager.touch_atomic(_SESSION_KEY_A) is None


@pytest.mark.slow
def test_touch_returns_none_after_absolute_max_age(short_manager):
    """Touches each shorter than idle_timeout eventually fail once max_age elapses."""
    short_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    for i in range(_ABS_EXPIRY_TOUCH_COUNT):
        time.sleep(_SLEEP_BETWEEN_TOUCHES)
        result = short_manager.touch_atomic(_SESSION_KEY_A)
        if i < _ABS_EXPIRY_TOUCH_COUNT - 1:
            assert result is not None, (
                f"Touch {i} should succeed (elapsed ~{(i + 1) * _SLEEP_BETWEEN_TOUCHES:.1f}s)"
            )
        else:
            assert result is None, (
                "Touch 3 must fail: cumulative elapsed exceeds max_age"
            )


# ------------------------------------------------------------------
# revoke() - behavior
# ------------------------------------------------------------------


def test_revoke_removes_session(manager):
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    assert manager.get_status(_SESSION_KEY_A) is not None
    manager.revoke(_SESSION_KEY_A)
    assert manager.get_status(_SESSION_KEY_A) is None


def test_revoke_missing_key_is_noop(manager):
    manager.revoke(_SESSION_KEY_A)  # Must not raise


def test_touch_returns_none_after_revoke(manager):
    manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    manager.revoke(_SESSION_KEY_A)
    assert manager.touch_atomic(_SESSION_KEY_A) is None


# ------------------------------------------------------------------
# revoke_all_for_username() - constants and tests
# ------------------------------------------------------------------

_USERNAME_ALICE = "alice"
_USERNAME_BOB = "bob"
_SESSION_KEY_C = "session_ghi"
_UNKNOWN_USER = "no_such_user"


def test_revoke_all_removes_all_sessions_for_user(manager):
    manager.create(_SESSION_KEY_A, _USERNAME_ALICE, _IP_LOCAL)
    manager.create(_SESSION_KEY_B, _USERNAME_ALICE, _IP_REMOTE)
    manager.create(_SESSION_KEY_C, _USERNAME_BOB, _IP_LOCAL)

    manager.revoke_all_for_username(_USERNAME_ALICE)

    assert manager.get_status(_SESSION_KEY_A) is None
    assert manager.get_status(_SESSION_KEY_B) is None
    assert manager.get_status(_SESSION_KEY_C) is not None


def test_revoke_all_noop_for_unknown_user(manager):
    manager.revoke_all_for_username(_UNKNOWN_USER)  # Must not raise


def test_revoke_all_does_not_affect_other_users(manager):
    manager.create(_SESSION_KEY_A, _USERNAME_ALICE, _IP_LOCAL)
    manager.create(_SESSION_KEY_B, _USERNAME_BOB, _IP_REMOTE)

    manager.revoke_all_for_username(_USERNAME_ALICE)

    assert manager.get_status(_SESSION_KEY_B) is not None


# ------------------------------------------------------------------
# Concurrency stress test
# ------------------------------------------------------------------

import queue as _queue  # noqa: E402
import threading as _threading  # noqa: E402

_CONCURRENCY_THREADS = 50
_CONCURRENCY_OPS_PER_THREAD = 5


@pytest.mark.slow
def test_concurrent_create_touch_revoke_no_exceptions(manager):
    """50 threads doing interleaved create/touch/revoke must not raise."""
    errors: _queue.Queue = _queue.Queue()

    def _worker(thread_idx: int) -> None:
        key = f"conc_key_{thread_idx}"
        try:
            for _ in range(_CONCURRENCY_OPS_PER_THREAD):
                manager.create(key, _USERNAME_ADMIN, _IP_LOCAL)
                manager.touch_atomic(key)
                manager.revoke(key)
        except Exception as exc:  # noqa: BLE001
            errors.put(exc)

    threads = [
        _threading.Thread(target=_worker, args=(i,), daemon=True)
        for i in range(_CONCURRENCY_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # All threads must have terminated — no deadlocks or stuck ops
    stuck = [t for t in threads if t.is_alive()]
    assert not stuck, f"{len(stuck)} thread(s) still running after timeout"

    # Drain error queue via safe API
    collected_errors = []
    while True:
        try:
            collected_errors.append(errors.get_nowait())
        except _queue.Empty:
            break
    assert not collected_errors, f"Exceptions raised: {collected_errors}"

    # Post-condition: every per-thread key was revoked on last iteration
    for i in range(_CONCURRENCY_THREADS):
        key = f"conc_key_{i}"
        assert manager.get_status(key) is None, (
            f"Key {key} not revoked after stress run"
        )


# ===========================================================================
# Cluster-mode pool wiring (Story #923 Codex Fix 2+3)
# ===========================================================================

# ---------------------------------------------------------------------------
# SQLite-backed psycopg-style pool adapter for testing PG code paths
# ---------------------------------------------------------------------------

_ESM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS elevated_sessions (
    session_key TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    elevated_at REAL NOT NULL,
    last_touched_at REAL NOT NULL,
    elevated_from_ip TEXT,
    scope TEXT DEFAULT 'full'
);
CREATE INDEX IF NOT EXISTS idx_elevated_sessions_last_touched
    ON elevated_sessions(last_touched_at);
CREATE INDEX IF NOT EXISTS idx_elevated_sessions_username
    ON elevated_sessions(username);
"""


class _EsmPgConn:
    """SQLite connection presenting psycopg3-style interface for ESM PG tests."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn
        self._conn.row_factory = sqlite3.Row
        # Allow production code to set row_factory without side effects
        self.row_factory = None

    @staticmethod
    def _translate(query: str) -> str:
        return query.replace("%s", "?")

    def _run(self, sql: str, params=None) -> sqlite3.Cursor:
        """Execute an already-translated SQL string against the underlying connection."""
        return self._conn.execute(sql, params) if params else self._conn.execute(sql)

    @staticmethod
    def _returning_session_key(query_no_returning: str, params) -> str:
        """Return the session_key value for a RETURNING re-SELECT.

        Validates table == elevated_sessions and derives session_key as the
        first WHERE param (immediately after the SET params). Raises RuntimeError
        on any parse failure — no silent fallbacks.
        """
        import re

        table_match = re.search(
            r"\bUPDATE\s+(\w+)\b", query_no_returning, re.IGNORECASE
        )
        set_match = re.search(
            r"\bSET\b(.*?)\bWHERE\b", query_no_returning, re.IGNORECASE | re.DOTALL
        )
        if not table_match or not set_match:
            raise RuntimeError(
                "_EsmPgConn: cannot emulate RETURNING — unrecognised UPDATE shape: "
                f"{query_no_returning!r}"
            )
        table = table_match.group(1)
        if table != "elevated_sessions":
            raise RuntimeError(
                "_EsmPgConn: RETURNING emulation only supports elevated_sessions, "
                f"got {table!r}"
            )
        set_param_count = set_match.group(1).count("%s")
        if not params or len(params) <= set_param_count:
            raise RuntimeError(
                f"_EsmPgConn: expected at least {set_param_count + 1} params "
                f"(SET + session_key…), got {params!r}"
            )
        return str(params[set_param_count])  # first WHERE param is always session_key

    def execute(self, query: str, params=None) -> sqlite3.Cursor:
        import re

        returning_match = re.search(r"\bRETURNING\b", query, re.IGNORECASE)
        if returning_match:
            query_no_ret = query[: returning_match.start()].strip()
            cursor = self._run(self._translate(query_no_ret), params)
            if cursor.rowcount > 0:
                session_key = self._returning_session_key(query_no_ret, params)
                return self._run(
                    "SELECT * FROM elevated_sessions WHERE session_key = ?",
                    (session_key,),
                )
            return self._run("SELECT * FROM elevated_sessions WHERE 0")
        return self._run(self._translate(query), params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


class _EsmPgCtx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._sqlite = conn

    def __enter__(self) -> _EsmPgConn:
        return _EsmPgConn(self._sqlite)

    def __exit__(self, *args) -> None:
        pass


class _EsmPgPool:
    """In-process SQLite pool presenting psycopg3 ConnectionPool interface."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_ESM_SCHEMA_SQL)
        self._conn.commit()

    def connection(self) -> _EsmPgCtx:
        return _EsmPgCtx(self._conn)

    def close(self) -> None:
        self._conn.close()


@pytest.fixture
def pg_pool():
    pool = _EsmPgPool()
    yield pool
    pool.close()


@pytest.fixture
def cluster_manager(pg_pool):
    """ElevatedSessionManager with PG-style pool wired (cluster mode)."""
    mgr = ElevatedSessionManager(
        idle_timeout_seconds=_DEFAULT_IDLE_TIMEOUT,
        max_age_seconds=_DEFAULT_MAX_AGE,
    )
    mgr.set_connection_pool(pg_pool)
    return mgr


# ---------------------------------------------------------------------------
# Pool wiring basics
# ---------------------------------------------------------------------------


def test_pool_is_none_by_default(manager):
    """_pool must be None before set_connection_pool() is called."""
    assert manager._pool is None


def test_set_connection_pool_exists_and_stores_pool(manager, pg_pool):
    """set_connection_pool() must exist, be callable, and store the pool reference."""
    assert hasattr(ElevatedSessionManager, "set_connection_pool")
    assert callable(ElevatedSessionManager.set_connection_pool)
    manager.set_connection_pool(pg_pool)
    assert manager._pool is pg_pool


# ---------------------------------------------------------------------------
# Data-dir path (HIGH fix: CIDX_DATA_DIR replaces tempfile)
# ---------------------------------------------------------------------------


def test_db_path_uses_cidx_data_dir_env_var(tmp_path, monkeypatch):
    """When CIDX_DATA_DIR is set, the SQLite DB must be placed inside it."""
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    mgr = ElevatedSessionManager()
    assert mgr._db_path.startswith(str(tmp_path))
    assert "elevated_sessions" in mgr._db_path


def test_db_path_constructor_override_wins_over_env_var(tmp_path, monkeypatch):
    """Constructor db_path must win even when CIDX_DATA_DIR is also set."""
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "from_env"))
    custom_path = str(tmp_path / "custom.db")
    mgr = ElevatedSessionManager(db_path=custom_path)
    assert mgr._db_path == custom_path


# ---------------------------------------------------------------------------
# PG backend — create() + get_status() happy path
# ---------------------------------------------------------------------------


def test_cluster_create_stores_session(cluster_manager):
    """create() via PG pool must persist session retrievable by get_status()."""
    cluster_manager.create(
        session_key=_SESSION_KEY_A,
        username=_USERNAME_ADMIN,
        elevated_from_ip=_IP_LOCAL,
        scope=_SCOPE_FULL,
    )
    session = cluster_manager.get_status(_SESSION_KEY_A)
    assert session is not None
    assert session.session_key == _SESSION_KEY_A
    assert session.username == _USERNAME_ADMIN
    assert session.scope == _SCOPE_FULL


def test_cluster_create_idempotent_on_conflict(cluster_manager):
    """create() called twice must upsert: second call wins."""
    cluster_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    cluster_manager.create(
        _SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL, scope=_SCOPE_REPAIR
    )
    session = cluster_manager.get_status(_SESSION_KEY_A)
    assert session is not None
    assert session.scope == _SCOPE_REPAIR


# ---------------------------------------------------------------------------
# PG backend — touch_atomic()
# ---------------------------------------------------------------------------


def test_cluster_touch_atomic_returns_updated_session(cluster_manager):
    """touch_atomic() via PG pool must return the updated session."""
    cluster_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    result = cluster_manager.touch_atomic(_SESSION_KEY_A)
    assert result is not None
    assert result.session_key == _SESSION_KEY_A


def test_cluster_touch_atomic_returns_none_for_missing_key(cluster_manager):
    """touch_atomic() on an unknown key must return None."""
    result = cluster_manager.touch_atomic("nonexistent-key")
    assert result is None


# ---------------------------------------------------------------------------
# PG backend — revoke()
# ---------------------------------------------------------------------------


def test_cluster_revoke_removes_session(cluster_manager):
    """revoke() via PG pool must delete the session."""
    cluster_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    cluster_manager.revoke(_SESSION_KEY_A)
    assert cluster_manager.get_status(_SESSION_KEY_A) is None


# ---------------------------------------------------------------------------
# PG backend — revoke_all_for_username()
# ---------------------------------------------------------------------------


def test_cluster_revoke_all_for_username_removes_all_sessions(cluster_manager):
    """revoke_all_for_username() via PG pool must remove all sessions for user."""
    cluster_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    cluster_manager.create(_SESSION_KEY_B, _USERNAME_ADMIN, _IP_LOCAL)
    cluster_manager.revoke_all_for_username(_USERNAME_ADMIN)
    assert cluster_manager.get_status(_SESSION_KEY_A) is None
    assert cluster_manager.get_status(_SESSION_KEY_B) is None


def test_cluster_revoke_all_does_not_affect_other_users(cluster_manager):
    """revoke_all_for_username() must not delete other users' sessions."""
    other_user = "other_admin"
    cluster_manager.create(_SESSION_KEY_A, _USERNAME_ADMIN, _IP_LOCAL)
    cluster_manager.create(_SESSION_KEY_B, other_user, _IP_LOCAL)
    cluster_manager.revoke_all_for_username(_USERNAME_ADMIN)
    assert cluster_manager.get_status(_SESSION_KEY_B) is not None
