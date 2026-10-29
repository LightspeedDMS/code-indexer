import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import List

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.id_mapping_cache import (
    reset_global_id_mapping_cache,
)

DIM = 8

_trace = threading.local()


def _audit(event: str, args: tuple) -> None:
    opened = getattr(_trace, "opened", None)
    if event == "open" and opened is not None:
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)):
            opened.append(os.fsdecode(path))


# ^ hazard audit hooks cannot be removed => install once, gate per thread
sys.addaudithook(_audit)


class _OpenTrace:
    def __enter__(self) -> "_OpenTrace":
        self.opened: List[str] = []
        _trace.opened = self.opened
        return self

    def __exit__(self, *exc: object) -> None:
        _trace.opened = None

    def meta_opens(self) -> int:
        return sum(1 for p in self.opened if p.endswith("collection_meta.json"))


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_global_id_mapping_cache()
    yield
    reset_global_id_mapping_cache()


def _build(manager: HNSWIndexManager, path: Path, ids: List[str]) -> np.ndarray:
    rng = np.random.default_rng(7)
    vectors = rng.random((len(ids), DIM), dtype=np.float32)
    manager.build_index(path, vectors, ids)
    return vectors


def test_repeated_queries_parse_metadata_once(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)

    with _OpenTrace() as trace:
        for _ in range(5):
            ids, _ = manager.query(index, vectors[0], tmp_path, k=2)
            assert ids[0] == "a"

    assert trace.meta_opens() == 1


def test_rebuilt_metadata_is_observed_on_next_query(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)
    first, _ = manager.query(index, vectors[0], tmp_path, k=1)
    assert first == ["a"]

    _build(manager, tmp_path, ["w", "x", "y", "z"])
    index = manager.load_index(tmp_path, max_elements=100)
    second, _ = manager.query(index, vectors[0], tmp_path, k=1)

    assert second == ["w"]


def test_in_place_rewrite_with_new_size_is_observed(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)
    manager.query(index, vectors[0], tmp_path, k=1)

    meta_file = tmp_path / "collection_meta.json"
    meta = json.loads(meta_file.read_text())
    meta["hnsw_index"]["id_mapping"]["0"] = "renamed-point-a"
    meta_file.write_text(json.dumps(meta))

    ids, _ = manager.query(index, vectors[0], tmp_path, k=1)

    assert ids == ["renamed-point-a"]


