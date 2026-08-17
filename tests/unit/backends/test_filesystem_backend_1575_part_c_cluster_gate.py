"""TDD tests for Bug #1575 Part C AC46 -- the cluster/postgres fail-closed
gate wired into ``FilesystemBackend.get_vector_store_client()``.

Uses the REAL ``app.state.storage_mode`` simulation pattern already
established in ``test_alias_lock_store_factory_1546.py`` (never
monkeypatching ``is_postgres_storage_mode`` itself, since it is imported
LOCALLY inside the function under test -- proving the actual end-to-end
contract via the real probe, not a patched stand-in).
"""

import contextlib

from code_indexer.backends.filesystem_backend import FilesystemBackend
from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)


def _make_hnsw_cache() -> HNSWIndexCache:
    return HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=60.0))


@contextlib.contextmanager
def _app_state_storage_mode(value):
    from code_indexer.server import app as app_module

    _unset = object()
    saved = getattr(app_module.app.state, "storage_mode", _unset)
    try:
        app_module.app.state.storage_mode = value
        yield
    finally:
        if saved is _unset:
            if hasattr(app_module.app.state, "storage_mode"):
                delattr(app_module.app.state, "storage_mode")
        else:
            app_module.app.state.storage_mode = saved


def test_postgres_storage_mode_disables_hnsw_sync_epoch(tmp_path):
    backend = FilesystemBackend(
        project_root=tmp_path, hnsw_index_cache=_make_hnsw_cache()
    )
    with _app_state_storage_mode("postgres"):
        store = backend.get_vector_store_client()

    assert store._hnsw_sync_epoch_enabled is False


def test_sqlite_storage_mode_keeps_hnsw_sync_epoch_enabled(tmp_path):
    backend = FilesystemBackend(
        project_root=tmp_path, hnsw_index_cache=_make_hnsw_cache()
    )
    with _app_state_storage_mode("sqlite"):
        store = backend.get_vector_store_client()

    assert store._hnsw_sync_epoch_enabled is True


def test_no_hnsw_index_cache_cli_daemon_mode_keeps_epoch_enabled(tmp_path):
    """CLI/daemon mode never sets hnsw_index_cache -- must default enabled
    regardless of any app.state (which shouldn't even be consulted)."""
    backend = FilesystemBackend(project_root=tmp_path, hnsw_index_cache=None)

    store = backend.get_vector_store_client()

    assert store._hnsw_sync_epoch_enabled is True


def test_cli_child_env_var_disables_hnsw_sync_epoch_without_app_state(
    tmp_path, monkeypatch
):
    """Bug #1575 Part C review Defect 3a bypass 3: a spawned CLI child
    process (e.g. the server's own `cidx index --fts` subprocess) has NO
    app.state to inspect via is_postgres_storage_mode() -- hnsw_index_cache
    is always None there (CLI mode). The parent server must be able to
    signal postgres/cluster mode via an explicit env var instead, and this
    call site must honor it even with hnsw_index_cache=None.
    """
    from code_indexer.storage.shared.hnsw_sync_state import (
        CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
    )

    monkeypatch.setenv(CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV, "1")
    backend = FilesystemBackend(project_root=tmp_path, hnsw_index_cache=None)

    store = backend.get_vector_store_client()

    assert store._hnsw_sync_epoch_enabled is False
