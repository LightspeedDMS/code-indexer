"""Bug #1928 (round 3, P1 -- Codex REJECT): store_batch_strict() must
PROPAGATE write failures, unlike store_batch() which catches and
warning-logs. Covers both PayloadCacheSqliteBackend (already propagates
via execute_atomic, so store_batch_strict is proven to delegate to it
correctly, AND that a mid-batch failure rolls back the WHOLE batch) and
PayloadCachePostgresBackend (whose store_batch() swallows failures --
store_batch_strict must NOT).

Mocking strategy: PayloadCacheSqliteBackend is tested against a REAL
on-disk SQLite database (tmp_path) -- including a genuine mid-batch
constraint violation to prove atomicity, not just a happy-path store.
PayloadCachePostgresBackend's failure path is tested via a fake
ConnectionPool whose connection() raises -- this simulates a real
infrastructure failure (a connection/transaction error) at the pool
boundary, not a mock of the backend's own logic under test.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, NoReturn, Tuple

import pytest

_ENTRY_TTL_SECONDS = 900


class _RaisingConnectionPool:
    """A fake ConnectionPool whose connection() always raises -- simulates
    a real PostgreSQL write failure (e.g. connection refused, disk full)
    without needing a live, broken database. The exception fires at the
    call site itself (before any `with` block runs), so no context-manager
    machinery is needed here."""

    def connection(self) -> NoReturn:
        raise RuntimeError("simulated PostgreSQL connection failure")


class TestPayloadCacheSqliteBackendStoreBatchStrict:
    """PayloadCacheSqliteBackend.store_batch_strict() -- real on-disk SQLite."""

    def _make_backend(self, tmp_path: Path):
        from code_indexer.server.storage.sqlite_backends import (
            PayloadCacheSqliteBackend,
        )

        return PayloadCacheSqliteBackend(str(tmp_path / "payload_cache.db"))

    def test_stores_all_entries_atomically(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        entries: List[Tuple[str, str, str, int]] = [
            (f"handle-{i}", f"content-{i}", f"preview-{i}", _ENTRY_TTL_SECONDS)
            for i in range(5)
        ]

        backend.store_batch_strict(entries)

        for handle, content, _preview, _ttl in entries:
            row = backend.retrieve(handle)
            assert row is not None
            assert row["content"] == content

    def test_mid_batch_failure_rolls_back_the_whole_batch(self, tmp_path: Path) -> None:
        """A real NOT-NULL constraint violation on the 3rd of 4 entries
        must roll back ALL of them -- none may persist. This is the
        genuine atomicity proof the previous round's review demanded."""
        backend = self._make_backend(tmp_path)
        entries: List[Tuple[str, str, str, int]] = [
            ("handle-0", "content-0", "preview-0", _ENTRY_TTL_SECONDS),
            ("handle-1", "content-1", "preview-1", _ENTRY_TTL_SECONDS),
            (  # type: ignore[list-item]  # None violates content NOT NULL
                "handle-2",
                None,
                "preview-2",
                _ENTRY_TTL_SECONDS,
            ),
            ("handle-3", "content-3", "preview-3", _ENTRY_TTL_SECONDS),
        ]

        with pytest.raises(sqlite3.IntegrityError):
            backend.store_batch_strict(entries)

        for handle, *_ in entries:
            assert backend.retrieve(handle) is None, (
                f"{handle} must not persist -- mid-batch failure must roll "
                "back the ENTIRE batch, not just the failing row"
            )

    def test_all_rows_share_the_same_created_at_timestamp(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        entries: List[Tuple[str, str, str, int]] = [
            (f"handle-{i}", f"content-{i}", f"preview-{i}", _ENTRY_TTL_SECONDS)
            for i in range(4)
        ]

        backend.store_batch_strict(entries)

        conn = sqlite3.connect(str(tmp_path / "payload_cache.db"))
        try:
            rows = conn.execute(
                "SELECT created_at FROM payload_cache WHERE cache_handle IN "
                "('handle-0','handle-1','handle-2','handle-3')"
            ).fetchall()
        finally:
            conn.close()
        distinct_timestamps = {r[0] for r in rows}
        assert len(distinct_timestamps) == 1, (
            f"expected one shared created_at for the whole batch, got "
            f"{distinct_timestamps}"
        )


class TestPayloadCachePostgresBackendStoreBatchStrictPropagates:
    """PayloadCachePostgresBackend.store_batch_strict() -- failure
    injection via a raising fake pool proves the failure is NOT
    swallowed (unlike store_batch(), which only warning-logs it)."""

    def test_connection_failure_raises_not_swallowed(self) -> None:
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        backend = PayloadCachePostgresBackend.__new__(PayloadCachePostgresBackend)
        # bypass __init__'s schema setup; fake pool deliberately implements
        # only .connection() -- the whole point of this failure injection.
        backend._pool = _RaisingConnectionPool()  # type: ignore[assignment]

        entries: List[Tuple[str, str, str, int]] = [
            ("handle-a", "content-a", "preview-a", _ENTRY_TTL_SECONDS),
        ]

        with pytest.raises(RuntimeError, match="simulated PostgreSQL"):
            backend.store_batch_strict(entries)

    def test_store_batch_non_strict_swallows_the_same_failure(self) -> None:
        """Contrast case proving the DISTINCTION this bug is about: the
        pre-existing store_batch() swallows the identical failure and
        returns normally -- store_batch_strict() must not."""
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        backend = PayloadCachePostgresBackend.__new__(PayloadCachePostgresBackend)
        backend._pool = _RaisingConnectionPool()  # type: ignore[assignment]

        entries: List[Tuple[str, str, str, int]] = [
            ("handle-a", "content-a", "preview-a", _ENTRY_TTL_SECONDS),
        ]

        backend.store_batch(entries)  # must NOT raise -- documents current behavior