def test_missing_metadata_falls_back_to_label_ids(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)
    manager.query(index, vectors[0], tmp_path, k=1)

    (tmp_path / "collection_meta.json").unlink()
    ids, _ = manager.query(index, vectors[0], tmp_path, k=1)

    assert ids == ["vec_0"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unreadable_metadata_raises_instead_of_empty_mapping(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)

    tmp_path.chmod(0o600)
    try:
        with pytest.raises(PermissionError):
            manager.query(index, vectors[0], tmp_path, k=1)
    finally:
        tmp_path.chmod(0o700)


def test_write_path_mapping_is_not_shared_with_query_cache(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)
    manager.query(index, vectors[0], tmp_path, k=1)

    _, _, label_to_id, _ = manager.load_for_incremental_update(tmp_path)
    label_to_id[0] = "mutated-by-writer"

    ids, _ = manager.query(index, vectors[0], tmp_path, k=1)
    assert ids == ["a"]


def _pad_mapping(meta_file: Path, extra_labels: int) -> None:
    meta = json.loads(meta_file.read_text())
    mapping = meta["hnsw_index"]["id_mapping"]
    for label in range(1000, 1000 + extra_labels):
        mapping[str(label)] = f"{label:040x}"
    meta_file.write_text(json.dumps(meta))


def test_large_metadata_over_8_mib_is_cached(tmp_path: Path) -> None:
    manager = HNSWIndexManager(vector_dim=DIM, num_threads=1)
    vectors = _build(manager, tmp_path, ["a", "b", "c", "d"])
    index = manager.load_index(tmp_path, max_elements=100)
    meta_file = tmp_path / "collection_meta.json"
    _pad_mapping(meta_file, 200_000)
    assert meta_file.stat().st_size > 8 * 1024 * 1024

    with _OpenTrace() as trace:
        for _ in range(3):
            ids, _ = manager.query(index, vectors[0], tmp_path, k=1)
            assert ids == ["a"]

    assert trace.meta_opens() == 1


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _collection(root: Path, name: str, labels: int) -> Path:
    path = root / name
    path.mkdir()
    mapping = {str(i): f"{name}-{i:032x}" for i in range(labels)}
    (path / "collection_meta.json").write_text(
        json.dumps({"hnsw_index": {"id_mapping": mapping}})
    )
    return path


@pytest.mark.parametrize("immutable, reloads", [(True, 1), (False, 2)])
def test_freshness_by_snapshot_kind(
    tmp_path: Path, monkeypatch, immutable: bool, reloads: int
) -> None:
    from code_indexer.storage.shared import id_mapping_cache

    monkeypatch.setattr(
        id_mapping_cache, "is_immutable_versioned_snapshot", lambda p: immutable
    )
    clock = _Clock()
    cache = id_mapping_cache.IdMappingCache(time_fn=clock)
    path = _collection(tmp_path, "c", 10)

    with _OpenTrace() as trace:
        assert cache.get(path)[3] == "c-" + f"{3:032x}"
        clock.now += 3600
        assert cache.get(path)[3] == "c-" + f"{3:032x}"

    assert trace.meta_opens() == reloads


def _weight(tmp_path: Path, labels: int) -> int:
    from code_indexer.storage.shared.id_mapping_cache import IdMappingCache

    probe = IdMappingCache()
    probe.get(_collection(tmp_path, "probe", labels))
    return probe.retained_bytes()


def test_concurrent_misses_parse_once(tmp_path: Path, monkeypatch) -> None:
    from code_indexer.storage.shared import id_mapping_cache

    cache = id_mapping_cache.IdMappingCache()
    path = _collection(tmp_path, "c", 2000)
    parses: List[int] = []
    real_read = id_mapping_cache.read_id_mapping

    def counted_read(meta_file: Path):
        parses.append(1)
        time.sleep(0.2)
        return real_read(meta_file)

    monkeypatch.setattr(id_mapping_cache, "read_id_mapping", counted_read)
    start = threading.Barrier(8)
    results: List[object] = []

    def worker() -> None:
        start.wait(timeout=10)
        results.append(cache.get(path))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(parses) == 1
    assert len(results) == 8 and all(r is results[0] for r in results)


def test_entry_weight_is_four_times_file_size(tmp_path: Path) -> None:
    from code_indexer.storage.shared.id_mapping_cache import IdMappingCache

    cache = IdMappingCache()
    path = _collection(tmp_path, "c", 3000)
    cache.get(path)

    assert cache.retained_bytes() == 4 * (path / "collection_meta.json").stat().st_size


def _clear_server_caches() -> None:
    import code_indexer.server.cache as cache_module

    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is not None:
            instance.stop_background_cleanup()
            setattr(cache_module, attr, None)
    reset_global_id_mapping_cache()


@pytest.mark.parametrize("workers, mib", [(1, 256), (2, 128), (8, 64), (0, 256)])
def test_initialize_caches_divides_id_mapping_budget_by_workers(
    tmp_path: Path, monkeypatch, workers: int, mib: int
) -> None:
    from code_indexer.server.cache import initialize_caches
    from code_indexer.storage.shared.id_mapping_cache import (
        get_global_id_mapping_cache,
    )

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _clear_server_caches()
    try:
        initialize_caches(worker_count=workers)
        assert get_global_id_mapping_cache().budget_bytes == mib * 1024 * 1024
    finally:
        _clear_server_caches()


def test_retained_bytes_never_exceed_budget(tmp_path: Path) -> None:
    from code_indexer.storage.shared.id_mapping_cache import IdMappingCache

    budget = int(_weight(tmp_path, 500) * 3.5)
    cache = IdMappingCache(budget_bytes=budget)
    for n in range(12):
        cache.get(_collection(tmp_path, f"c{n:02d}", 500))
        assert 0 < cache.retained_bytes() <= budget


def test_eviction_is_least_recently_used(tmp_path: Path) -> None:
    from code_indexer.storage.shared.id_mapping_cache import IdMappingCache

    cache = IdMappingCache(budget_bytes=int(_weight(tmp_path, 500) * 2.5))
    a, b, c = (_collection(tmp_path, n, 500) for n in ("a", "b", "c"))
    cache.get(a)
    cache.get(b)
    cache.get(a)
    cache.get(c)

    with _OpenTrace() as trace:
        cache.get(a)
        cache.get(c)
    assert trace.meta_opens() == 0
    with _OpenTrace() as trace:
        cache.get(b)
    assert trace.meta_opens() == 1


def test_mapping_larger_than_budget_is_not_cached(tmp_path: Path, caplog) -> None:
    from code_indexer.storage.shared import id_mapping_cache

    cache = id_mapping_cache.IdMappingCache(budget_bytes=1024)
    path = _collection(tmp_path, "big", 500)

    with caplog.at_level("INFO", logger=id_mapping_cache.__name__):
        with _OpenTrace() as trace:
            for _ in range(3):
                assert len(cache.get(path)) == 500
    assert trace.meta_opens() == 3
    assert cache.retained_bytes() == 0
    assert sum("exceeds" in r.getMessage() for r in caplog.records) == 1


def test_atomic_replace_with_same_size_and_mtime_is_observed(tmp_path: Path) -> None:
    from code_indexer.storage.shared.id_mapping_cache import IdMappingCache

    cache = IdMappingCache()
    path = _collection(tmp_path, "c", 10)
    meta_file = path / "collection_meta.json"
    before = meta_file.stat()
    assert cache.get(path)[0] == "c-" + f"{0:032x}"
    one_entry = cache.retained_bytes()

    text = meta_file.read_text().replace(f"c-{0:032x}", f"c-{0:032X}")
    tmp_file = path / "collection_meta.json.tmp"
    tmp_file.write_text(text)
    os.utime(tmp_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(tmp_file, meta_file)
    after = meta_file.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    assert cache.get(path)[0] == "c-" + f"{0:032X}"
    assert cache.retained_bytes() == one_entry
