"""TDD tests for Bug #1575 Part C AC46 -- the cluster/postgres fail-closed
gate wired into ``FilesystemBackend.get_vector_store_client()``.

Drives the REAL probe (never monkeypatching ``is_postgres_storage_mode``
itself, since it is imported LOCALLY inside the function under test): the
server app whose ``state.storage_mode`` it reads is installed exactly where
``registry_factory._running_server_app_state()`` looks for it.
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
    """A running server app with ``state.storage_mode == value``, placed in
    ``sys.modules['code_indexer.server.app'].__dict__['app']`` (where the
    real probe reads it), restored afterwards.

    A lightweight real FastAPI app with a real starlette ``State`` -- NOT the
    module's lazy ``app`` singleton: touching ``app_module.app`` runs the
    whole ``create_app()`` (DB schema, migrations, admin seeding: 3-9 s, a
    15 s gate timeout under load), which this unit test does not need."""
    from fastapi import FastAPI

    from code_indexer.server import app as app_module  # never builds the app

    server_app = FastAPI()
    server_app.state.storage_mode = value
    _unset = object()
    saved = app_module.__dict__.get("app", _unset)
    app_module.__dict__["app"] = server_app
    try:
        yield
    finally:
        if saved is _unset:
            del app_module.__dict__["app"]
        else:
            app_module.__dict__["app"] = saved


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
