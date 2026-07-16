"""
Tests for Story #1400 Phase 3: reconstruct_temporal_backend().

Extracts the config/index_path/vector_store reconstruction block that lives
inline in SemanticQueryManager._execute_temporal_query
(ConfigManager.create_with_backtrack -> BackendFactory.create ->
get_vector_store_client()) into a standalone, module-level helper (living in
semantic_query_manager.py -- a server-only module, since the reconstruction
depends on server-only imports like ..app._server_hnsw_cache and
..services.memory_governor that must never leak into the CLI-safe
services/temporal/ package) so a future temporal worker (which has no
SemanticQueryManager instance to call methods on) can reconstruct the
identical backend from just a repo_path + repository_alias.

TDD: written BEFORE implementation.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    reconstruct_temporal_backend,
)


class TestReconstructTemporalBackend:
    def test_returns_config_index_path_and_vector_store(self, tmp_path: Path):
        mock_config = MagicMock()
        mock_config_manager = MagicMock()
        mock_config_manager.get_config.return_value = mock_config
        mock_vector_store = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store

        with (
            patch(
                "code_indexer.proxy.config_manager.ConfigManager.create_with_backtrack",
                return_value=mock_config_manager,
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create",
                return_value=mock_backend,
            ),
        ):
            config, index_path, vector_store = reconstruct_temporal_backend(
                repo_path=tmp_path,
                repository_alias="my-repo",
            )

        assert config is mock_config
        assert index_path == tmp_path / ".code-indexer" / "index"
        assert vector_store is mock_vector_store

    def test_shard_ownership_none_means_cache_used(self, tmp_path: Path):
        """shard_ownership=None (default) -> always use the shared hnsw_cache
        (mirrors _owns_for_cache's None-safe fail-open semantics: sharding
        off or no ownership info -> use the cache)."""
        mock_config_manager = MagicMock()
        mock_config_manager.get_config.return_value = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = MagicMock()

        with (
            patch(
                "code_indexer.proxy.config_manager.ConfigManager.create_with_backtrack",
                return_value=mock_config_manager,
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create",
                return_value=mock_backend,
            ) as mock_create,
        ):
            reconstruct_temporal_backend(
                repo_path=tmp_path,
                repository_alias="my-repo",
                shard_ownership=None,
            )

        _, kwargs = mock_create.call_args
        assert kwargs["hnsw_cache"] is not None

    def test_shard_ownership_denies_bypasses_cache(self, tmp_path: Path):
        mock_config_manager = MagicMock()
        mock_config_manager.get_config.return_value = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = MagicMock()

        denying_ownership = MagicMock()
        denying_ownership.owns.return_value = False

        with (
            patch(
                "code_indexer.proxy.config_manager.ConfigManager.create_with_backtrack",
                return_value=mock_config_manager,
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create",
                return_value=mock_backend,
            ) as mock_create,
        ):
            reconstruct_temporal_backend(
                repo_path=tmp_path,
                repository_alias="my-repo",
                shard_ownership=denying_ownership,
            )

        _, kwargs = mock_create.call_args
        assert kwargs["hnsw_cache"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
