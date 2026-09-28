"""Indexer resume-state trust and containment.

``GitSyncExecutor._trigger_cidx_index`` (git_sync_executor.py)
calls ``SmartIndexer.smart_index()`` IN-PROCESS (no `cidx` subprocess is
spawned here, unlike every other server-spawned indexing call site) against
a repository whose working tree a tenant/committer can write to. Its stored
``.code-indexer/metadata-<provider>.json`` resume state must never be
trusted as provenance for what the server indexes on the caller's behalf,
so this call site must pass ``trust_resume_state=False`` directly (there is
no CLI subprocess here, so the ``append_server_layout_args``
``--ignore-resume-state`` seam used by every other server spawn site does
not apply).

Mirrors the mocking pattern of
tests/unit/server/git/test_git_sync_executor_1575_part_c_cluster_gate.py,
extended so ``health_check()`` returns True on both collaborators (reaching
the real `smart_index()` call), with `SmartIndexer` itself replaced by a
recording spy so the actual keyword arguments reach this test.
"""

from __future__ import annotations

import contextlib
import subprocess
from unittest.mock import MagicMock, patch

# Imported up front so the patch windows below never perform the FIRST
# import of these modules: patching SmartIndexer imports smart_indexer (and
# high_throughput_processor) while FilesystemVectorStore is patched, which
# would bind the mock into their module namespaces for the rest of the
# session and break later tests that index for real.
import code_indexer.services.smart_indexer  # noqa: F401
from code_indexer.server.git.git_sync_executor import GitSyncExecutor


@contextlib.contextmanager
def _app_state_storage_mode(value):
    from code_indexer.server import app as app_module

    _unset = object()
    saved = getattr(app_module.app.state, "storage_mode", _unset)
    saved_http = getattr(app_module.app.state, "http_client_factory", _unset)
    try:
        app_module.app.state.storage_mode = value
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


def test_trigger_cidx_index_passes_trust_resume_state_false(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    executor = GitSyncExecutor(repository_path=tmp_path)

    mock_config = MagicMock()
    mock_config.codebase_dir = tmp_path
    mock_config.voyage_ai.parallel_requests = 8
    mock_config_manager = MagicMock()
    mock_config_manager.load.return_value = mock_config

    mock_embedding_provider = MagicMock()
    mock_embedding_provider.health_check.return_value = True

    mock_store = MagicMock()
    mock_store.health_check.return_value = True

    mock_stats = MagicMock()
    mock_stats.cancelled = False
    mock_stats.files_processed = 1
    mock_stats.chunks_created = 1

    mock_indexer = MagicMock()
    mock_indexer.smart_index.return_value = mock_stats

    with (
        # Entered first: it imports the server app before any collaborator
        # below is patched (see the module-level import note).
        _app_state_storage_mode("sqlite"),
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
        ),
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            return_value=mock_indexer,
        ),
    ):
        result = executor._trigger_cidx_index()

    assert result is True
    assert mock_indexer.smart_index.call_count == 1
    _, kwargs = mock_indexer.smart_index.call_args
    assert kwargs.get("trust_resume_state") is False, (
        "SECURITY: GitSyncExecutor._trigger_cidx_index must call "
        "smart_index() with trust_resume_state=False -- this server-side "
        f"in-process indexing trigger must never trust repo-authored resume "
        f"state. Got kwargs: {kwargs}"
    )
