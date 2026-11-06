"""The xray feature's on_startup / on_shutdown (cidx_xray.server.lifecycle).

What the core's lifespan and service init used to wire for X-Ray: the
dedicated executor, the execution backend and placement host, the cell
limiter (resized on a committed config change), the graph wire-file cache
under the memory governor and the K-calibration provider. Driven with a
FeatureContext whose ``config`` and ``storage`` capabilities are test
doubles of the SDK Protocols; the server platform is the core's real one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Mapping

import pytest
from cidx_sdk.capabilities import CONFIG, STORAGE
from cidx_sdk.capability_types import STORAGE_SQLITE

from cidx_xray._platform import capabilities
from cidx_xray.server import runtime
from cidx_xray.server.graph_governor.cache_proxy import get_xray_graph_cache
from cidx_xray.server.graph_governor.k_calibration_store import get_xray_k_provider
from cidx_xray.server.lifecycle import XrayRuntime
from cidx_xray.server.migrations import XRAY_MIGRATIONS
from code_indexer.server import feature_platform
from code_indexer.server.services.governed_cache_composite import CompositeLRUCache


class _Config:
    def __init__(self, values: Dict[str, Any]) -> None:
        self.values = values
        self.callbacks: List[Callable[[], None]] = []

    def runtime_config(self) -> object:
        return type("Runtime", (), {"content_backend": "local"})()

    def section(self) -> Mapping[str, object]:
        return dict(self.values)

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)


class _Storage:
    def __init__(self, db: Path) -> None:
        self._db = db

    @property
    def mode(self) -> str:
        return STORAGE_SQLITE

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class _Context:
    def __init__(self, caps: Dict[str, object]) -> None:
        self._caps = caps

    @property
    def feature_name(self) -> str:
        return "xray"

    @property
    def sdk_version(self) -> str:
        return "0.3.0"

    @property
    def core_version(self) -> str:
        return "test"

    def provided_capabilities(self) -> FrozenSet[str]:
        return frozenset(self._caps)

    def has_capability(self, name: str) -> bool:
        return name in self._caps

    def capability(self, name: str) -> object:
        return self._caps[name]


class _Hnsw:
    def get_stats(self) -> Any:
        return type("Stats", (), {"cached_repositories": 0})()

    def evict_lru_entries(self, n: int) -> int:
        return 0


@pytest.fixture
def composite() -> Iterator[CompositeLRUCache]:
    cache = CompositeLRUCache([_Hnsw()], floor_per_cache=1)
    feature_platform.set_governed_cache_composite(cache)
    try:
        yield cache
    finally:
        feature_platform.set_governed_cache_composite(None)


def _context(tmp_path: Path, values: Dict[str, Any]) -> _Context:
    db = tmp_path / "cidx_server.db"
    conn = sqlite3.connect(db)
    for migration in XRAY_MIGRATIONS:
        migration.apply(conn)
    conn.commit()
    conn.close()
    return _Context({CONFIG: _Config(values), STORAGE: _Storage(db)})


def test_startup_installs_everything_and_shutdown_removes_it(
    tmp_path: Path, composite: CompositeLRUCache
) -> None:
    ctx = _context(tmp_path, {"xray_worker_threads": 3, "xray_backend": "local"})
    started = XrayRuntime()
    started.start(ctx)
    try:
        assert runtime.context() is ctx
        assert runtime.executor() is not None
        limiter = runtime.cell_limiter()
        assert limiter is not None and limiter.limit == 3
        assert runtime.EXECUTION_BACKEND.get() is None  # local: in-process
        assert capabilities.XRAY_PLACEMENT_HOST.get() is not None
        graph_cache = get_xray_graph_cache()
        assert graph_cache is not None and graph_cache in composite._members()
        assert get_xray_k_provider() is not None
    finally:
        started.stop()
    assert runtime.context() is None
    assert runtime.executor() is None
    assert runtime.cell_limiter() is None
    assert capabilities.XRAY_PLACEMENT_HOST.get() is None
    assert get_xray_graph_cache() is None
    assert get_xray_k_provider() is None
    assert len(composite._members()) == 1


def test_a_committed_worker_threads_change_resizes_the_limiter(
    tmp_path: Path, composite: CompositeLRUCache
) -> None:
    ctx = _context(tmp_path, {"xray_worker_threads": 2})
    config = ctx.capability(CONFIG)
    assert isinstance(config, _Config)
    started = XrayRuntime()
    started.start(ctx)
    try:
        config.values["xray_worker_threads"] = 6
        for callback in list(config.callbacks):
            callback()
        limiter = runtime.cell_limiter()
        assert limiter is not None and limiter.limit == 6
    finally:
        started.stop()
    assert config.callbacks == []


def test_an_invalid_backend_refuses_startup_and_leaves_nothing(
    tmp_path: Path, composite: CompositeLRUCache
) -> None:
    ctx = _context(tmp_path, {"xray_backend": "sideways"})
    with pytest.raises(RuntimeError, match="Unknown xray_backend"):
        XrayRuntime().start(ctx)
    assert runtime.context() is None
    assert runtime.executor() is None
    assert runtime.cell_limiter() is None


def test_delegated_without_a_plugin_refuses_startup(
    tmp_path: Path, composite: CompositeLRUCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No silent fallback to in-pod execution of a caller's evaluator."""
    from cidx_xray.server import backend_plugin

    monkeypatch.setattr(backend_plugin, "resolve_xray_backend", lambda: None)
    ctx = _context(tmp_path, {"xray_backend": "delegated"})
    with pytest.raises(RuntimeError, match="no X-Ray backend plugin"):
        XrayRuntime().start(ctx)
    assert runtime.EXECUTION_BACKEND.get() is None
