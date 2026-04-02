"""Tests for ProviderIndexService (Story #490)."""

import json
from unittest.mock import patch, MagicMock


class TestListProviders:
    """Test list_providers returns configured providers."""

    def test_list_providers_returns_configured(self):
        """list_providers returns info for each configured provider."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_provider_info"
            ) as mock_info,
        ):
            mock_configured.return_value = ["voyage-ai", "cohere"]
            mock_info.return_value = {
                "voyage-ai": {
                    "name": "VoyageAI",
                    "default_model": "voyage-code-3",
                    "supports_batch": True,
                    "api_key_env": "VOYAGE_API_KEY",
                },
                "cohere": {
                    "name": "Cohere",
                    "default_model": "embed-v4.0",
                    "supports_batch": True,
                    "api_key_env": "CO_API_KEY",
                },
            }

            result = service.list_providers()

            assert len(result) == 2
            assert result[0]["name"] == "voyage-ai"
            assert result[1]["name"] == "cohere"

    def test_list_providers_returns_display_name(self):
        """list_providers includes display_name from provider info."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_provider_info"
            ) as mock_info,
        ):
            mock_configured.return_value = ["voyage-ai"]
            mock_info.return_value = {
                "voyage-ai": {
                    "name": "VoyageAI",
                    "default_model": "voyage-code-3",
                    "supports_batch": True,
                    "api_key_env": "VOYAGE_API_KEY",
                },
            }

            result = service.list_providers()

            assert result[0]["display_name"] == "VoyageAI"
            assert result[0]["default_model"] == "voyage-code-3"
            assert result[0]["supports_batch"] is True
            assert result[0]["api_key_env"] == "VOYAGE_API_KEY"

    def test_list_providers_uses_fallback_for_unknown_provider(self):
        """list_providers falls back gracefully when provider not in info dict."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_provider_info"
            ) as mock_info,
        ):
            mock_configured.return_value = ["unknown-provider"]
            mock_info.return_value = {}  # no info for this provider

            result = service.list_providers()

            assert len(result) == 1
            assert result[0]["name"] == "unknown-provider"
            assert result[0]["display_name"] == "unknown-provider"  # fallback to name

    def test_list_providers_empty_when_none_configured(self):
        """list_providers returns empty list when no providers configured."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_provider_info"
            ) as mock_info,
        ):
            mock_configured.return_value = []
            mock_info.return_value = {}

            result = service.list_providers()
            assert result == []


