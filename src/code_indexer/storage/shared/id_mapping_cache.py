# ^ perf uncached id_mapping parse = 42 ms per query at 57k labels; 7.7% of GIL under fan-out; #2226

# ^ invariant every collection_meta.json writer replaces it atomically => new inode; test:test_atomic_replace_with_same_size_and_mtime_is_observed
# ^ see filesystem_vector_store.py:FilesystemVectorStore._atomic_write_json; hnsw_index_manager.py:HNSWIndexManager._atomic_write_metadata_durable
# ^ see chunk_layout.py:_atomic_write_json_durable; temporal_projection_matrix.py; fleet_migration/completion_gate.py

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Dict, Iterator, Mapping, NamedTuple, Optional, Union

from code_indexer.server.services.query_path_cache import (
    is_immutable_versioned_snapshot,
)
from code_indexer.storage.shared.collection_meta_cache import (
    CollectionMetaCache,
    _MetaCacheKey,
)

logger = logging.getLogger(__name__)

_COLLECTION_META_FILENAME = "collection_meta.json"
_MUTABLE_TTL_SECONDS = 30.0
# ^ perf parsed map = 3.5 x its JSON (tracemalloc, 57k and 400k labels); 4 x whole file over-counts => safe bound
_WEIGHT_PER_FILE_BYTE = 4
# ^ perf node budget 256 MiB = 1/16 of DEFAULT_MAX_CACHE_SIZE_MB; largest seen map (400k labels) = 62 MiB parsed
_NODE_BUDGET_BYTES = 256 * 1024 * 1024
_MIN_WORKER_BUDGET_BYTES = 64 * 1024 * 1024

_EMPTY: Mapping[int, str] = MappingProxyType({})


def read_id_mapping(meta_file: Path) -> Dict[int, str]:
    if not meta_file.exists():
        return {}
    try:
        with open(meta_file) as f:
            metadata = json.load(f)
        if "hnsw_index" not in metadata:
            return {}
        id_mapping_str = metadata["hnsw_index"].get("id_mapping", {})
        return {int(k): v for k, v in id_mapping_str.items()}
    except (json.JSONDecodeError, KeyError, ValueError):
        return {}


def _weight(key: _MetaCacheKey) -> int:
    return _WEIGHT_PER_FILE_BYTE * key[2]


class _Entry(NamedTuple):
    value: Mapping[int, str]
    weight: int
    expires_at: Optional[float]


class _KeyLock:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refcount = 0


class IdMappingCache:
    def __init__(
        self,
        budget_bytes: int = _NODE_BUDGET_BYTES,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if budget_bytes < 1:
            raise ValueError(f"budget_bytes must be >= 1, got {budget_bytes}")
        self._budget = budget_bytes
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._entries: "OrderedDict[_MetaCacheKey, _Entry]" = OrderedDict()
        self._key_of_path: Dict[str, _MetaCacheKey] = {}
        self._key_locks: Dict[_MetaCacheKey, _KeyLock] = {}
        self._bytes = 0
        self._oversize_logged = False

    @property
    def budget_bytes(self) -> int:
        return self._budget

    def retained_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def get(self, collection_dir: Union[str, Path]) -> Mapping[int, str]:
        key = CollectionMetaCache._stat_key(collection_dir)
        if key is None:
            # ^ hazard returning empty here => EACCES/EIO answers empty results; test:test_unreadable_metadata_raises_instead_of_empty_mapping
            meta_file = Path(collection_dir) / _COLLECTION_META_FILENAME
            return MappingProxyType(read_id_mapping(meta_file))
        hit = self._hit(key)
        if hit is not None:
            return hit
        with self._single_flight(key):
            hit = self._hit(key)
            if hit is not None:
                return hit
            parsed = read_id_mapping(Path(key[0]) / _COLLECTION_META_FILENAME)
            value: Mapping[int, str] = MappingProxyType(parsed)
            self._store(key, value, _weight(key))
            return value

    @contextmanager
    def _single_flight(self, key: _MetaCacheKey) -> Iterator[None]:
        with self._lock:
            holder = self._key_locks.setdefault(key, _KeyLock())
            holder.refcount += 1
        try:
            with holder.lock:
                yield
        finally:
            with self._lock:
                holder.refcount -= 1
                if holder.refcount == 0:
                    del self._key_locks[key]

    def _hit(self, key: _MetaCacheKey) -> Optional[Mapping[int, str]]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at is not None and self._time_fn() >= entry.expires_at:
                self._drop(key)
                return None
            self._entries.move_to_end(key)
            return entry.value

    def _drop(self, key: _MetaCacheKey) -> None:
        entry = self._entries.pop(key)
        self._bytes -= entry.weight
        if self._key_of_path.get(key[0]) == key:
            del self._key_of_path[key[0]]

    def _store(self, key: _MetaCacheKey, value: Mapping[int, str], weight: int) -> None:
        if weight > self._budget:
            self._log_oversize(key[0], weight)
            return
        immutable = is_immutable_versioned_snapshot(key[0])
        with self._lock:
            older = self._key_of_path.get(key[0])
            if older is not None and older in self._entries:
                self._drop(older)
            expires_at = None if immutable else self._time_fn() + _MUTABLE_TTL_SECONDS
            self._entries[key] = _Entry(value, weight, expires_at)
            self._key_of_path[key[0]] = key
            self._bytes += weight
            while self._bytes > self._budget:
                self._drop(next(iter(self._entries)))
            assert 0 <= self._bytes <= self._budget

    def _log_oversize(self, path: str, weight: int) -> None:
        with self._lock:
            if self._oversize_logged:
                return
            self._oversize_logged = True
        logger.info(
            "HNSW id mapping of %s (~%d MiB weighed) exceeds the %d MiB id-mapping "
            "cache budget (%d MiB retained); it is parsed on every query",
            path,
            weight // (1024 * 1024),
            self._budget // (1024 * 1024),
            self.retained_bytes() // (1024 * 1024),
        )


_instance: Optional[IdMappingCache] = None
_instance_lock = threading.Lock()


def get_global_id_mapping_cache() -> IdMappingCache:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = IdMappingCache()
    return _instance


def configure_global_id_mapping_cache(worker_count: int) -> None:
    global _instance
    budget = max(_MIN_WORKER_BUDGET_BYTES, _NODE_BUDGET_BYTES // max(1, worker_count))
    with _instance_lock:
        if _instance is None:
            _instance = IdMappingCache(budget_bytes=budget)


def reset_global_id_mapping_cache() -> None:
    global _instance
    with _instance_lock:
        _instance = None
