"""
Regression test for Bug #1532 follow-up: direct-read call sites racing close_all().

The original Bug #1532 fix made execute_atomic() hold DatabaseConnectionManager's
lock for its whole BEGIN..COMMIT/ROLLBACK duration, closing the race against
close_all() for that one call pattern. Code review (Codex) found the SAME race
class still open at ~10+ other production call sites that call get_connection()
directly and issue SQL without ever going through execute_atomic() -- e.g.
oauth_manager.get_client(), payload_cache's read paths, job_tracker reads,
wiki_cache reads, etc. Any of those can still have close_all() close their
connection mid-read on another thread.

The fix is a single shared context manager, guarded_connection(), that yields
the calling thread's connection while holding the SAME lock close_all() uses,
so any call site that wraps its read in `with self._conn_manager.guarded_connection()
as conn:` gets the exact same protection execute_atomic() has -- structurally,
not by remembering to wrap every call site by hand.

This test proves the mechanism directly: a slow direct-SQL read performed
through guarded_connection() on a background thread must not race a
concurrent close_all() call on the main thread.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import List

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

# Time the slow direct-read sleeps while holding the connection open, widening
# the race window deterministically (mirrors the execute_atomic race test).
_READ_HOLD_SECONDS = 0.3

# Delay after `started` fires before the main thread calls close_all(), so the
# reader thread is genuinely inside its hold-sleep before the race triggers.
_PRE_CLOSE_SETTLE_SECONDS = 0.05

_STARTED_WAIT_TIMEOUT_SECONDS = 2.0
_READER_JOIN_TIMEOUT_SECONDS = 5.0


def test_guarded_connection_does_not_race_with_close_all(tmp_path: Path) -> None:
    """A direct-read call site using guarded_connection() must not observe a
    connection closed out from under it by a concurrent close_all()."""
    db_path = tmp_path / "guarded_race_1532.db"
    mgr = DatabaseConnectionManager.get_instance(str(db_path))

    mgr.execute_atomic(
        lambda conn: conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
    )

    started = threading.Event()
    errors: List[Exception] = []

    def slow_direct_read() -> None:
        try:
            with mgr.guarded_connection() as conn:
                started.set()
                # Widen the race window: close_all() (called from the main
                # thread right after `started` is set) must block until this
                # read finishes, never close the connection while this sleep
                # is running.
                time.sleep(_READ_HOLD_SECONDS)
                cursor: sqlite3.Cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM t")
                cursor.fetchone()
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    reader_thread = threading.Thread(target=slow_direct_read)
    reader_thread.start()
    try:
        try:
            assert started.wait(timeout=_STARTED_WAIT_TIMEOUT_SECONDS), (
                "reader thread never entered guarded_connection"
            )
            time.sleep(_PRE_CLOSE_SETTLE_SECONDS)
        finally:
            # Must block until slow_direct_read() finishes rather than
            # closing the connection out from under it.
            mgr.close_all()
    finally:
        reader_thread.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)

    assert not reader_thread.is_alive(), "reader thread did not finish"

    assert not errors, (
        "guarded_connection() read raised while racing close_all() -- "
        f"connection was closed out from under an in-flight read: {errors}"
    )


def _bare_read_after_signal(
    mgr: DatabaseConnectionManager,
    started: threading.Event,
    close_all_done: threading.Event,
    errors: List[Exception],
) -> None:
    """Reader body for the OLD, unprotected get_connection() pattern.

    Acquires the connection with no lock held, signals `started`, waits for
    the main thread's close_all() to actually run, then reads -- recording
    whatever exception (if any) the now-possibly-closed connection raises.
    """
    try:
        conn = mgr.get_connection()
        started.set()
        assert close_all_done.wait(timeout=_READER_JOIN_TIMEOUT_SECONDS)
        cursor: sqlite3.Cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM t")
        cursor.fetchone()
    except Exception as exc:
        errors.append(exc)


def test_bare_get_connection_races_with_close_all(tmp_path: Path) -> None:
    """PASSES by pinning the OLD race: bare get_connection() (no
    guarded_connection()) deterministically lets close_all() close the
    connection out from under a concurrent reader, raising
    sqlite3.ProgrammingError. This is a normal green regression test -- the
    assertions below are on the CAUGHT exception's type/message, proving the
    race is real and reproducible, not merely an AttributeError like the
    pre-fix failure of test_guarded_connection_does_not_race_with_close_all
    above. It complements that test: this one pins the OLD (buggy) call
    pattern's behavior, that one proves the NEW (guarded_connection())
    pattern avoids it.
    """
    db_path = tmp_path / "bare_race_1532.db"
    mgr = DatabaseConnectionManager.get_instance(str(db_path))
    mgr.execute_atomic(
        lambda conn: conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
    )

    started = threading.Event()
    close_all_done = threading.Event()
    errors: List[Exception] = []

    reader_thread = threading.Thread(
        target=_bare_read_after_signal, args=(mgr, started, close_all_done, errors)
    )
    reader_thread.start()
    try:
        try:
            assert started.wait(timeout=_STARTED_WAIT_TIMEOUT_SECONDS)
        finally:
            # Bare get_connection() holds no lock, so this succeeds
            # immediately and closes the connection the reader thread is
            # still holding. Attempted even if the wait above failed, so
            # the reader is never left holding a connection nobody closes.
            mgr.close_all()
    finally:
        close_all_done.set()
        reader_thread.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)

    assert not reader_thread.is_alive()
    assert len(errors) == 1, f"expected exactly one race error, got: {errors}"
    assert isinstance(errors[0], sqlite3.ProgrammingError), type(errors[0])
    assert "closed database" in str(errors[0]).lower(), errors[0]
