"""Discriminating regression tests for Bug #1850 reader leases."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest

from code_indexer.global_repos.snapshot_reader_lease import snapshot_has_live_reader
from code_indexer.server.cache import fts_index_cache, hnsw_index_cache, id_index_cache
from code_indexer.server.storage.shared.snapshot_paths import is_versioned_snapshot
from code_indexer.storage.shared import chunk_store_cache


class _RecordingLease:
    created: List["_RecordingLease"] = []

    def __init__(self, snapshot_path: str, ttl_seconds: float, *, lease_root: Path):
        self.snapshot_path = snapshot_path
        self.lease_root = lease_root
        self.ttl_seconds = ttl_seconds
        self.acquired = False
        self.__class__.created.append(self)

    def acquire(self) -> None:
        self.acquired = True

    def renew(self) -> None:
        pass

    def release(self) -> None:
        pass


class _FakeHnsw:
    def index_file_size(self) -> int:
        return 1


def _snapshot_index_dir(tmp_path: Path) -> Tuple[str, str]:
    root = tmp_path / ".versioned" / "repo" / "v_1234567890"
    return str(root), str(root / ".code-indexer" / "index" / "voyage-code-3:chunks_db")


def _cache_cases() -> List[Tuple[str, Any, Callable[..., Any]]]:
    return [
        (
            "hnsw",
            hnsw_index_cache.HNSWIndexCache,
            lambda cache, key: cache.get_or_load(key, lambda: (_FakeHnsw(), {})),
        ),
        (
            "fts",
            fts_index_cache.FTSIndexCache,
            lambda cache, key: cache.get_or_load(key, lambda: (object(), object())),
        ),
        (
            "id-index",
            id_index_cache.IdIndexCache,
            lambda cache, key: cache.get_or_load(key, lambda: {"id": "value"}),
        ),
    ]


@pytest.mark.parametrize("name,cache_type,load", _cache_cases())
def test_all_index_cache_readers_lease_the_snapshot_root(
    name: str,
    cache_type: Any,
    load: Callable[[Any, str], Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_root, index_dir = _snapshot_index_dir(tmp_path)
    _RecordingLease.created = []
    module = {
        "hnsw": hnsw_index_cache,
        "fts": fts_index_cache,
        "id-index": id_index_cache,
    }[name]
    monkeypatch.setattr(module, "SnapshotReaderLease", _RecordingLease)
    cache = cache_type(
        lease_root=tmp_path / "cidx-meta", is_versioned_snapshot=is_versioned_snapshot
    )

    load(cache, index_dir)

    assert len(_RecordingLease.created) == 1
    lease = _RecordingLease.created[0]
    assert lease.acquired
    assert lease.snapshot_path == snapshot_root


def test_chunk_store_reader_leases_snapshot_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_root, index_dir = _snapshot_index_dir(tmp_path)
    _RecordingLease.created = []
    monkeypatch.setattr(chunk_store_cache, "SnapshotReaderLease", _RecordingLease)
    cache = chunk_store_cache.ChunkStoreThreadCache(
        lease_root=tmp_path / "cidx-meta", is_versioned_snapshot=is_versioned_snapshot
    )
    lease = cache._acquire_reader_lease(index_dir)

    assert lease is not None
    assert _RecordingLease.created[0].snapshot_path == snapshot_root


def test_reader_lease_identity_matches_cleanup_root(tmp_path: Path) -> None:
    """A lease PUBLISHED BY A REAL READER for an index-dir cache key must be
    visible to snapshot_has_live_reader() at the snapshot ROOT -- the same
    root CleanupManager would check/delete (AC2, both directions).

    Deliberately routes through HNSWIndexCache.get_or_load() (the real
    production call path), not a bare SnapshotReaderLease(index_dir, ...)
    construction -- the latter can never demonstrate AC2 regardless of the
    production fix, since it bypasses resolve_versioned_snapshot_root
    entirely.
    """
    snapshot_root, index_dir = _snapshot_index_dir(tmp_path)
    lease_root = tmp_path / "cidx-meta"
    cache = hnsw_index_cache.HNSWIndexCache(
        lease_root=lease_root, is_versioned_snapshot=is_versioned_snapshot
    )
    try:
        cache.get_or_load(index_dir, lambda: (_FakeHnsw(), {}))
        assert snapshot_has_live_reader(snapshot_root, lease_root=lease_root)
        assert not snapshot_has_live_reader(index_dir, lease_root=lease_root)
    finally:
        cache._release_reader_lease_locked(index_dir)


@pytest.mark.parametrize("name,cache_type,load", _cache_cases())
def test_snapshot_lease_failure_is_loud_for_index_dirs(
    name: str,
    cache_type: Any,
    load: Callable[[Any, str], Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, index_dir = _snapshot_index_dir(tmp_path)
    cache = cache_type(is_versioned_snapshot=is_versioned_snapshot)
    with caplog.at_level("ERROR"):
        load(cache, index_dir)
    assert "reader-lease" in caplog.text


def test_chunk_store_reader_lease_failure_is_loud_for_index_dirs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, index_dir = _snapshot_index_dir(tmp_path)
    cache = chunk_store_cache.ChunkStoreThreadCache(
        is_versioned_snapshot=is_versioned_snapshot
    )
    with caplog.at_level("ERROR"):
        lease = cache._acquire_reader_lease(index_dir)
    assert lease is None
    assert "reader-lease" in caplog.text


def test_non_snapshot_cache_path_stays_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = hnsw_index_cache.HNSWIndexCache(is_versioned_snapshot=is_versioned_snapshot)
    with caplog.at_level("ERROR"):
        cache.get_or_load(
            str(tmp_path / "ordinary" / "index"), lambda: (_FakeHnsw(), {})
        )
    assert "reader-lease" not in caplog.text