class TestValidateProvider:
    """Test provider name validation."""

    def test_valid_provider_returns_none(self):
        """validate_provider returns None for a valid configured provider."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
        ) as mock_configured:
            mock_configured.return_value = ["voyage-ai", "cohere"]
            assert service.validate_provider("voyage-ai") is None

    def test_invalid_provider_returns_error(self):
        """validate_provider returns error string for unconfigured provider."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
        ) as mock_configured:
            mock_configured.return_value = ["voyage-ai"]
            error = service.validate_provider("nonexistent")
            assert error is not None
            assert "nonexistent" in error
            assert "voyage-ai" in error

    def test_invalid_provider_error_mentions_no_available_when_none_configured(self):
        """validate_provider error message says 'none' when no providers configured."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        with patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
        ) as mock_configured:
            mock_configured.return_value = []
            error = service.validate_provider("nonexistent")
            assert error is not None
            assert "none" in error


class TestRemoveProviderIndex:
    """Test collection removal."""

    def test_remove_nonexistent_collection(self, tmp_path):
        """remove_provider_index returns removed=False when collection absent."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        # Create repo dir structure without collection
        index_dir = tmp_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_create,
        ):
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"
            mock_backend = MagicMock()
            mock_vs_client = MagicMock()
            mock_vs_client.resolve_collection_name.return_value = (
                "voyage_ai__voyage_code_3"
            )
            mock_backend.get_vector_store_client.return_value = mock_vs_client
            mock_backend_create.return_value = mock_backend

            result = service.remove_provider_index(str(tmp_path), "voyage-ai")
            assert result["removed"] is False
            assert result["collection_name"] == "voyage_ai__voyage_code_3"
            assert "does not exist" in result["message"]

    def test_remove_existing_collection(self, tmp_path):
        """remove_provider_index removes the directory and returns removed=True."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        # Create collection dir with content
        index_dir = tmp_path / ".code-indexer" / "index"
        collection_dir = index_dir / "voyage_ai__voyage_code_3"
        collection_dir.mkdir(parents=True)
        (collection_dir / "data.json").write_text("{}")

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_create,
        ):
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"
            mock_backend = MagicMock()
            mock_vs_client = MagicMock()
            mock_vs_client.resolve_collection_name.return_value = (
                "voyage_ai__voyage_code_3"
            )
            mock_backend.get_vector_store_client.return_value = mock_vs_client
            mock_backend_create.return_value = mock_backend

            result = service.remove_provider_index(str(tmp_path), "voyage-ai")
            assert result["removed"] is True
            assert not collection_dir.exists()
            assert "voyage_ai__voyage_code_3" in result["message"]

    def test_remove_also_deletes_metadata_file(self, tmp_path):
        """remove_provider_index removes provider-specific metadata file if present."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        # Create collection dir with content
        index_dir = tmp_path / ".code-indexer" / "index"
        collection_dir = index_dir / "voyage_ai__voyage_code_3"
        collection_dir.mkdir(parents=True)
        (collection_dir / "data.json").write_text("{}")

        # Create provider metadata file
        meta_file = tmp_path / ".code-indexer" / "metadata-voyage-ai.json"
        meta_file.write_text('{"total_chunks": 100}')

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_create,
        ):
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"
            mock_backend = MagicMock()
            mock_vs_client = MagicMock()
            mock_vs_client.resolve_collection_name.return_value = (
                "voyage_ai__voyage_code_3"
            )
            mock_backend.get_vector_store_client.return_value = mock_vs_client
            mock_backend_create.return_value = mock_backend

            result = service.remove_provider_index(str(tmp_path), "voyage-ai")
            assert result["removed"] is True
            assert not meta_file.exists()

    def test_remove_succeeds_without_metadata_file(self, tmp_path):
        """remove_provider_index succeeds when no metadata file exists."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        index_dir = tmp_path / ".code-indexer" / "index"
        collection_dir = index_dir / "voyage_ai__voyage_code_3"
        collection_dir.mkdir(parents=True)
        (collection_dir / "data.json").write_text("{}")

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_create,
        ):
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"
            mock_backend = MagicMock()
            mock_vs_client = MagicMock()
            mock_vs_client.resolve_collection_name.return_value = (
                "voyage_ai__voyage_code_3"
            )
            mock_backend.get_vector_store_client.return_value = mock_vs_client
            mock_backend_create.return_value = mock_backend

            # No metadata file present — should not raise
            result = service.remove_provider_index(str(tmp_path), "voyage-ai")
            assert result["removed"] is True


class TestGetProviderIndexStatus:
    """Test get_provider_index_status for per-provider index reporting."""

    def test_status_returns_empty_for_nonexistent_collection(self, tmp_path):
        """get_provider_index_status marks provider as not existing when no collection dir."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        (tmp_path / ".code-indexer" / "index").mkdir(parents=True)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
        ):
            mock_configured.return_value = ["voyage-ai"]
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"

            result = service.get_provider_index_status(str(tmp_path), "test-repo")

            assert "voyage-ai" in result
            assert result["voyage-ai"]["exists"] is False
            assert result["voyage-ai"]["vector_count"] == 0
            assert result["voyage-ai"]["last_indexed"] is None

    def test_status_returns_exists_for_populated_collection(self, tmp_path):
        """get_provider_index_status marks provider as existing when collection has files."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        collection_dir = (
            tmp_path / ".code-indexer" / "index" / "voyage_ai__voyage_code_3"
        )
        collection_dir.mkdir(parents=True)
        (collection_dir / "chunk.json").write_text("{}")

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_create,
        ):
            mock_configured.return_value = ["voyage-ai"]
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"
            mock_backend = MagicMock()
            mock_vs_client = MagicMock()
            mock_vs_client.resolve_collection_name.return_value = (
                "voyage_ai__voyage_code_3"
            )
            mock_backend.get_vector_store_client.return_value = mock_vs_client
            mock_backend_create.return_value = mock_backend

            result = service.get_provider_index_status(str(tmp_path), "test-repo")

            assert result["voyage-ai"]["exists"] is True
            assert result["voyage-ai"]["collection_name"] == "voyage_ai__voyage_code_3"
            assert result["voyage-ai"]["model"] == "voyage-code-3"

    def test_status_reads_metadata_file_when_present(self, tmp_path):
        """get_provider_index_status reads vector_count and last_indexed from metadata."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        collection_dir = (
            tmp_path / ".code-indexer" / "index" / "voyage_ai__voyage_code_3"
        )
        collection_dir.mkdir(parents=True)
        (collection_dir / "chunk.json").write_text("{}")

        meta_file = tmp_path / ".code-indexer" / "metadata-voyage-ai.json"
        meta_file.write_text(
            json.dumps({"chunks_indexed": 42, "indexed_at": "2026-01-01T00:00:00"})
        )

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create"
            ) as mock_backend_create,
        ):
            mock_configured.return_value = ["voyage-ai"]
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"
            mock_backend = MagicMock()
            mock_vs_client = MagicMock()
            mock_vs_client.resolve_collection_name.return_value = (
                "voyage_ai__voyage_code_3"
            )
            mock_backend.get_vector_store_client.return_value = mock_vs_client
            mock_backend_create.return_value = mock_backend

            result = service.get_provider_index_status(str(tmp_path), "test-repo")

            assert result["voyage-ai"]["vector_count"] == 42
            assert result["voyage-ai"]["last_indexed"] == "2026-01-01T00:00:00"

    def test_status_marks_empty_collection_dir_as_not_exists(self, tmp_path):
        """get_provider_index_status treats empty collection directory as non-existent."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        # Collection directory exists but is empty
        collection_dir = (
            tmp_path / ".code-indexer" / "index" / "voyage_ai__voyage_code_3"
        )
        collection_dir.mkdir(parents=True)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
        ):
            mock_configured.return_value = ["voyage-ai"]
            mock_provider = MagicMock()
            mock_provider.get_current_model.return_value = "voyage-code-3"
            mock_create.return_value = mock_provider
            mock_slug.return_value = "voyage_ai__voyage_code_3"

            result = service.get_provider_index_status(str(tmp_path), "test-repo")

            assert result["voyage-ai"]["exists"] is False

    def test_status_handles_provider_exception_gracefully(self, tmp_path):
        """get_provider_index_status captures provider errors and marks error field."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        (tmp_path / ".code-indexer" / "index").mkdir(parents=True)

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
        ):
            mock_configured.return_value = ["voyage-ai"]
            mock_create.side_effect = RuntimeError("API key missing")

            result = service.get_provider_index_status(str(tmp_path), "test-repo")

            assert "voyage-ai" in result
            assert result["voyage-ai"]["exists"] is False
            assert "error" in result["voyage-ai"]
            assert "API key missing" in result["voyage-ai"]["error"]

    def test_status_returns_multiple_providers(self, tmp_path):
        """get_provider_index_status returns status for all configured providers."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)

        (tmp_path / ".code-indexer" / "index").mkdir(parents=True)

        def make_provider(model_name):
            m = MagicMock()
            m.get_current_model.return_value = model_name
            return m

        slug_map = {
            ("voyage-ai", "voyage-code-3"): "voyage_ai__voyage_code_3",
            ("cohere", "embed-v4.0"): "cohere__embed_v4_0",
        }

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers"
            ) as mock_configured,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
            ) as mock_create,
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.generate_model_slug"
            ) as mock_slug,
        ):
            mock_configured.return_value = ["voyage-ai", "cohere"]

            call_count = [0]

            def create_side(config, provider_name):
                models = ["voyage-code-3", "embed-v4.0"]
                m = MagicMock()
                m.get_current_model.return_value = models[call_count[0] % 2]
                call_count[0] += 1
                return m

            mock_create.side_effect = create_side
            mock_slug.side_effect = lambda p, m: slug_map.get((p, m), f"{p}__{m}")

            result = service.get_provider_index_status(str(tmp_path), "test-repo")

            assert "voyage-ai" in result
            assert "cohere" in result


class TestGetConfig:
    """Test _get_config fallback when no config injected at init."""

    def test_get_config_uses_injected_config(self):
        """_get_config returns the injected config when present."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        mock_config = MagicMock()
        service = ProviderIndexService(config=mock_config)
        assert service._get_config() is mock_config

    def test_get_config_loads_from_config_manager_when_none(self):
        """_get_config loads via ConfigManager when no config injected."""
        from code_indexer.server.services.provider_index_service import (
            ProviderIndexService,
        )

        service = ProviderIndexService(config=None)

        mock_loaded_config = MagicMock()
        mock_manager_instance = MagicMock()
        mock_manager_instance.load.return_value = mock_loaded_config

        with patch("code_indexer.config.ConfigManager") as mock_manager_cls:
            mock_manager_cls.return_value = mock_manager_instance
            result = service._get_config()

        assert result is mock_loaded_config
        mock_manager_cls.assert_called_once()
        mock_manager_instance.load.assert_called_once()
