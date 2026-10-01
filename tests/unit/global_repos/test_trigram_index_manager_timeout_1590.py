"""Bug #1590: TrigramIndexManager.exists()/.query() have no timeout and

collapse "index absent" and "index present but corrupt" into the same
silent fallback.

Root cause (see GitHub issue #1590): xray_search/xray_explore Phase 1's
trigram pre-filter (regex_search.py's _prefilter_candidate_files) calls
TrigramIndexManager.exists()/.query(), which open raw sqlite3 connections
with NO timeout, no subprocess isolation, no cancellation mechanism. An
unexpectedly slow/blocking sqlite operation there can hang a job forever
while it holds one of only 4 global xray concurrency slots.

This file proves, with REAL sqlite3 connections (never mocked) and a REAL
thread-based watchdog (mirroring the established idiom already used by
scip/database/queries.py's trace_call_chain_v2 fix for Bug #1603):

- AC1/AC5: exists()/query() accept an optional timeout_seconds and bound
  wall-clock time even when the underlying sqlite3.connect() call itself
  blocks (simulated by monkeypatching sqlite3.connect to sleep -- the
  connection-open step, not a mocked "instant timeout").
- AC7: a genuinely corrupt index file (garbage bytes, not a valid sqlite
  database) is logged at ERROR severity with a message distinguishing it
  from a missing-table/stale-schema index (which stays at the existing,
  lower-severity "will rebuild" path) -- both currently collapse into the
  same silent "return False/None" with no differentiated observability.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from code_indexer.global_repos.regex_trigram import trigrams
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auth.java").write_text("public class LSAuthenticator {}")
    return repo


def _built_mgr(tmp_path: Path) -> TrigramIndexManager:
    repo = _repo(tmp_path)
    mgr = TrigramIndexManager(tmp_path / "idx")
    mgr.build(repo, file_list=["auth.java"])
    return mgr


# ---------------------------------------------------------------------------
# AC1/AC5: real thread-watchdog timeout bounding, real sqlite3 connections.
# ---------------------------------------------------------------------------


class TestTimeoutBoundsRealBlockingCall:
    def test_exists_raises_timeout_error_when_connect_blocks(
        self, tmp_path, monkeypatch
    ):
        """A slow sqlite3.connect() (simulating an unresponsive mount /
        contended I/O) must not make exists() block past timeout_seconds.

        Pre-fix: exists() has no timeout_seconds parameter at all, so this
        call would take the full BLOCK_SECONDS regardless of any deadline
        the caller cares about -- proving the structural gap Bug #1590
        describes. Post-fix: exists(timeout_seconds=...) must return/raise
        within timeout_seconds + a small tolerance, genuinely abandoning
        the blocked connect() call (Python cannot forcibly kill a thread;
        the pragmatic tradeoff documented in the issue).
        """
        mgr = _built_mgr(tmp_path)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_connect = tim_mod.sqlite3.connect
        BLOCK_SECONDS = 3.0

        def _slow_connect(*args, **kwargs):
            time.sleep(BLOCK_SECONDS)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _slow_connect)

        from code_indexer.global_repos.trigram_index_manager import (
            TrigramIndexTimeoutError,
        )

        start = time.monotonic()
        with pytest.raises(TrigramIndexTimeoutError):
            mgr.exists(timeout_seconds=1)
        elapsed = time.monotonic() - start

        assert elapsed < BLOCK_SECONDS, (
            f"exists() took {elapsed:.2f}s -- expected to be bounded well "
            f"under the {BLOCK_SECONDS}s blocking connect(), proving the "
            f"timeout genuinely fired rather than waiting for the slow "
            f"call to finish"
        )
        assert elapsed <= 1 + 1.5, f"exists() took {elapsed:.2f}s, expected <= 2.5s"

    def test_query_raises_timeout_error_when_connect_blocks(
        self, tmp_path, monkeypatch
    ):
        mgr = _built_mgr(tmp_path)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_connect = tim_mod.sqlite3.connect
        BLOCK_SECONDS = 3.0

        def _slow_connect(*args, **kwargs):
            time.sleep(BLOCK_SECONDS)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _slow_connect)

        from code_indexer.global_repos.trigram_index_manager import (
            TrigramIndexTimeoutError,
        )

        start = time.monotonic()
        with pytest.raises(TrigramIndexTimeoutError):
            mgr.query(trigrams("LSAuthenticator"), timeout_seconds=1)
        elapsed = time.monotonic() - start

        assert elapsed < BLOCK_SECONDS
        assert elapsed <= 1 + 1.5, f"query() took {elapsed:.2f}s, expected <= 2.5s"

    def test_exists_without_timeout_param_still_completes_normally(
        self, tmp_path, monkeypatch
    ):
        """Backward compatibility: omitting timeout_seconds (default None)
        must preserve the original unbounded behavior for existing callers
        that never pass it."""
        mgr = _built_mgr(tmp_path)
        assert mgr.exists() is True

    def test_exists_with_generous_timeout_returns_true_normally(self, tmp_path):
        """A generous timeout on a healthy index must not spuriously fire."""
        mgr = _built_mgr(tmp_path)
        assert mgr.exists(timeout_seconds=30) is True

    def test_query_with_generous_timeout_returns_real_candidates(self, tmp_path):
        mgr = _built_mgr(tmp_path)
        result = mgr.query(trigrams("LSAuthenticator"), timeout_seconds=30)
        assert result is not None
        assert "auth.java" in result


# ---------------------------------------------------------------------------
# AC7: corrupt index vs. missing-table/stale-schema index must be
# distinguishable via logging, not collapsed into the same silent fallback.
# ---------------------------------------------------------------------------


class TestCorruptionDistinguishedFromStaleSchema:
    def test_corrupt_db_file_logs_error_from_exists(self, tmp_path, caplog):
        """A genuinely corrupt (not-a-database) file must log at ERROR
        severity with a message identifying it as corruption -- not the
        same silent path as a routine missing-table/stale-schema index."""
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        db_path = idx_dir / "trigrams.db"
        db_path.write_bytes(b"not a real sqlite database, just garbage bytes 123456")

        mgr = TrigramIndexManager(idx_dir)

        with caplog.at_level(
            logging.ERROR, logger="code_indexer.global_repos.trigram_index_manager"
        ):
            result = mgr.exists()

        assert result is False
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR-level log record for a corrupt index"
        assert any("corrupt" in r.message.lower() for r in error_records), (
            f"expected 'corrupt' in an ERROR log message, got: "
            f"{[r.message for r in error_records]}"
        )

    def test_corrupt_db_file_logs_error_from_query(self, tmp_path, caplog):
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        db_path = idx_dir / "trigrams.db"
        db_path.write_bytes(b"not a real sqlite database, just garbage bytes 123456")

        mgr = TrigramIndexManager(idx_dir)

        with caplog.at_level(
            logging.ERROR, logger="code_indexer.global_repos.trigram_index_manager"
        ):
            result = mgr.query(trigrams("LSAuthenticator"))

        assert result is None
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR-level log record for a corrupt index"
        assert any("corrupt" in r.message.lower() for r in error_records)

    def test_missing_table_does_not_log_error_from_exists(self, tmp_path, caplog):
        """A stale-schema index (missing 'meta' table -- the documented,
        routine "will rebuild" case) must NOT be logged at ERROR severity --
        that would cry wolf on every normal schema migration."""
        mgr = _built_mgr(tmp_path)
        with sqlite3.connect(mgr.db_path) as conn:
            conn.execute("DROP TABLE meta")

        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.global_repos.trigram_index_manager"
        ):
            result = mgr.exists()

        assert result is False
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, (
            f"missing-table/stale-schema must NOT log at ERROR severity, got: "
            f"{[r.message for r in error_records]}"
        )


# ---------------------------------------------------------------------------
# Code-review finding F5: sqlite3.InterfaceError is a SIBLING branch of the
# exception hierarchy (Error -> InterfaceError), NOT a sqlite3.DatabaseError
# subclass -- so it was silently uncaught by exists()/query() after the old
# broad `except sqlite3.Error` was removed for the AC7 corruption-vs-absent
# split. Both methods must still degrade gracefully rather than propagate.
# ---------------------------------------------------------------------------


class TestInterfaceErrorGap:
    def test_exists_catches_interface_error(self, tmp_path, monkeypatch):
        import code_indexer.global_repos.trigram_index_manager as tim_mod

        mgr = _built_mgr(tmp_path)

        def _raise_interface_error(*args, **kwargs):
            raise sqlite3.InterfaceError("simulated interface error")

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _raise_interface_error)

        result = mgr.exists()
        assert result is False

    def test_query_catches_interface_error(self, tmp_path, monkeypatch):
        import code_indexer.global_repos.trigram_index_manager as tim_mod

        mgr = _built_mgr(tmp_path)

        def _raise_interface_error(*args, **kwargs):
            raise sqlite3.InterfaceError("simulated interface error")

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _raise_interface_error)

        result = mgr.query(trigrams("LSAuthenticator"))
        assert result is None


# ---------------------------------------------------------------------------
# Bug #1590 review round 3 finding B2: the ``self._db_path.exists()`` stat
# check in ``exists()`` runs BEFORE the watchdog thread is armed. On the
# `hard` NFSv3 golden-repo mount this raw stat can block in uninterruptible
# kernel retry forever -- reproduced here by monkeypatching pathlib.Path's
# stat-based .exists() to block, proving it currently ignores
# timeout_seconds entirely (it fires before the watchdog-protected _work()
# even starts).
# ---------------------------------------------------------------------------


class _SlowExistsPathProxy:
    """Thin proxy delegating everything to a real ``Path`` except
    ``.exists()``, which sleeps first. Scoped to ONE manager instance's
    ``_db_path`` attribute (rather than monkeypatching ``pathlib.Path``
    globally) so the delay cannot bleed into pytest's own internal
    filesystem calls made on other threads/paths during the test."""

    def __init__(self, real_path, block_seconds):
        self._real_path = real_path
        self._block_seconds = block_seconds

    def exists(self, *args, **kwargs):
        time.sleep(self._block_seconds)
        return self._real_path.exists(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real_path, name)


class TestExistsStatCheckIsWatchdogProtected:
    def test_exists_stat_check_before_watchdog_is_bounded_by_timeout(
        self, tmp_path, monkeypatch
    ):
        mgr = _built_mgr(tmp_path)

        BLOCK_SECONDS = 3.0
        monkeypatch.setattr(
            mgr, "_db_path", _SlowExistsPathProxy(mgr._db_path, BLOCK_SECONDS)
        )

        from code_indexer.global_repos.trigram_index_manager import (
            TrigramIndexTimeoutError,
        )

        start = time.monotonic()
        with pytest.raises(TrigramIndexTimeoutError):
            mgr.exists(timeout_seconds=1)
        elapsed = time.monotonic() - start

        assert elapsed < BLOCK_SECONDS, (
            f"exists() took {elapsed:.2f}s -- expected to be bounded well "
            f"under the {BLOCK_SECONDS}s blocking .exists() stat call, "
            f"proving the timeout genuinely covers the existence check "
            f"rather than running it before the watchdog is armed"
        )
        assert elapsed <= 1 + 1.5, f"exists() took {elapsed:.2f}s, expected <= 2.5s"
