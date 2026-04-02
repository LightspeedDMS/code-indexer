"""
Tests for Bug #599: CohereConfig missing performance tuning fields.

Verifies:
1. CohereConfig has parallel_requests (default 8)
2. CohereConfig has batch_size (default 96)
3. CohereConfig has max_concurrent_batches_per_commit (default 10)
4. CohereConfig has exponential_backoff (default True)
5. api_key_seeding sets voyageai_seeded=True when VoyageAI key is present
"""

from unittest.mock import MagicMock, patch


class TestCohereConfigFields:
    """CohereConfig must have performance tuning fields matching VoyageAIConfig."""

    def test_cohere_config_has_parallel_requests(self):
        """CohereConfig.parallel_requests defaults to 8."""
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        assert config.parallel_requests == 8

    def test_cohere_config_has_batch_size(self):
        """CohereConfig.batch_size defaults to 96."""
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        assert config.batch_size == 96

    def test_cohere_config_has_max_concurrent_batches_per_commit(self):
        """CohereConfig.max_concurrent_batches_per_commit defaults to 10."""
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        assert config.max_concurrent_batches_per_commit == 10

    def test_cohere_config_has_exponential_backoff(self):
        """CohereConfig.exponential_backoff defaults to True."""
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        assert config.exponential_backoff is True

    def test_cohere_config_fields_are_overridable(self):
        """CohereConfig performance fields can be overridden."""
        from code_indexer.config import CohereConfig

        config = CohereConfig(
            parallel_requests=4,
            batch_size=48,
            max_concurrent_batches_per_commit=5,
            exponential_backoff=False,
        )
        assert config.parallel_requests == 4
        assert config.batch_size == 48
        assert config.max_concurrent_batches_per_commit == 5
        assert config.exponential_backoff is False


class TestApiKeySeeding:
    """api_key_seeding must set voyageai_seeded=True when VoyageAI key is seeded."""

    def test_voyageai_seeded_true_when_key_present(self):
        """voyageai_seeded must be True when VoyageAI API key is configured."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        mock_config_service = MagicMock()
        mock_config = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_config.claude_integration_config.anthropic_api_key = ""
        mock_config.claude_integration_config.voyageai_api_key = "test-voyage-key"
        mock_config.claude_integration_config.cohere_api_key = ""

        with patch("os.environ", {}):
            result = seed_api_keys_on_startup(mock_config_service)

        assert result["voyageai_seeded"] is True, (
            "voyageai_seeded should be True when VoyageAI key is seeded"
        )

    def test_voyageai_seeded_false_when_no_key(self):
        """voyageai_seeded must be False when no VoyageAI API key is configured."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        mock_config_service = MagicMock()
        mock_config = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_config.claude_integration_config.anthropic_api_key = ""
        mock_config.claude_integration_config.voyageai_api_key = ""
        mock_config.claude_integration_config.cohere_api_key = ""

        with patch("os.environ", {}):
            result = seed_api_keys_on_startup(mock_config_service)

        assert result["voyageai_seeded"] is False

    def test_cohere_seeded_true_when_key_present(self):
        """cohere_seeded must be True when Cohere API key is configured (existing behavior)."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        mock_config_service = MagicMock()
        mock_config = MagicMock()
        mock_config_service.get_config.return_value = mock_config
        mock_config.claude_integration_config.anthropic_api_key = ""
        mock_config.claude_integration_config.voyageai_api_key = ""
        mock_config.claude_integration_config.cohere_api_key = "test-cohere-key"

        with patch("os.environ", {}):
            result = seed_api_keys_on_startup(mock_config_service)

        assert result["cohere_seeded"] is True
