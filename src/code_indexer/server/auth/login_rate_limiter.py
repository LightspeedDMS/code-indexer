"""Progressive per-username login throttle.

A login is THROTTLED after repeated failures; there is no lock state and no
unlock step.  Every door RESERVES the attempt before it checks the password:

- ``begin_attempt`` first reads the key's row with a plain SELECT (no
  transaction): while its backoff window runs the attempt is refused at once
  (``retry_after_seconds``) -- a correct password included.  A refusal takes
  no database write lock, no process-wide lock and writes nothing.
  Otherwise it counts the attempt in one row-locked transaction (which
  re-checks the window, so a stale "not blocked" read never admits past the
  throttle) and admits it.  Concurrent requests therefore cannot slip past a
  check-then-record gap: at most ``max_attempts`` checks are admitted before
  the first window, and one per window after that;
- the attempt that brings the consecutive count to ``max_attempts`` starts a
  ``base_delay_seconds`` window; each further admitted attempt doubles it,
  capped at ``max_delay_seconds``;
- a successful check calls ``record_success``, which deletes the row (the
  admitted attempt never counts as a failure); attempts older than
  ``window_minutes`` are forgotten.

A throttled username refuses every password attempt until its window ends.
Because the key is the username alone, someone who keeps sending wrong
passwords for an account can keep it throttled; its owner then signs in with
an API key, MCP credentials or SSO instead.

Keys are domain-separated by scope: ``SCOPE_LOGIN`` (the typed username) and
``SCOPE_STEP_UP`` (the authenticated username of a TOTP step-up), so a login
can never throttle someone's step-up, or vice versa.

State is DB-backed so every worker and node sees it: PostgreSQL
(``set_connection_pool``, wired by ``lifespan``) in a cluster; in solo mode
the node's shared ``cidx_server.db`` (``set_sqlite_path``, wired by
``service_init``) through the throttle's OWN long-lived per-thread
connection -- never the shared ``DatabaseConnectionManager`` connection, so
it holds no process-wide lock and never shares a transaction with another
store.  Each reservation is a durable write to that shared database, so a
process caps new login reservations at ``RESERVATIONS_PER_SECOND`` (10/s,
bursts of ``RESERVATION_BURST`` = 10); beyond that an attempt is refused at
once with ``ThrottleStoreBusy`` and the door answers 503 "busy".  This is a
deliberate trade-off: it bounds the login throttle's write load on the
database every other store shares.  Refusals and the success-path delete
take no token.  The reservation transaction itself waits at most
``RESERVATION_BUSY_TIMEOUT_SECONDS`` (2 s) for SQLite's write lock, then
also raises ``ThrottleStoreBusy`` instead of parking the worker.  (A refusal's read can
only wait on SQLite itself, while a commit holds the file in rollback-journal
mode, up to the same bound; cidx_server.db runs in WAL, where reads never
wait.)  An unwired instance keeps a private in-memory SQLite database
(tests, standalone use).

Storage bound: one fixed-size row per key; the key is stored only as its
SHA-256 digest, never the typed text.  The table grows ONLY when a
reservation inserts a new row, and every such insert also deletes up to
``_PRUNE_BATCH`` rows idle for longer than the window (updates of existing
rows prune nothing).  So an insert can grow the table only when no expired
row exists, i.e. when every row had an attempt within the window: the table
never holds more than the peak number of distinct keys with an attempt
inside one window (plus inserts in flight).  ``max_delay_seconds`` < the
window, so a pruned row is never throttling.

Callers never sleep: a refused attempt is answered with 429.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NamedTuple, Optional, Tuple, Union

from code_indexer.server.auth.token_bucket import TokenBucket

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_SECONDS = 5.0
DEFAULT_MAX_DELAY_SECONDS = 120.0
DEFAULT_WINDOW_MINUTES = 15.0
# Longest a solo-mode reservation waits for SQLite's write lock.
RESERVATION_BUSY_TIMEOUT_SECONDS = 2.0
# Throttle key namespaces (closed set; names never contain NUL).
SCOPE_LOGIN = "login"
SCOPE_STEP_UP = "stepup"
_SCOPES = frozenset({SCOPE_LOGIN, SCOPE_STEP_UP})
# Doubling reaches any sane cap long before this; bounds 2**n for huge counts.
_MAX_BACKOFF_EXPONENT = 32
# Expired rows deleted per inserting reservation (bounded work per request).
_PRUNE_BATCH = 100
# At most one "store busy" WARNING per interval (a flood must not flood logs).
_BUSY_WARNING_INTERVAL_SECONDS = 60.0

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_throttle (
    key_hash TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL,
    last_failure_at REAL NOT NULL,
    blocked_until REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_throttle_last_failure
    ON login_throttle(last_failure_at);
"""
_SELECT = (
    "SELECT failure_count, last_failure_at, blocked_until "
    "FROM login_throttle WHERE key_hash = {p}"
)
_UPSERT = (
    "INSERT INTO login_throttle "
    "(key_hash, failure_count, last_failure_at, blocked_until) "
    "VALUES ({p}, {p}, {p}, {p}) ON CONFLICT (key_hash) DO UPDATE SET "
    "failure_count = EXCLUDED.failure_count, "
    "last_failure_at = EXCLUDED.last_failure_at, "
    "blocked_until = EXCLUDED.blocked_until"
)
_DELETE = "DELETE FROM login_throttle WHERE key_hash = {p}"
_SQLITE_PRUNE = (
    "DELETE FROM login_throttle WHERE key_hash IN ("
    "SELECT key_hash FROM login_throttle WHERE last_failure_at < ? LIMIT ?)"
)
# SKIP LOCKED: concurrent prunes on different nodes never wait on (or
# deadlock with) each other or with a reservation in progress.
_PG_PRUNE = (
    "DELETE FROM login_throttle WHERE key_hash IN ("
    "SELECT key_hash FROM login_throttle WHERE last_failure_at < %s "
    "LIMIT %s FOR UPDATE SKIP LOCKED)"
)
# Creates the row a first reservation locks (FOR UPDATE cannot lock an
# absent row); last_failure_at = 0 reads as "no recent attempts".
_PG_PLACEHOLDER = (
    "INSERT INTO login_throttle "
    "(key_hash, failure_count, last_failure_at, blocked_until) "
    "VALUES (%s, 0, 0, 0) ON CONFLICT (key_hash) DO NOTHING"
)


