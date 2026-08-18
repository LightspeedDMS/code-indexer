"""TDD test for Bug #1575 Part C review Defect 3a bypass 2.

``GitSyncExecutor._trigger_cidx_index`` (git_sync_executor.py) constructs
``FilesystemVectorStore`` with no ``hnsw_sync_epoch_enabled`` kwarg at all
-- unlike ``FilesystemBackend.get_vector_store_client()``, which already
resolves ``hnsw_sync_epoch_enabled = not is_postgres_storage_mode()``. This
means a cluster/postgres-mode server silently leaves the epoch-sync
mechanism ENABLED at this call site regardless of storage mode.

Uses the same real ``app.state.storage_mode`` simulation pattern already
established in test_filesystem_backend_1575_part_c_cluster_gate.py --
never monkeypatching ``is_postgres_storage_mode`` itself.
"""

import contextlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.git.git_sync_executor import GitSyncExecutor


@contextlib.contextmanager
def _app_state_storage_mode(value):
    from code_indexer.server import app as app_module

    _unset = object()
    saved = getattr(app_module.app.state, "storage_mode", _unset)
    saved_http = getattr(app_module.app.state, "http_client_factory", _unset)
    try:
        app_module.app.state.storage_mode = value
        # _trigger_cidx_index reads app.state.http_client_factory directly
        # (no getattr default) -- must exist or the method raises before
        # ever reaching the FilesystemVectorStore construction under test.
        app_module.app.state.http_client_factory = MagicMock()
        yield
    finally:
        if saved is _unset:
            if hasattr(app_module.app.state, "storage_mode"):
                delattr(app_module.app.state, "storage_mode")
        else:
            app_module.app.state.storage_mode = saved
        if saved_http is _unset:
            if hasattr(app_module.app.state, "http_client_factory"):
                delattr(app_module.app.state, "http_client_factory")
        else:
            app_module.app.state.http_client_factory = saved_http


def test_trigger_cidx_index_threads_postgres_storage_mode_to_disable_epoch(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    executor = GitSyncExecutor(repository_path=tmp_path)

    mock_config = MagicMock()
    mock_config.codebase_dir = tmp_path
    mock_config_manager = MagicMock()
    mock_config_manager.load.return_value = mock_config

    mock_embedding_provider = MagicMock()
    # False so the method returns right after constructing
    # vector_store_client -- no need to stand up a real SmartIndexer.
    mock_embedding_provider.health_check.return_value = False

    mock_store = MagicMock()

    # ConfigManager and EmbeddingProviderFactory are imported LOCALLY inside
    # _trigger_cidx_index (`from ...config import ConfigManager`, `from
    # ...services.embedding_factory import EmbeddingProviderFactory`) --
    # patch their DEFINING modules, not git_sync_executor's own namespace,
    # since the local import resolves the name at call time.
    with (
        patch(
            "code_indexer.config.ConfigManager.create_with_backtrack",
            return_value=mock_config_manager,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            return_value=mock_embedding_provider,
        ),
        patch(
            "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
            return_value=mock_store,
        ) as mock_store_cls,
        _app_state_storage_mode("postgres"),
    ):
        result = executor._trigger_cidx_index()

    assert result is False
    index_dir = Path(tmp_path) / ".code-indexer" / "index"
    mock_store_cls.assert_any_call(
        base_path=index_dir,
        project_root=Path(tmp_path),
        hnsw_sync_epoch_enabled=False,
    )
