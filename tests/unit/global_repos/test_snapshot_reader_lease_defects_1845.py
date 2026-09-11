"""Discriminating regression tests for the second-round #1845 defects."""

from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.global_repos.snapshot_reader_lease import (
    SnapshotReaderLease,
    _lease_directory,
    snapshot_has_live_reader,
)
import code_indexer.global_repos.snapshot_reader_lease as lease_module
from code_indexer.server.cache import hnsw_index_cache
from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
    HNSWIndexCacheEntry,
)


def test_reader_writer_use_the_same_injected_lease_root(tmp_path: Path) -> None:
    golden_repos = tmp_path / "data" / "golden-repos"
    snapshot = golden_repos / "repo" / ".versioned" / "ns" / "v_123"
    snapshot.mkdir(parents=True)
    lease_root = golden_repos / "cidx-meta"

    lease = SnapshotReaderLease(str(snapshot), 120.0, lease_root=lease_root)
    lease.acquire()
    try:
        assert snapshot_has_live_reader(str(snapshot), lease_root=lease_root)
        assert (
            _lease_directory(str(snapshot), lease_root=lease_root, create=False)
            == lease_root / ".snapshot-reader-leases"
        )
        assert not (golden_repos / "repo" / "cidx-meta").exists()
    finally:
        lease.release()


def test_lease_without_a_resolved_root_fails_loudly(tmp_path: Path) -> None:
    snapshot = tmp_path / ".versioned" / "ns" / "v_123"
    snapshot.mkdir(parents=True)

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        SnapshotReaderLease(str(snapshot), 120.0)  # type: ignore[call-arg]  # deliberately omitted to prove the runtime guard


def test_lease_module_has_no_duplicate_snapshot_predicate() -> None:
    assert not hasattr(lease_module, "is_versioned_snapshot_path")


def test_cache_hit_does_not_spawn_or_renew_a_lease(monkeypatch, tmp_path: Path) -> None:
    repo_path = str(tmp_path / "repo")
    cache = HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=10.0))

    class FakeIndex:
        def index_file_size(self) -> int:
            return 1

    class FakeLease:
        renewals = 0

        def renew(self) -> None:
            self.renewals += 1
            hnsw_index_cache.os.replace("lease.tmp", "lease.json")

    lease = FakeLease()
    entry = HNSWIndexCacheEntry(
        hnsw_index=FakeIndex(),
        id_mapping={1: "one"},
        repo_path=repo_path,
        ttl_minutes=10.0,
    )
    with cache._cache_lock:
        cache._cache[repo_path] = entry
        cache._reader_leases[repo_path] = lease  # type: ignore[assignment]

    spawned: List[
        Any
    ] = []  # Any: entries are heterogeneous -- (args, kwargs) tuples and the plain string "started"

    class SpyThread:
        def __init__(self, *args, **kwargs) -> None:
            spawned.append((args, kwargs))
            self._target = kwargs["target"]
            self._args = kwargs["args"]

        def start(self) -> None:
            spawned.append("started")
            self._target(*self._args)

    write_calls = []
    monkeypatch.setattr(
        hnsw_index_cache.os,
        "replace",
        lambda *args: write_calls.append(args),
    )
    monkeypatch.setattr(hnsw_index_cache.threading, "Thread", SpyThread)
    result = cache.get_or_load(
        repo_path, loader=lambda: pytest.fail("cache hit reloaded"), index_file=None
    )

    assert result[1] == {1: "one"}
    assert spawned == []
    assert lease.renewals == 0
    assert write_calls == []