class ThrottleStoreBusy(Exception):
    """The solo-mode throttle store stayed write-locked past its bound.

    The door answers 503 "try again shortly" instead of parking the worker.
    """


class AttemptOutcome(NamedTuple):
    """Result of reserving one login attempt."""

    # False: refused while the backoff window runs; do not check the password.
    admitted: bool
    # Seconds until the key's next attempt is admitted (0 when unthrottled).
    retry_after_seconds: float
    # True only for the admitted attempt that reached the threshold.
    throttle_started: bool


class _Row(NamedTuple):
    failure_count: int
    last_failure_at: float
    blocked_until: float


Decide = Callable[[Optional[_Row]], Optional[_Row]]


def throttle_key(subject: str, scope: str = SCOPE_LOGIN) -> str:
    """Fixed-size, scope-separated storage key.

    ``scope + NUL + subject`` is prefix-free across scopes (scope names hold
    no NUL), so no subject of one scope can spell a key of another.  The
    typed text itself is never stored.  ``surrogatepass``: a JSON body can
    carry a lone surrogate, which must be keyed, not raise.
    """
    if scope not in _SCOPES:
        raise ValueError(f"unknown throttle scope: {scope!r}")
    material = f"{scope}\0{subject}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()


def failure_reason(outcome: AttemptOutcome) -> str:
    """Audit reason for a failed admitted check (existing vocabulary)."""
    return "rate_limited" if outcome.throttle_started else "bad_credentials"


def _as_row(raw: Any) -> Optional[_Row]:
    if raw is None:
        return None
    return _Row(int(raw[0]), float(raw[1]), float(raw[2]))


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


# Per-process rate cap on solo-mode reservation transactions (each one is a
# durable write to cidx_server.db, which every other store shares): at most
# RESERVATIONS_PER_SECOND new reservations per process, bursts of
# RESERVATION_BURST.  Beyond that a reservation is refused as busy at once
# (503) -- never a wait for a token.  Refusals (the read path) and the
# success-path delete never take a token.  A deliberate trade-off: no
# setting.
RESERVATIONS_PER_SECOND = 10.0
RESERVATION_BURST = 10
_RESERVATION_BUCKET = TokenBucket(
    capacity=RESERVATION_BURST, refill_rate=RESERVATIONS_PER_SECOND
)
_RESERVATION_BUCKET_LOCK = threading.Lock()  # TokenBucket is not thread-safe


def _take_reservation_token() -> bool:
    """Consume one reservation token now if one is available (never waits)."""
    with _RESERVATION_BUCKET_LOCK:
        allowed, _retry_after = _RESERVATION_BUCKET.consume()
    return allowed


