"""Unit tests for PayloadCache.store_batch_with_keys() (Bug #1928 round 3,
P1 -- Codex REJECT on the first rework).

Codex's finding: xray_truncation.py stored N whole-entry pages via ONE
store_batch() call, then stored the pages-v1 manifest SEPARATELY via a
second store() call. This meant: (1) pages and the manifest could get
slightly different expiry/TTL windows (pages stored first, expire
slightly earlier); (2) a failed manifest write orphaned already-stored
pages; (3) since the PG backend's store_batch() SWALLOWS write failures
(catches + warning-logs), a failed page write could leave a manifest
pointing at nonexistent pages while the facade still returned a handle.

store_batch_with_keys() closes this: it takes pre-assigned (handle,
content) pairs -- so a caller can build a manifest that references page
handles BEFORE any write happens, then submit pages+manifest as ONE
atomic batch, sharing one timestamp, with failures propagating (not
swallowed) so the caller can surface an explicit error instead of a
handle to unwritten data.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig

_MAX_FETCH_SIZE_CHARS = 5000
_CACHE_TTL_SECONDS = 900
_CLEANUP_INTERVAL_SECONDS = 60


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "payload_cache.db"


@pytest.fixture
def sqlite_cache(temp_db_path: Path) -> Generator[PayloadCache, None, None]:
    """A plain (no _backend) facade -- the CLI/daemon path."""
    config = PayloadCacheConfig(
        preview_size_chars=_MAX_FETCH_SIZE_CHARS,
        max_fetch_size_chars=_MAX_FETCH_SIZE_CHARS,
        cache_ttl_seconds=_CACHE_TTL_SECONDS,
        cleanup_interval_seconds=_CLEANUP_INTERVAL_SECONDS,
    )
    c = PayloadCache(db_path=temp_db_path, config=config)
    c.initialize()
    yield c
    c.close()


class _FakeStrictBackend:
    """Records store_batch_strict() calls, satisfying the
    PayloadCacheBackend Protocol shape used via the facade's `_backend`
    delegation path. `raise_on_call`, when set, makes the call raise --
    proving the facade does not swallow a backend failure."""

    def __init__(self, raise_on_call: Optional[Exception] = None) -> None:
        self.calls: List[Tuple[Tuple[str, str, str, int], ...]] = []
        self._raise_on_call = raise_on_call

    def store_batch_strict(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.calls.append(tuple(entries))

    # Unused by this test but required by the real PayloadCacheBackend
    # Protocol shape -- kept minimal/inert with the Protocol's own
    # signatures (never Any) so an accidental real call fails loudly.
    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    def store_batch(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def cleanup_expired(self) -> int:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class TestStoreBatchWithKeysSqlitePath:
    """No _backend configured -- the facade owns its own SQLite writes
    directly (the CLI/daemon path)."""

    def test_stores_all_pairs_and_they_are_retrievable(
        self, sqlite_cache: PayloadCache
    ) -> None:
        items = [(f"handle-{i}", f"content-{i}") for i in range(5)]

        sqlite_cache.store_batch_with_keys(items)

        for handle, content in items:
            assert sqlite_cache.retrieve_full(handle) == content

    def test_all_rows_share_one_timestamp(self, sqlite_cache: PayloadCache) -> None:
        items = [(f"handle-{i}", f"content-{i}") for i in range(4)]

        sqlite_cache.store_batch_with_keys(items)

        conn = sqlite3.connect(str(sqlite_cache.db_path))
        try:
            rows = conn.execute(
                "SELECT created_at FROM payload_cache WHERE handle IN "
                "('handle-0','handle-1','handle-2','handle-3')"
            ).fetchall()
        finally:
            conn.close()
        assert len({r[0] for r in rows}) == 1

    def test_mid_batch_failure_rolls_back_the_whole_batch(
        self, sqlite_cache: PayloadCache
    ) -> None:
        """A real PRIMARY KEY violation partway through (a duplicate
        handle already committed by a prior call) must roll back the
        WHOLE new batch -- the atomicity guarantee this bug is about."""
        sqlite_cache.store_batch_with_keys([("handle-1", "pre-existing")])

        items = [
            ("handle-0", "content-0"),
            ("handle-1", "content-1"),  # duplicate PK -> real IntegrityError
            ("handle-2", "content-2"),
        ]

        with pytest.raises(sqlite3.IntegrityError):
            sqlite_cache.store_batch_with_keys(items)

        conn = sqlite3.connect(str(sqlite_cache.db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM payload_cache WHERE handle IN "
                "('handle-0','handle-2')"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0, "a mid-batch failure must leave NO partial rows"

    def test_empty_items_is_a_noop(self, sqlite_cache: PayloadCache) -> None:
        sqlite_cache.store_batch_with_keys([])  # must not raise

    def test_not_initialized_raises_runtime_error(self, temp_db_path: Path) -> None:
        config = PayloadCacheConfig(
            preview_size_chars=_MAX_FETCH_SIZE_CHARS,
            max_fetch_size_chars=_MAX_FETCH_SIZE_CHARS,
            cache_ttl_seconds=_CACHE_TTL_SECONDS,
            cleanup_interval_seconds=_CLEANUP_INTERVAL_SECONDS,
        )
        uninitialized = PayloadCache(db_path=temp_db_path, config=config)

        with pytest.raises(RuntimeError, match="not initialized"):
            uninitialized.store_batch_with_keys([("h", "c")])


class TestStoreBatchWithKeysBackendPath:
    """A _backend is configured (PG cluster or solo-via-BackendRegistry) --
    the facade must delegate to store_batch_strict(), never store_batch()
    (which would swallow a failure on the PG backend)."""

    def _make_cache_with_backend(self, backend: _FakeStrictBackend) -> PayloadCache:
        config = PayloadCacheConfig(
            preview_size_chars=_MAX_FETCH_SIZE_CHARS,
            max_fetch_size_chars=_MAX_FETCH_SIZE_CHARS,
            cache_ttl_seconds=_CACHE_TTL_SECONDS,
            cleanup_interval_seconds=_CLEANUP_INTERVAL_SECONDS,
        )
        cache = PayloadCache(
            db_path=Path("/unused"), config=config, storage_backend=backend
        )
        cache.initialize()
        return cache

    def test_delegates_to_store_batch_strict_not_store_batch(self) -> None:
        backend = _FakeStrictBackend()
        cache = self._make_cache_with_backend(backend)
        items = [("handle-a", "content-a"), ("handle-b", "content-b")]

        cache.store_batch_with_keys(items)

        assert len(backend.calls) == 1
        entries = backend.calls[0]
        assert [(h, c) for h, c, _preview, _ttl in entries] == items
        # Every entry in the SAME call shares the same ttl -- one batch.
        ttls = {ttl for _h, _c, _p, ttl in entries}
        assert ttls == {_CACHE_TTL_SECONDS}

    def test_backend_failure_propagates_not_swallowed(self) -> None:
        backend = _FakeStrictBackend(raise_on_call=RuntimeError("backend write failed"))
        cache = self._make_cache_with_backend(backend)

        with pytest.raises(RuntimeError, match="backend write failed"):
            cache.store_batch_with_keys([("handle-a", "content-a")])
