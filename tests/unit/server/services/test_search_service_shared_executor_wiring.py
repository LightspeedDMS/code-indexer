"""Server query path threads the shared query executor down to the leaf.

perf refactor: the server owns ONE long-lived ThreadPoolExecutor
(app.state.query_executor). SemanticSearchService must resolve it (via the
patchable _get_query_executor() helper, mirroring _get_http_client_factory())
and pass it as parallel_executor= into FilesystemVectorStore.search(), so the
embed||index-load fan-out reuses the shared pool instead of creating a fresh
per-request ThreadPoolExecutor (the _global_shutdown_lock contention source).

When no server executor is available (helper returns None), search() receives
parallel_executor=None and falls back to its per-call pool (unchanged behaviour).
"""

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.services.search_service import SemanticSearchService
from src.code_indexer.server.models.api_models import SemanticSearchRequest
from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _make_fs_repo(temp_dir: str) -> str:
    repo_path = Path(temp_dir) / "test_repo"
    repo_path.mkdir()
    config_dir = repo_path / ".code-indexer"
    config_dir.mkdir()
    config_data = {
        "embedding": {
            "provider": "voyage",
            "model": "voyage-3-large",
            "dimensions": 1024,
        },
        "vector_store": {"provider": "filesystem"},
        "chunking": {
            "chunk_size": 512,
            "chunk_overlap": 128,
            "tree_sitter_config": {"python": {"enabled": True}},
        },
    }
    (config_dir / "config.json").write_text(json.dumps(config_data, indent=2))
    (config_dir / "index").mkdir()
    return str(repo_path)


def _run_capture(repo_path: str):
    """Run a search and return the kwargs FilesystemVectorStore.search received."""
    search_service = SemanticSearchService()
    mock_embedding_service = MagicMock()
    mock_embedding_service.get_embedding.return_value = [0.1] * 1024

    captured = {}

    def tracked_search(self, *args, **kwargs):
        captured.update(kwargs)
        return [], {}

    with (
        patch.object(FilesystemVectorStore, "search", tracked_search),
        patch(
            "src.code_indexer.server.services.search_service._get_http_client_factory",
            return_value=None,
        ),
    ):
        with patch(
            "src.code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
            return_value=mock_embedding_service,
        ):
            request = SemanticSearchRequest(
                query="authentication logic", limit=5, include_source=True
            )
            try:
                search_service.search_repository_path(repo_path, request)
            except Exception:
                pass
    return captured


@pytest.mark.slow
class TestSearchServiceThreadsSharedExecutor:
    def test_shared_executor_passed_when_server_executor_present(self):
        """Server path: parallel_executor is the shared pool from _get_query_executor()."""
        shared = ThreadPoolExecutor(max_workers=4)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                repo_path = _make_fs_repo(temp_dir)
                with patch(
                    "src.code_indexer.server.services.search_service._get_query_executor",
                    return_value=shared,
                ):
                    captured = _run_capture(repo_path)

            assert "parallel_executor" in captured, (
                "FilesystemVectorStore.search() must receive 'parallel_executor' "
                f"on the server path. Got kwargs: {sorted(captured.keys())}"
            )
            assert captured["parallel_executor"] is shared, (
                "parallel_executor must be the shared app-level executor."
            )
        finally:
            shared.shutdown(wait=True)

    def test_no_executor_passes_none_when_server_executor_absent(self):
        """No server executor: parallel_executor resolves to None (CLI-in-process)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = _make_fs_repo(temp_dir)
            with patch(
                "src.code_indexer.server.services.search_service._get_query_executor",
                return_value=None,
            ):
                captured = _run_capture(repo_path)

        # Either omitted or explicitly None — both mean "use the per-call pool".
        assert captured.get("parallel_executor") is None, (
            "Without a server executor, search() must receive parallel_executor=None. "
            f"Got: {captured.get('parallel_executor')!r}"
        )