class _SqliteStore:
    """Rows in the node's shared SQLite file, through the throttle's own
    long-lived per-thread connection, or in a private in-memory database
    when *db_path* is None.  A file-backed reservation first takes a token
    from the process's ``_RESERVATION_BUCKET``."""

    def __init__(self, db_path: Optional[str]) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._memory_lock = threading.Lock()
        self._memory: Optional[sqlite3.Connection] = None
        if db_path is None:
            self._memory = sqlite3.connect(
                ":memory:", check_same_thread=False, isolation_level=None
            )
        with self._connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)

    def _thread_connection(self) -> sqlite3.Connection:
        """This thread's own connection, opened once and kept (autocommit;
        SQLite waits at most the reservation bound for a lock)."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            assert self._db_path is not None
            conn = sqlite3.connect(
                self._db_path,
                timeout=RESERVATION_BUSY_TIMEOUT_SECONDS,
                isolation_level=None,
            )
            self._local.conn = conn
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            with self._memory_lock:
                yield self._memory
            return
        try:
            yield self._thread_connection()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise ThrottleStoreBusy(str(exc)) from exc
            raise

    def read(self, key: str) -> Optional[_Row]:
        """Plain SELECT in autocommit: no transaction, no write lock."""
        with self._connect() as conn:
            return _as_row(conn.execute(_SELECT.format(p="?"), (key,)).fetchone())

    def record(
        self, key: str, decide: Decide, prune_before: float
    ) -> Tuple[Optional[_Row], Optional[_Row]]:
        if self._memory is None and not _take_reservation_token():
            raise ThrottleStoreBusy(
                "login reservations are over this process's rate cap"
            )
        with self._connect() as conn:
            if conn.in_transaction:
                raise RuntimeError(
                    "login throttle connection is unexpectedly inside a transaction"
                )
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = _as_row(conn.execute(_SELECT.format(p="?"), (key,)).fetchone())
                new = decide(old)
                if new is not None:
                    conn.execute(_UPSERT.format(p="?"), (key, *new))
                if old is None and new is not None:
                    # Only a new row grows the table: prune expired rows in
                    # the same transaction that inserts it.
                    conn.execute(_SQLITE_PRUNE, (prune_before, _PRUNE_BATCH))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return old, new

    def delete(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute(_DELETE.format(p="?"), (key,))


class _PgStore:
    """Rows in PostgreSQL, shared by every node of the cluster (pool
    connections: no process-wide lock; FOR UPDATE locks only the key's row)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def read(self, key: str) -> Optional[_Row]:
        """Plain SELECT: no row lock, no write."""
        from psycopg.rows import tuple_row

        with self._pool.connection() as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                raw = cur.execute(_SELECT.format(p="%s"), (key,)).fetchone()
        return _as_row(raw)

    def record(
        self, key: str, decide: Decide, prune_before: float
    ) -> Tuple[Optional[_Row], Optional[_Row]]:
        from psycopg.rows import tuple_row

        with self._pool.connection() as conn:
            inserted = conn.execute(_PG_PLACEHOLDER, (key,)).rowcount == 1
            with conn.cursor(row_factory=tuple_row) as cur:
                raw = cur.execute(
                    _SELECT.format(p="%s") + " FOR UPDATE", (key,)
                ).fetchone()
            old = _as_row(raw)
            new = decide(old)
            if new is not None:
                conn.execute(_UPSERT.format(p="%s"), (key, *new))
            conn.commit()
            if inserted:
                # Only a new row grows the table: prune when one landed.
                conn.execute(_PG_PRUNE, (prune_before, _PRUNE_BATCH))
                conn.commit()
        return old, new

    def delete(self, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(_DELETE.format(p="%s"), (key,))
            conn.commit()


class LoginRateLimiter:
    """Per-key progressive throttle over a DB-backed store (see module doc)."""

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        window_minutes: float = DEFAULT_WINDOW_MINUTES,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        window_seconds = window_minutes * 60.0
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if not 0 < base_delay_seconds <= max_delay_seconds:
            raise ValueError("require 0 < base_delay_seconds <= max_delay_seconds")
        if max_delay_seconds >= window_seconds:
            raise ValueError(
                "max_delay_seconds must be shorter than the attempt window, "
                "or a pruned row could still be throttling"
            )
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._window_seconds = window_seconds
        self._enabled = enabled
        self._clock = clock
        self._lock = threading.Lock()  # guards store re-wiring only
        self._warn_lock = threading.Lock()  # guards the warning timestamp only
        self._last_busy_warning = float("-inf")
        self._pool: Optional[Any] = None
        self._store: Union[_SqliteStore, _PgStore] = _SqliteStore(None)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def set_connection_pool(self, pool: Any) -> None:
        """Cluster mode: keep throttle state in PostgreSQL (all nodes)."""
        with self._lock:
            self._pool = pool
            self._store = _PgStore(pool)
        logger.info("LoginRateLimiter: using PostgreSQL connection pool (cluster mode)")

    def set_sqlite_path(self, db_path: str) -> None:
        """Solo mode: keep throttle state in the node's shared SQLite file so
        every uvicorn worker sees it.  No-op once a PostgreSQL pool is wired."""
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path must be a non-empty string")
        with self._lock:
            if self._pool is not None:
                return
            self._store = _SqliteStore(db_path)

    def _current_store(self) -> Union[_SqliteStore, _PgStore]:
        with self._lock:
            return self._store

    def _warn_store_busy(self, action: str) -> None:
        now = time.monotonic()
        with self._warn_lock:
            if now - self._last_busy_warning < _BUSY_WARNING_INTERVAL_SECONDS:
                return
            self._last_busy_warning = now
        logger.warning(
            "Login throttle store stayed locked for %.0f s during %s; "
            "answering 'try again shortly' (logged at most once per %.0f s)",
            RESERVATION_BUSY_TIMEOUT_SECONDS,
            action,
            _BUSY_WARNING_INTERVAL_SECONDS,
        )

    # ------------------------------------------------------------------
    # Throttle
    # ------------------------------------------------------------------

    def is_throttled(
        self, subject: str, *, scope: str = SCOPE_LOGIN
    ) -> Tuple[bool, float]:
        """Read-only status: ``(throttled, retry_after_seconds)``."""
        if not self._enabled:
            return False, 0.0
        row = self._current_store().read(throttle_key(subject, scope))
        now = self._clock()
        if row is None or row.blocked_until <= now:
            return False, 0.0
        return True, row.blocked_until - now

    def begin_attempt(
        self, subject: str, *, scope: str = SCOPE_LOGIN
    ) -> AttemptOutcome:
        """Reserve one attempt BEFORE its password/code is checked.

        Refused (not counted) while the backoff window runs.  Otherwise the
        attempt is counted and admitted; the one that reaches the threshold
        starts the window (``throttle_started``), every later one doubles
        it.  The caller calls ``record_success`` when the check passes.

        Raises:
            ThrottleStoreBusy: solo-mode store write-locked past the bound;
                the door answers 503 "try again shortly".
        """
        if not self._enabled:
            return AttemptOutcome(True, 0.0, False)
        try:
            return self._reserve(throttle_key(subject, scope))
        except ThrottleStoreBusy:
            self._warn_store_busy("a login attempt")
            raise

    def _reserve(self, key: str) -> AttemptOutcome:
        now = self._clock()
        store = self._current_store()

        # Read first, without a transaction: a refusal takes no write lock
        # and writes nothing.  A stale "blocked" can only cost one spurious
        # refusal; a stale "not blocked" is re-checked under the row lock.
        current = store.read(key)
        if current is not None and current.blocked_until > now:
            return AttemptOutcome(False, current.blocked_until - now, False)

        def decide(row: Optional[_Row]) -> Optional[_Row]:
            if row is not None and row.blocked_until > now:
                return None
            recent = (
                row is not None and row.last_failure_at >= now - self._window_seconds
            )
            count = (row.failure_count if recent and row else 0) + 1
            blocked_until = (
                now + self._delay_for(count) if count >= self._max_attempts else 0.0
            )
            return _Row(count, now, blocked_until)

        old, new = store.record(key, decide, now - self._window_seconds)
        if new is None:
            assert old is not None
            return AttemptOutcome(False, old.blocked_until - now, False)
        retry_after = max(new.blocked_until - now, 0.0)
        return AttemptOutcome(
            True, retry_after, new.failure_count == self._max_attempts
        )

    def record_success(self, subject: str, *, scope: str = SCOPE_LOGIN) -> None:
        """A passed check clears the key's attempt history.

        If the solo-mode store stays write-locked past the bound, the row is
        kept and a WARNING logged (rate-limited): the password was already
        verified, and the row is forgotten after ``window_minutes``.
        """
        if not self._enabled:
            return
        try:
            self._current_store().delete(throttle_key(subject, scope))
        except ThrottleStoreBusy:
            self._warn_store_busy("clearing a verified login's attempt count")

    def _delay_for(self, attempt_count: int) -> float:
        exponent = min(attempt_count - self._max_attempts, _MAX_BACKOFF_EXPONENT)
        return float(min(self._base_delay * (2**exponent), self._max_delay))


# Module-level singleton used by the login doors and the TOTP step-up.
login_rate_limiter = LoginRateLimiter()
