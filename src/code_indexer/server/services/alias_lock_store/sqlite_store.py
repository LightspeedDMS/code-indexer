"""SQLite-backed AliasLockStore (Issue #1546 Phase 1).

Fix #2 (Issue #1546 review): a SINGLE shared ``alias_locks.db`` file was
the original Phase 1 design, but SQLite has NO row-level write locking --
holding one alias's lock transaction takes the WHOLE FILE's writer lock,
so acquiring a DIFFERENT, completely unrelated lock_key in the SAME file
would ALSO fail while any lock is held. That is a false-negative
correctness bug, not merely a serialization inconvenience: "lock B is
held" would be reported when B was never touched at all.

The fix is one DEDICATED SQLite file PER ALIAS, named
``{sanitized(lock_key)}.db`` under a caller-supplied lock directory (see
``SqliteAliasLockStore.__init__``). This does NOT reinstate the old
JSON-file lock's TOCTOU race class: SQLite itself owns the OS-level file
lock via ``BEGIN IMMEDIATE`` on that specific file -- there is no
read-then-act on file content, no inode-identity spoof, no torn write.
Three properties this design depends on:

  (a) The lock directory MUST be node-local storage (this project's
      convention: under ``CIDX_DATA_DIR``, default ``~/.cidx-server``,
      e.g. ``{CIDX_DATA_DIR}/data/alias_locks/``) -- NEVER the shared NFS
      ``golden_repos_dir``, whose mount is ``nolock`` (file locking does
      not actually work there). Wiring the production call site to the
      correct node-local path is Phase 2 work; this module only accepts
      whatever directory the caller passes.
  (b) Per-alias lock files are NEVER deleted/unlinked while anything
      might still reference the path -- no reaper, no cleanup sweep.
      Small, bounded, permanent files are the correct trade-off.
  (c) Filenames are a REVERSIBLE encoding of the lock_key
      (``urllib.parse.quote(lock_key, safe="")`` -- this project's
      established path-encoding convention, see e.g.
      ``git_state_manager.py``, ``forge_client.py``), not an opaque
      hash, so an operator can identify which alias a lock file
      corresponds to just by looking at the directory. Letters, digits,
      and ``_.-~`` are never quoted, so every alias validated by
      ``GoldenRepoAddRequest.validate_alias`` (alphanumeric/hyphen
      /underscore) round-trips to an UNCHANGED filename stem.

Known SQLite characteristic (documented, not a defect, and now confined
to WITHIN one alias's file rather than across all aliases): while ONE
lock transaction is held open on its own dedicated file, a SECOND
`BEGIN IMMEDIATE` attempt for the SAME lock_key in the SAME file
contends for that file's single writer lock and can be delayed (bounded
by `busy_timeout_seconds`) before it can even attempt its own INSERT.
This is acceptable for solo/single-node deployments (which use SQLite
specifically because they are single-node) and is the reason the
architecture calls for PostgreSQL -- true per-row locking -- in cluster
mode. `busy_timeout_seconds` is deliberately modest (not the old
file-lock's implicit unboundedness) so contention resolves to a definite
"not acquired" (`None`) rather than hanging indefinitely.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import quote

_MILLISECONDS_PER_SECOND = 1000
_DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

# is_held()'s probe uses its OWN small, fixed timeout (Issue #1546 Phase
# 2), deliberately decoupled from the store's configured
# busy_timeout_seconds (which governs a genuine acquire attempt's
# contention wait, potentially deliberately long). is_held() only needs
# to know "is someone else's BEGIN IMMEDIATE currently open" -- a near
# -zero bound is enough to distinguish "contended" from "not contended"
# without making callers wait meaningfully.
_IS_HELD_PROBE_TIMEOUT_SECONDS = 0.05

# Substrings SQLite uses for the specific "another connection holds the
# writer lock" condition. Any OTHER OperationalError (malformed database,
# read-only filesystem, etc.) must propagate rather than be silently
# treated as ordinary lock contention.
_LOCK_CONTENTION_MESSAGE_SUBSTRINGS = (
    "database is locked",
    "database table is locked",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS golden_repo_alias_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL,
    operation        TEXT NOT NULL,
    acquired_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_renewed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
# owner_token is deliberately NOT UNIQUE (round-3 review, Issue #1546,
# matching migration 043's schema for cross-backend consistency): a
# UNIQUE constraint here was already a no-op for this per-alias
# -dedicated-file design (each file holds at most one row, keyed by
# lock_key), but PostgreSQL's SHARED-table design made the same
# constraint a real cross-key false-negative hazard -- keeping both
# schemas aligned avoids a future schema-drift trap.


def _rows_changed(conn: sqlite3.Connection) -> int:
    """Return SQLite's `changes()` -- rows affected by the most recent
    statement on this connection. Used instead of `cursor.rowcount` so the
    acquire/release/renew paths match the issue's specified primitive
    (`SELECT changes()`) literally, not merely an equivalent DB-API count.
    """
    row = conn.execute("SELECT changes()").fetchone()
    return int(row[0])


def _validate_busy_timeout_seconds(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"busy_timeout_seconds must be a number, got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf check
        raise ValueError(f"busy_timeout_seconds must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"busy_timeout_seconds must be >= 0, got {value!r}")
    return float(value)


def _is_lock_contention_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        substring in message for substring in _LOCK_CONTENTION_MESSAGE_SUBSTRINGS
    )


def _sanitize_lock_key_for_filename(lock_key: str) -> str:
    """Fix #2c: reversible, sanitized-alias-derived filename stem -- this
    project's established `urllib.parse.quote(value, safe="")` path
    -encoding convention (see `git_state_manager.py`,
    `forge_client.py`'s project_path encoding). Every alias accepted by
    `GoldenRepoAddRequest.validate_alias` (alphanumeric, hyphens,
    underscores only) round-trips UNCHANGED, since letters, digits, and
    `_.-~` are never quoted -- an operator can identify the alias just by
    reading the directory listing. Any other character (a defensive
    case for opaque non-golden-repo lock_keys) is percent-encoded,
    reversibly, rather than hashed away.
    """
    return quote(lock_key, safe="")


def _db_path_for_lock_key(lock_dir: Path, lock_key: str) -> Path:
    return lock_dir / f"{_sanitize_lock_key_for_filename(lock_key)}.db"


def _open_and_begin_immediate(
    db_path: Path, busy_timeout_seconds: float
) -> Optional[sqlite3.Connection]:
    """Open a dedicated connection to this alias's OWN file, ensure its
    schema exists (idempotent, lazy -- Fix #2's per-alias files are
    created on first use, never pre-created for every possible alias),
    and start a BEGIN IMMEDIATE transaction.

    Returns the connection (transaction open, not yet committed) on
    success, or None if the writer lock specifically could not be
    obtained within busy_timeout_seconds -- a definite "someone else is
    using this exact alias's file right now" signal, never an indefinite
    wait. Any OTHER OperationalError (malformed database, unreadable
    file, etc.), or any other exception at all during this setup
    sequence, is a genuine operational failure: the connection is always
    closed before propagating, and only the specific busy/locked
    OperationalError is ever translated into a "not acquired" `None`.

    Round-5 review fix (Opus, NEW CRITICAL): `check_same_thread=False`
    is REQUIRED here. The realistic Phase 2 call-site shape is
    acquire-on-one-thread, release/renew-on-a-different-thread
    (background workers, schedulers, job queues all use threads) --
    without this flag, Python's sqlite3 module raises
    `ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread` on the very first cross-thread
    `release()`/`renew()` call. That error was previously misclassified
    as ownership-lost AND raised BEFORE the connection could be closed,
    permanently wedging the alias (unrecoverable short of a server
    restart, since this design deliberately has no reaper/TTL).
    Reproduced empirically. Safe to disable here because
    `AliasLockHandle._release_lock` (an RLock) already serializes all
    release()/renew() access to a handle's connection across threads --
    exactly the "additional locking applied by the application" the
    sqlite3 docs require when disabling this check.
    """
    # isolation_level=None -> autocommit off, so we control BEGIN/COMMIT
    # explicitly (needed for BEGIN IMMEDIATE, which Python's implicit
    # "deferred" transaction start does not give us).
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_seconds,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        busy_timeout_ms = int(busy_timeout_seconds * _MILLISECONDS_PER_SECOND)
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        conn.close()
        if _is_lock_contention_error(exc):
            return None
        raise
    except Exception:
        conn.close()
        raise
    return conn


def _validate_non_empty_str(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _probe_lock_contention(db_path: Path) -> bool:
    """Non-destructive, bounded probe (Issue #1546 Phase 2): True iff
    `db_path`'s writer lock is currently held by ANOTHER connection.

    Reuses `_open_and_begin_immediate()` -- the exact same mechanism a
    real `try_acquire()` uses -- with the small, fixed
    `_IS_HELD_PROBE_TIMEOUT_SECONDS` bound, but ALWAYS rolls back and
    closes rather than ever returning the connection as a handle: this
    probe must never actually hold the lock, even momentarily, past the
    instant it needs to test contention. If the probe succeeds (no
    contention), rolling back is a true no-op -- no row was ever
    inserted, so nothing changes.
    """
    conn = _open_and_begin_immediate(db_path, _IS_HELD_PROBE_TIMEOUT_SECONDS)
    if conn is None:
        # Contended: someone else's BEGIN IMMEDIATE is currently open.
        return True
    try:
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    return False


_CLOSED_CONNECTION_MESSAGE_SUBSTRING = "cannot operate on a closed database"


def _require_live_connection(handle) -> sqlite3.Connection:
    """Raise a loud AliasLockOwnershipLostError if the handle's connection
    was EXPLICITLY closed (real crash, or a test simulating one) --
    otherwise let any other sqlite3.Error propagate as itself.

    Round-5 review fix (Opus): narrowed from a blanket `except
    sqlite3.Error` to a message-text check for the SPECIFIC "closed
    database" case. The broad version previously misclassified a
    cross-thread-misuse ProgrammingError as ownership-lost (now fixed
    at the source via `check_same_thread=False`, but this narrowing is
    the defense-in-depth Opus asked for regardless): a genuinely
    non-fatal or unrelated sqlite3.Error must never be silently folded
    into the ownership-loss signal.
    """
    from .base import AliasLockOwnershipLostError

    if handle is None:
        raise ValueError("handle must not be None")

    conn: sqlite3.Connection = handle._connection
    try:
        conn.execute("SELECT 1")
    except sqlite3.Error as exc:
        if _CLOSED_CONNECTION_MESSAGE_SUBSTRING in str(exc).lower():
            raise AliasLockOwnershipLostError(
                f"lock_key={handle.lock_key!r} owner_token={handle.owner_token!r}: "
                f"underlying connection is closed -- ownership already lost"
            ) from exc
        raise
    return conn


class SqliteAliasLockStore:
    """Session-held-transaction alias lock store backed by a dedicated
    SQLite file PER ALIAS (Fix #2 -- see module docstring)."""

    def __init__(
        self,
        lock_dir,
        *,
        busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            lock_dir: Directory containing one dedicated SQLite file per
                alias (created, along with parent directories, if it
                does not exist). MUST be node-local storage -- see the
                module docstring's caveat (a).
            busy_timeout_seconds: How long an acquire attempt will wait
                on file-lock contention (against the SAME alias's file)
                before giving up. Bounded and finite by design -- never
                indefinite.
        """
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_seconds = _validate_busy_timeout_seconds(
            busy_timeout_seconds
        )

    def try_acquire(self, lock_key: str, operation: str, owner_token=None):
        import uuid

        from .base import AliasLockHandle

        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        operation = _validate_non_empty_str(operation, "operation")
        owner_token = owner_token or str(uuid.uuid4())
        db_path = _db_path_for_lock_key(self._lock_dir, lock_key)

        conn = _open_and_begin_immediate(db_path, self._busy_timeout_seconds)
        if conn is None:
            return None

        try:
            conn.execute(
                "INSERT OR IGNORE INTO golden_repo_alias_locks "
                "(lock_key, owner_token, operation) VALUES (?, ?, ?)",
                (lock_key, owner_token, operation),
            )
            if _rows_changed(conn) == 1:
                # Acquired. Leave the transaction OPEN -- this uncommitted
                # transaction, on this connection, IS the lock. Do not
                # commit or close here.
                return AliasLockHandle(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    operation=operation,
                    _connection=conn,
                    _store=self,
                )

            # Someone else already holds lock_key (extremely unlikely
            # inside this alias's OWN dedicated file, since only ONE
            # writer can ever hold BEGIN IMMEDIATE at a time -- but a
            # crashed holder's row could theoretically still be visible
            # for one instant during recovery). Roll back and close --
            # this connection has no further purpose.
            conn.execute("ROLLBACK")
            conn.close()
            return None
        except Exception:
            conn.close()
            raise

    def release(self, handle) -> None:
        """Exact-token DELETE on the SAME connection/transaction that
        acquired the lock, then COMMIT -- this is the ONLY point in the
        lock's lifetime where the held transaction is ever committed,
        and doing so is exactly what releases the underlying SQLite
        writer lock (the mechanism that WAS this alias's lock).

        Fix #4: a genuine execution failure (e.g. a disk I/O error from
        a severed file descriptor) during the DELETE, the row-count
        check, or the COMMIT is normalized to AliasLockOwnershipLostError,
        chaining the original exception as cause, rather than leaking a
        raw sqlite3 exception. The deliberate AliasLockOwnershipLostError
        raised below for the zero-rows-affected case passes through
        this normalization unchanged -- it is not itself a connection
        failure.
        """
        from .base import AliasLockOwnershipLostError

        # Round-4 review fix (Finding #5): atomic check-and-set of
        # _released under the handle's own RLock -- closes the
        # Codex-reproduced race where two concurrent release() calls on
        # the SAME handle could both pass a bare "if not _released"
        # check before either set it. Holding the lock for the REST of
        # this method too serializes the real DB work per-handle, which
        # is deliberate and cheap (a single alias's DELETE/COMMIT is
        # fast) -- simpler and deadlock-free than trying to release the
        # lock mid-method, and reentrant-safe against __exit__ calling
        # into this same method while already holding the same lock.
        with handle._release_lock:
            if handle._released:
                raise AliasLockOwnershipLostError(
                    f"release() called again for lock_key={handle.lock_key!r} "
                    f"owner_token={handle.owner_token!r} -- already released "
                    f"(possibly by a concurrent caller) -- ownership already "
                    f"lost"
                )
            handle._released = True

            try:
                conn = _require_live_connection(handle)
            except Exception:
                try:
                    handle._connection.close()
                except Exception:
                    pass
                raise
            try:
                try:
                    conn.execute(
                        "DELETE FROM golden_repo_alias_locks "
                        "WHERE lock_key = ? AND owner_token = ?",
                        (handle.lock_key, handle.owner_token),
                    )
                    if _rows_changed(conn) == 0:
                        conn.execute("ROLLBACK")
                        raise AliasLockOwnershipLostError(
                            f"release() found zero rows for "
                            f"lock_key={handle.lock_key!r} "
                            f"owner_token={handle.owner_token!r} -- ownership "
                            f"already lost"
                        )
                    conn.execute("COMMIT")
                except AliasLockOwnershipLostError:
                    raise
                except sqlite3.Error as exc:
                    raise AliasLockOwnershipLostError(
                        f"lock_key={handle.lock_key!r} "
                        f"owner_token={handle.owner_token!r}: release() "
                        f"failed -- connection is no longer usable"
                    ) from exc
            finally:
                conn.close()

    def is_held(self, lock_key: str) -> bool:
        """Non-blocking contention probe (Issue #1546 Phase 2) -- see
        `AliasLockStore.is_held()`'s docstring for the full contract and
        the architecture reason this is a boolean, not a metadata read.
        Body delegates to the module-level `_probe_lock_contention()`
        helper; this method's job is only to validate `lock_key` and
        resolve its dedicated file path.
        """
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        db_path = _db_path_for_lock_key(self._lock_dir, lock_key)
        return _probe_lock_contention(db_path)

    def renew(self, handle) -> None:
        """Diagnostic-only heartbeat: exact-token UPDATE of
        last_renewed_at. Deliberately does NOT commit on success --
        committing would end the held transaction and release the
        underlying SQLite writer lock that IS this alias's lock --
        defeating the entire session-held-lock design (the lock's
        connection keeps exactly ONE transaction open for its whole
        lifetime and commits it exactly once, in release()).

        Fix #3: a WRONG-TOKEN renew() (the UPDATE executes fine but
        matches zero rows) raises WITHOUT rolling back or closing the
        connection -- the real held transaction (whatever it actually
        is) stays open and the lock stays held. renew() is diagnostic
        -only and must NEVER be capable of releasing the lock on a
        caller's mistake; only release() ends the lock's lifecycle.

        Fix F1 (round-3 review, CRITICAL correction of round-1's Fix #4):
        renew() NEVER calls conn.close()/rollback() on ANY path,
        including a genuine statement-execution failure. Round-1's
        broad except-sqlite3.Error-then-close()-then-raise was itself
        the bug it was meant to fix: closing a connection IS what
        releases the underlying SQLite writer lock, so a perfectly
        non-fatal error on a still-healthy connection (reproduced
        live: sqlite3.OperationalError: no such table, on a connection
        with no actual problem) silently released an actively-held
        lock while the real holder kept running -- exactly the
        "successor takes over while the original holder is still
        active" hazard this whole story exists to eliminate. If the
        connection is genuinely dead, the lock is already gone by
        construction (a crash rolls the transaction back
        automatically) -- closing it here would add nothing. If it is
        not genuinely dead, closing it is the defect. The statement's
        exception (if any) therefore propagates AS ITSELF, unwrapped,
        with the connection left completely untouched either way; only
        the explicit zero-rows-affected check below still raises
        AliasLockOwnershipLostError.
        """
        from .base import AliasLockOwnershipLostError

        # Round-5 review fix (Opus, extends Finding #5's RLock to
        # renew()): without this lock, a concurrent release() on a
        # different thread could close this exact connection between
        # _require_live_connection's check and the UPDATE below --
        # renew() must never touch the connection outside this
        # synchronized region.
        with handle._release_lock:
            if handle._released:
                raise AliasLockOwnershipLostError(
                    f"renew() called for lock_key={handle.lock_key!r} "
                    f"owner_token={handle.owner_token!r} but the lock was "
                    f"already released -- ownership already lost"
                )
            conn = _require_live_connection(handle)
            conn.execute(
                "UPDATE golden_repo_alias_locks "
                "SET last_renewed_at = CURRENT_TIMESTAMP "
                "WHERE lock_key = ? AND owner_token = ?",
                (handle.lock_key, handle.owner_token),
            )
            rows_changed = _rows_changed(conn)

        if rows_changed == 0:
            # Wrong token: the UPDATE executed cleanly but matched zero
            # rows. Do NOT rollback, do NOT close -- the real lock (this
            # connection's actual open transaction) must remain held.
            raise AliasLockOwnershipLostError(
                f"renew() found zero rows for lock_key={handle.lock_key!r} "
                f"owner_token={handle.owner_token!r} -- ownership already lost"
            )
        # Success: leave the connection/transaction open, untouched.
