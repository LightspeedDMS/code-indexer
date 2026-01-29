"""
Unit tests for CLI multimodal query integration.

Tests that CLI query command properly integrates MultiIndexQueryService when
multimodal_index exists, maintaining backward compatibility when it doesn't.
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import Mock, patch
from click.testing import CliRunner

from code_indexer.cli import cli


class TestCLIMultimodalQueryIntegration:
    """Test CLI query integration with MultiIndexQueryService."""

    @pytest.fixture
    def runner(self):
        """Create CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create mock configuration."""
        config = Mock()
        config.codebase_dir = str(tmp_path)
        config.embedding_provider = "voyageai"
        config.voyageai_api_key = "test_key"
        config.voyageai_model = "voyage-3"
        return config

    @pytest.fixture
    def project_with_multimodal_index(self, tmp_path):
        """Create project directory with multimodal_index."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        cidx_dir = project_dir / ".code-indexer"
        cidx_dir.mkdir()

        # Create multimodal_index directory
        multimodal_dir = cidx_dir / "multimodal_index"
        multimodal_dir.mkdir()

        return project_dir

    @pytest.fixture
    def project_without_multimodal_index(self, tmp_path):
        """Create project directory without multimodal_index."""
        project_dir = tmp_path / "project_no_mm"
        project_dir.mkdir()

        cidx_dir = project_dir / ".code-indexer"
        cidx_dir.mkdir()

        # No multimodal_index directory

        return project_dir

    def test_query_uses_multi_index_service_when_multimodal_exists(
        self, runner, project_with_multimodal_index, mock_config
    ):
        """
        Test that CLI query instantiates MultiIndexQueryService when multimodal_index exists.

        Validates:
        1. MultiIndexQueryService is instantiated when multimodal_index/ exists
        2. Service is initialized with correct parameters (project_root, vector_store, embedding_provider)
        """
        mock_config.codebase_dir = str(project_with_multimodal_index)

        # Create a valid config.json for local mode detection
        config_file = project_with_multimodal_index / ".code-indexer" / "config.json"
        config_data = {
            "codebase_dir": str(project_with_multimodal_index),
            "embedding_provider": "voyageai",
            "backend": "filesystem",
        }
        config_file.write_text(json.dumps(config_data))

        with runner.isolated_filesystem(temp_dir=project_with_multimodal_index.parent):
            # Change to project directory for mode detection
            original_cwd = os.getcwd()
            try:
                os.chdir(str(project_with_multimodal_index))

                with (
                    patch("code_indexer.cli.ConfigManager") as MockConfigManager,
                    patch(
                        "code_indexer.cli.EmbeddingProviderFactory"
                    ) as MockEmbeddingFactory,
                    patch("code_indexer.cli.BackendFactory") as MockBackendFactory,
                    patch(
                        "code_indexer.services.multi_index_query_service.MultiIndexQueryService"
                    ) as MockMultiIndexService,
                    patch(
                        "code_indexer.services.generic_query_service.GenericQueryService"
                    ) as MockGenericQueryService,
                    patch("code_indexer.services.language_validator.LanguageValidator"),
                    patch("code_indexer.services.language_mapper.LanguageMapper"),
                    patch(
                        "code_indexer.services.git_topology_service.GitTopologyService"
                    ) as MockGitTopology,
                ):

                    # Setup mocks
                    mock_config_manager = Mock()
                    mock_config.codebase_dir = Path(
                        mock_config.codebase_dir
                    )  # Ensure it's a Path
                    mock_config_manager.load.return_value = mock_config
                    MockConfigManager.return_value = mock_config_manager

                    mock_embedding_provider = Mock()
                    mock_embedding_provider.health_check.return_value = True
                    mock_embedding_provider.get_provider_name.return_value = "voyageai"
                    mock_embedding_provider.get_current_model.return_value = "voyage-3"
                    mock_embedding_provider.get_model_info.return_value = {
                        "name": "voyage-3"
                    }
                    MockEmbeddingFactory.create.return_value = mock_embedding_provider

                    mock_backend = Mock()
                    mock_vector_store = Mock()
                    mock_vector_store.health_check.return_value = True
                    mock_vector_store.resolve_collection_name.return_value = "default"
                    mock_vector_store._current_collection_name = "default"
                    mock_vector_store.ensure_payload_indexes.return_value = None
                    mock_backend.get_vector_store_client.return_value = (
                        mock_vector_store
                    )
                    MockBackendFactory.create.return_value = mock_backend

                    # Mock GitTopologyService to follow non-git path
                    mock_git_topology = Mock()
                    mock_git_topology.is_git_available.return_value = False
                    MockGitTopology.return_value = mock_git_topology

                    # Mock GenericQueryService
                    mock_query_service = Mock()
                    mock_query_service.get_current_branch_context.return_value = {
                        "project_id": "test_project"
                    }
                    mock_query_service.filter_results_by_current_branch.return_value = (
                        []
                    )
                    MockGenericQueryService.return_value = mock_query_service

                    # Mock MultiIndexQueryService
                    mock_service_instance = Mock()
                    mock_service_instance.has_multimodal_index.return_value = True
                    mock_service_instance.query.return_value = [
                        {
                            "id": "result1",
                            "score": 0.9,
                            "payload": {
                                "path": "docs/guide.md",
                                "content": "test content",
                                "images": ["diagram.png"],
                            },
                        }
                    ]
                    MockMultiIndexService.return_value = mock_service_instance

                    # Run query with context object
                    result = runner.invoke(
                        cli,
                        ["query", "test query", "--quiet"],
                        obj={"config_manager": mock_config_manager},
                        catch_exceptions=False,
                    )

                    # CRITICAL ASSERTIONS:
                    # 1. MultiIndexQueryService was instantiated
                    MockMultiIndexService.assert_called_once()
                    call_kwargs = MockMultiIndexService.call_args[1]
                    assert call_kwargs["project_root"] == Path(mock_config.codebase_dir)
                    assert call_kwargs["vector_store"] == mock_vector_store
                    assert call_kwargs["embedding_provider"] == mock_embedding_provider

                    # 2. Command succeeded
                    assert result.exit_code == 0
            finally:
                os.chdir(original_cwd)

    def test_query_uses_single_index_when_no_multimodal(
        self, runner, project_without_multimodal_index, mock_config
    ):
        """
        Test backward compatibility: MultiIndexQueryService is instantiated even without multimodal_index.

        Validates:
        1. MultiIndexQueryService is instantiated (backward compatible)
        2. Service is initialized with correct parameters
        """
        mock_config.codebase_dir = str(project_without_multimodal_index)

        # Create a valid config.json for local mode detection
        config_file = project_without_multimodal_index / ".code-indexer" / "config.json"
        config_data = {
            "codebase_dir": str(project_without_multimodal_index),
            "embedding_provider": "voyageai",
            "backend": "filesystem",
        }
        config_file.write_text(json.dumps(config_data))

        with runner.isolated_filesystem(
            temp_dir=project_without_multimodal_index.parent
        ):
            # Change to project directory for mode detection
            original_cwd = os.getcwd()
            try:
                os.chdir(str(project_without_multimodal_index))

                with (
                    patch("code_indexer.cli.ConfigManager") as MockConfigManager,
                    patch(
                        "code_indexer.cli.EmbeddingProviderFactory"
                    ) as MockEmbeddingFactory,
                    patch("code_indexer.cli.BackendFactory") as MockBackendFactory,
                    patch(
                        "code_indexer.services.multi_index_query_service.MultiIndexQueryService"
                    ) as MockMultiIndexService,
                    patch(
                        "code_indexer.services.generic_query_service.GenericQueryService"
                    ) as MockGenericQueryService,
                    patch("code_indexer.services.language_validator.LanguageValidator"),
                    patch("code_indexer.services.language_mapper.LanguageMapper"),
                    patch(
                        "code_indexer.services.git_topology_service.GitTopologyService"
                    ) as MockGitTopology,
                ):

                    # Setup mocks
                    mock_config_manager = Mock()
                    mock_config.codebase_dir = Path(
                        mock_config.codebase_dir
                    )  # Ensure it's a Path
                    mock_config_manager.load.return_value = mock_config
                    MockConfigManager.return_value = mock_config_manager

                    mock_embedding_provider = Mock()
                    mock_embedding_provider.health_check.return_value = True
                    mock_embedding_provider.get_provider_name.return_value = "voyageai"
                    mock_embedding_provider.get_current_model.return_value = "voyage-3"
                    mock_embedding_provider.get_model_info.return_value = {
                        "name": "voyage-3"
                    }
                    MockEmbeddingFactory.create.return_value = mock_embedding_provider

                    mock_backend = Mock()
                    mock_vector_store = Mock()
                    mock_vector_store.health_check.return_value = True
                    mock_vector_store.resolve_collection_name.return_value = "default"
                    mock_vector_store._current_collection_name = "default"
                    mock_vector_store.ensure_payload_indexes.return_value = None
                    mock_backend.get_vector_store_client.return_value = (
                        mock_vector_store
                    )
                    MockBackendFactory.create.return_value = mock_backend

                    # Mock GitTopologyService to follow non-git path
                    mock_git_topology = Mock()
                    mock_git_topology.is_git_available.return_value = False
                    MockGitTopology.return_value = mock_git_topology

                    # Mock GenericQueryService
                    mock_query_service = Mock()
                    mock_query_service.get_current_branch_context.return_value = {
                        "project_id": "test_project"
                    }
                    mock_query_service.filter_results_by_current_branch.return_value = (
                        []
                    )
                    MockGenericQueryService.return_value = mock_query_service

                    # Mock MultiIndexQueryService - no multimodal index
                    mock_service_instance = Mock()
                    mock_service_instance.has_multimodal_index.return_value = False
                    mock_service_instance.query.return_value = [
                        {
                            "id": "result1",
                            "score": 0.9,
                            "payload": {
                                "path": "src/file.py",
                                "content": "code content",
                            },
                        }
                    ]
                    MockMultiIndexService.return_value = mock_service_instance

                    # Run query with context object
                    result = runner.invoke(
                        cli,
                        ["query", "test query", "--quiet"],
                        obj={"config_manager": mock_config_manager},
                        catch_exceptions=False,
                    )

                    # CRITICAL ASSERTIONS:
                    # 1. MultiIndexQueryService was instantiated (always used now)
                    MockMultiIndexService.assert_called_once()

                    # 2. Command succeeded
                    assert result.exit_code == 0
            finally:
                os.chdir(original_cwd)

    def test_multi_index_query_respects_filters(
        self, runner, project_with_multimodal_index, mock_config
    ):
        """
        Test that MultiIndexQueryService is instantiated when query has filters.

        Validates:
        1. MultiIndexQueryService is instantiated with filter parameters
        2. Service is initialized with correct parameters
        """
        mock_config.codebase_dir = str(project_with_multimodal_index)

        # Create a valid config.json for local mode detection
        config_file = project_with_multimodal_index / ".code-indexer" / "config.json"
        config_data = {
            "codebase_dir": str(project_with_multimodal_index),
            "embedding_provider": "voyageai",
            "backend": "filesystem",
        }
        config_file.write_text(json.dumps(config_data))

        with runner.isolated_filesystem(temp_dir=project_with_multimodal_index.parent):
            # Change to project directory for mode detection
            original_cwd = os.getcwd()
            try:
                os.chdir(str(project_with_multimodal_index))

                with (
                    patch("code_indexer.cli.ConfigManager") as MockConfigManager,
                    patch(
                        "code_indexer.cli.EmbeddingProviderFactory"
                    ) as MockEmbeddingFactory,
                    patch("code_indexer.cli.BackendFactory") as MockBackendFactory,
                    patch(
                        "code_indexer.services.multi_index_query_service.MultiIndexQueryService"
                    ) as MockMultiIndexService,
                    patch(
                        "code_indexer.services.generic_query_service.GenericQueryService"
                    ) as MockGenericQueryService,
                    patch(
                        "code_indexer.services.language_validator.LanguageValidator"
                    ) as MockLanguageValidator,
                    patch(
                        "code_indexer.services.language_mapper.LanguageMapper"
                    ) as MockLanguageMapper,
                    patch(
                        "code_indexer.services.git_topology_service.GitTopologyService"
                    ) as MockGitTopology,
                    patch(
                        "code_indexer.services.path_filter_builder.PathFilterBuilder"
                    ) as MockPathFilterBuilder,
                    patch(
                        "code_indexer.services.filter_conflict_detector.FilterConflictDetector"
                    ) as MockFilterConflictDetector,
                ):

                    # Setup mocks (similar to previous tests)
                    mock_config_manager = Mock()
                    mock_config.codebase_dir = Path(
                        mock_config.codebase_dir
                    )  # Ensure it's a Path
                    mock_config_manager.load.return_value = mock_config
                    MockConfigManager.return_value = mock_config_manager

                    mock_embedding_provider = Mock()
                    mock_embedding_provider.health_check.return_value = True
                    mock_embedding_provider.get_provider_name.return_value = "voyageai"
                    mock_embedding_provider.get_current_model.return_value = "voyage-3"
                    mock_embedding_provider.get_model_info.return_value = {
                        "name": "voyage-3"
                    }
                    MockEmbeddingFactory.create.return_value = mock_embedding_provider

                    mock_backend = Mock()
                    mock_vector_store = Mock()
                    mock_vector_store.health_check.return_value = True
                    mock_vector_store.resolve_collection_name.return_value = "default"
                    mock_vector_store._current_collection_name = "default"
                    mock_vector_store.ensure_payload_indexes.return_value = None
                    mock_backend.get_vector_store_client.return_value = (
                        mock_vector_store
                    )
                    MockBackendFactory.create.return_value = mock_backend

                    # Mock GitTopologyService to follow non-git path
                    mock_git_topology = Mock()
                    mock_git_topology.is_git_available.return_value = False
                    MockGitTopology.return_value = mock_git_topology

                    # Mock GenericQueryService
                    mock_query_service = Mock()
                    mock_query_service.get_current_branch_context.return_value = {
                        "project_id": "test_project"
                    }
                    mock_query_service.filter_results_by_current_branch.return_value = (
                        []
                    )
                    MockGenericQueryService.return_value = mock_query_service

                    # Mock LanguageValidator and LanguageMapper to return actual values
                    mock_lang_validator = Mock()
                    mock_lang_validator.validate_and_normalize.return_value = ["python"]
                    # Mock validate_language to return a validation result with required attributes
                    mock_validation_result = Mock()
                    mock_validation_result.is_valid = True
                    mock_validation_result.error_message = None
                    mock_validation_result.suggestions = []
                    mock_lang_validator.validate_language.return_value = (
                        mock_validation_result
                    )
                    MockLanguageValidator.return_value = mock_lang_validator

                    mock_lang_mapper = Mock()
                    mock_lang_mapper.map_to_extensions.return_value = [".py"]
                    mock_lang_mapper.get_extensions.return_value = [".py"]
                    # Mock build_language_filter to return actual dict (not Mock object)
                    mock_lang_mapper.build_language_filter.return_value = {
                        "key": "language",
                        "match": {"value": ".py"},
                    }
                    MockLanguageMapper.return_value = mock_lang_mapper

                    # Mock PathFilterBuilder to return actual dict values
                    mock_path_filter_builder = Mock()
                    mock_path_filter_builder.build_exclusion_filter.return_value = {
                        "must_not": [{"key": "path", "match": {"text": "*/tests/*"}}]
                    }
                    MockPathFilterBuilder.return_value = mock_path_filter_builder

                    # Mock FilterConflictDetector to return empty list (no conflicts)
                    mock_conflict_detector = Mock()
                    mock_conflict_detector.detect_conflicts.return_value = []
                    MockFilterConflictDetector.return_value = mock_conflict_detector

                    mock_service_instance = Mock()
                    mock_service_instance.has_multimodal_index.return_value = True
                    mock_service_instance.query.return_value = [
                        {
                            "id": "result1",
                            "score": 0.9,
                            "payload": {
                                "path": "tests/test_file.py",
                                "content": "test content",
                            },
                        }
                    ]
                    MockMultiIndexService.return_value = mock_service_instance

                    # Run query with filters and context object
                    result = runner.invoke(
                        cli,
                        [
                            "query",
                            "test query",
                            "--language",
                            "python",
                            "--path-filter",
                            "*/tests/*",
                            "--quiet",
                        ],
                        obj={"config_manager": mock_config_manager},
                        catch_exceptions=False,
                    )

                    # CRITICAL ASSERTIONS:
                    # 1. MultiIndexQueryService was instantiated
                    MockMultiIndexService.assert_called_once()

                    # 2. Command succeeded
                    assert result.exit_code == 0
            finally:
                os.chdir(original_cwd)
