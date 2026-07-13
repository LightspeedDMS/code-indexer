"""Tests for multi-provider loop in cidx index (Story #620).

Tests verify that:
- get_embedding_providers() returns single-item list for backward compat repos
- resolve_api_key() correctly gates which providers get indexed
- providers missing API keys are skipped
- a secondary provider whose authenticating health check fails (invalid key) is
  skipped so the already-complete primary index is not hung at ~99% (EVO-64222)
"""

import os
from unittest.mock import MagicMock, patch


class TestMultiProviderIndexGating:
    """Test that provider loop only indexes providers with valid API keys."""

    def test_single_provider_config_gives_one_provider(self):
        """Repos with only embedding_provider (singular) give single-item provider list."""
        from code_indexer.config import Config

        config = Config(embedding_provider="voyage-ai", embedding_providers=None)
        providers = config.get_embedding_providers()
        assert providers == ["voyage-ai"]
        assert len(providers) == 1

    def test_multi_provider_config_gives_full_list(self):
        """Repos with embedding_providers list give the full list."""
        from code_indexer.config import Config

        config = Config(embedding_providers=["voyage-ai", "cohere"])
        providers = config.get_embedding_providers()
        assert providers == ["voyage-ai", "cohere"]
        assert len(providers) == 2

    def test_resolve_api_key_gates_voyage_ai_indexing(self):
        """Only voyage-ai with VOYAGE_API_KEY set proceeds to indexing."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "v-key"}, clear=False):
            key = EmbeddingProviderFactory.resolve_api_key("voyage-ai")
        assert key == "v-key"

    def test_resolve_api_key_gates_cohere_indexing(self):
        """Only cohere with CO_API_KEY set proceeds to indexing."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {"CO_API_KEY": "c-key"}, clear=False):
            key = EmbeddingProviderFactory.resolve_api_key("cohere")
        assert key == "c-key"

    def test_missing_api_key_blocks_provider(self):
        """Provider with missing API key is blocked from indexing (returns None)."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {}, clear=True):
            key = EmbeddingProviderFactory.resolve_api_key("cohere")
        assert key is None

    def test_provider_loop_skips_providers_without_api_key(self):
        """Provider loop skips providers where resolve_api_key() returns None."""
        from code_indexer.config import Config
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = Config(embedding_providers=["voyage-ai", "cohere"])
        providers = config.get_embedding_providers()

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "v-key"}, clear=True):
            indexable = [
                p
                for p in providers
                if EmbeddingProviderFactory.resolve_api_key(p) is not None
            ]

        assert "cohere" not in indexable
        assert "voyage-ai" in indexable

    def test_backward_compat_single_provider_repo_no_embedding_providers_key(self):
        """Repos without embedding_providers key fall back gracefully to single provider."""
        from code_indexer.config import Config

        config = Config(embedding_provider="voyage-ai")
        assert config.embedding_providers is None
        providers = config.get_embedding_providers()
        assert providers == ["voyage-ai"]

    def test_skip_warning_logged_for_missing_api_key(self, caplog):
        """Warning is logged when a provider is skipped due to missing API key."""
        import logging
        from code_indexer.cli import _log_skipped_provider_warning

        with caplog.at_level(logging.WARNING, logger="code_indexer.cli"):
            with patch.dict(os.environ, {}, clear=True):
                _log_skipped_provider_warning("cohere")

        assert any("cohere" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestSecondaryProviderInvalidKeySkipped:
    """EVO-64222: an additional (secondary) provider whose authenticating
    health check fails — e.g. an invalid Cohere key that returns 401 — must be
    SKIPPED at the pre-check so the already-complete primary index finishes
    instead of hanging at ~99% while the doomed secondary pass retries 401s."""

    def _make_two_provider_index_mocks(self, primary_stats):
        """Build the patch context managers to drive cli `index` through the
        additional-providers loop with a voyage-ai primary (healthy) and a
        cohere secondary whose health_check(test_api=True) returns False.

        Returns (patches_tuple, handles_dict) so the test can both enter the
        patches and assert against the underlying mocks.
        """
        mock_config = MagicMock()
        mock_config.daemon = None  # disables daemon-delegation branch
        mock_config.embedding_provider = "voyage-ai"
        mock_config.codebase_dir = "/fake/codebase"
        mock_config.get_embedding_providers.return_value = ["voyage-ai", "cohere"]
        mock_config.vector_store = None

        mock_config_manager = MagicMock()
        mock_config_manager.load.return_value = mock_config

        # Primary provider: shallow health_check() passes.
        mock_primary = MagicMock()
        mock_primary.health_check.return_value = True
        mock_primary.get_provider_name.return_value = "voyage-ai"

        # Secondary provider: authenticating health_check(test_api=True) fails
        # (simulates an invalid Cohere key returning 401 on a real probe).
        mock_secondary = MagicMock()
        mock_secondary.health_check.return_value = False
        mock_secondary.get_provider_name.return_value = "cohere"

        mock_backend = MagicMock()
        mock_backend.health_check.return_value = True
        mock_backend.get_vector_store_client.return_value = MagicMock()

        mock_smart_indexer = MagicMock()
        mock_smart_indexer.smart_index.return_value = primary_stats
        mock_smart_indexer.get_indexing_status.return_value = {
            "status": "completed",
            "can_resume": False,
            "files_processed": primary_stats.files_processed,
            "chunks_indexed": 0,
        }
        mock_smart_indexer.get_git_status.return_value = {
            "git_available": False,
            "project_id": "fake-project",
        }
        mock_smart_indexer.slot_tracker = None

        create_patch = patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            side_effect=[mock_primary, mock_secondary],
        )
        patches = (
            # Satisfy the @require_mode("local") gate on `index` — CliRunner runs
            # in a cwd without an initialized project, which would otherwise fail
            # the command before the multi-provider loop is reached.
            patch(
                "code_indexer.disabled_commands.detect_current_mode",
                return_value="local",
            ),
            patch(
                "code_indexer.cli.ConfigManager.create_with_backtrack",
                return_value=mock_config_manager,
            ),
            create_patch,
            patch(
                "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
                return_value="fake-key",
            ),
            patch(
                "code_indexer.cli.BackendFactory.create",
                return_value=mock_backend,
            ),
            patch(
                "code_indexer.services.smart_indexer.SmartIndexer",
                return_value=mock_smart_indexer,
            ),
        )
        handles = {
            "primary": mock_primary,
            "secondary": mock_secondary,
            "smart_indexer": mock_smart_indexer,
        }
        return patches, handles

    def test_invalid_secondary_key_is_skipped_and_index_completes(self):
        """Functional: `cidx index` with a healthy voyage-ai primary and a
        cohere secondary whose authenticating health check fails must exit 0
        (primary index intact), probe the secondary with test_api=True, and
        never run the secondary's smart_index (it is skipped, not hung)."""
        from click.testing import CliRunner

        from code_indexer.cli import cli
        from code_indexer.indexing.processor import ProcessingStats

        stats = ProcessingStats(files_processed=5, failed_files=0, cancelled=False)
        patches, handles = self._make_two_provider_index_mocks(stats)
        runner = CliRunner()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
        ):
            result = runner.invoke(cli, ["index"])

        # Primary index completed — no hang, clean exit.
        assert result.exit_code == 0, (
            f"cidx index exited {result.exit_code} (expected 0). An invalid "
            "secondary key must be skipped, leaving the primary index usable. "
            f"Output:\n{result.output}"
        )
        # The secondary was gated with an authenticating probe (EVO-64222 fix).
        handles["secondary"].health_check.assert_called_once_with(test_api=True)
        # Only the primary was actually indexed — the secondary was skipped,
        # so smart_index ran exactly once (never for the doomed secondary).
        assert handles["smart_indexer"].smart_index.call_count == 1, (
            "smart_index ran more than once — the invalid secondary provider "
            "was NOT skipped before its doomed indexing pass."
        )
