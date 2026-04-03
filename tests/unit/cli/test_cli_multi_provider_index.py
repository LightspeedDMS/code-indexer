"""Unit tests for the CLI multi-provider index loop (Story #620).

Verifies:
1. config has embedding_providers: ["voyage-ai", "cohere"], both API keys set ->
   both providers indexed
2. config has both, only VOYAGE_API_KEY set -> only voyage-ai indexed, cohere
   skipped with warning
3. config has only embedding_provider: "voyage-ai" (no embedding_providers key) ->
   backward compat path works normally
"""

import contextlib
from unittest.mock import MagicMock, patch


def _make_config(
    codebase_dir: str,
    embedding_provider: str = "voyage-ai",
    embedding_providers=None,
    voyage_parallel: int = 8,
    cohere_parallel: int = 4,
) -> MagicMock:
    """Create a minimal mock Config object that mimics code_indexer.config.Config."""
    cfg = MagicMock()
    cfg.codebase_dir = codebase_dir
    cfg.embedding_provider = embedding_provider
    cfg.embedding_providers = embedding_providers

    cfg.voyage_ai = MagicMock()
    cfg.voyage_ai.parallel_requests = voyage_parallel

    cfg.cohere = MagicMock()
    cfg.cohere.parallel_requests = cohere_parallel

    cfg.vector_store = None
    cfg.daemon = None  # Disable daemon mode so the standalone index path (with multi-provider loop) runs

    def _get_embedding_providers():
        if embedding_providers is not None:
            return list(embedding_providers)
        return [cfg.embedding_provider]

    cfg.get_embedding_providers = _get_embedding_providers
    return cfg


def _provider_aware_create(config, console):
    """Provider-aware factory: returns a mock with correct provider identity."""
    provider_name = getattr(config, "embedding_provider", "voyage-ai")
    model = "voyage-3" if provider_name == "voyage-ai" else "embed-v4.0"
    mock = MagicMock()
    mock.health_check.return_value = True
    mock.get_provider_name.return_value = provider_name
    mock.get_current_model.return_value = model
    mock.get_model_info.return_value = {}
    return mock


@contextlib.contextmanager
def _index_test_env(tmp_path, cfg, resolve_api_key_side_effect, mock_indexer):
    """Context manager providing all standard patches for cidx index tests.

    Yields the CliRunner so tests can invoke the CLI and inspect output.
    """
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "metadata.json").write_text("{}")

    with (
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
            side_effect=resolve_api_key_side_effect,
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            side_effect=_provider_aware_create,
        ),
        patch(
            "code_indexer.cli.BackendFactory.create",
            return_value=MagicMock(
                health_check=lambda: True,
                get_vector_store_client=lambda: MagicMock(),
            ),
        ),
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            return_value=mock_indexer,
        ),
        patch("code_indexer.cli.ConfigManager") as mock_cm,
        patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
        patch(
            "code_indexer.progress.multi_threaded_display.MultiThreadedProgressManager"
        ),
    ):
        # The CLI group uses ConfigManager.create_with_backtrack() to create the instance,
        # so the instance is mock_cm.create_with_backtrack.return_value.
        mock_cm.create_with_backtrack.return_value.load.return_value = cfg
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )

        from click.testing import CliRunner

        yield CliRunner()


def _make_indexer(files: int = 5, chunks: int = 20) -> MagicMock:
    """Return a SmartIndexer mock with sensible defaults."""
    stats = MagicMock()
    stats.duration = 1.0
    stats.files_processed = files
    stats.chunks_created = chunks
    stats.failed_files = 0
    stats.cancelled = False  # Explicit False prevents MagicMock truthy detection

    indexer = MagicMock()
    indexer.smart_index.return_value = stats
    indexer.get_git_status.return_value = {
        "git_available": False,
        "project_id": "test-proj",
    }
    indexer.get_indexing_status.return_value = {
        "status": "completed",
        "can_resume": False,
        "files_processed": 0,
        "chunks_indexed": 0,
    }
    indexer.slot_tracker = None
    return indexer


class TestCliMultiProviderIndex:
    """Tests for the multi-provider loop in cidx index."""

    def test_index_loops_multiple_providers(self, tmp_path):
        """When embedding_providers has voyage-ai and cohere and both API keys are set,
        smart_index is called twice — once per provider."""
        cfg = _make_config(
            codebase_dir=str(tmp_path),
            embedding_provider="voyage-ai",
            embedding_providers=["voyage-ai", "cohere"],
        )
        mock_indexer = _make_indexer()

        with _index_test_env(
            tmp_path,
            cfg,
            resolve_api_key_side_effect=lambda p: "key-123",
            mock_indexer=mock_indexer,
        ) as runner:
            from code_indexer.cli import cli

            result = runner.invoke(cli, ["index"])

        assert mock_indexer.smart_index.call_count >= 2, (
            f"Expected smart_index called at least twice (one per provider), "
            f"got {mock_indexer.smart_index.call_count} call(s).\n{result.output}"
        )

    def test_index_skips_provider_without_api_key(self, tmp_path, caplog):
        """When embedding_providers has voyage-ai and cohere but only VOYAGE_API_KEY
        is set, only voyage-ai is indexed and a warning is logged for cohere."""
        import logging

        cfg = _make_config(
            codebase_dir=str(tmp_path),
            embedding_provider="voyage-ai",
            embedding_providers=["voyage-ai", "cohere"],
        )
        mock_indexer = _make_indexer()

        def _api_key_for_provider(provider_name: str):
            return "voyage-key" if provider_name == "voyage-ai" else None

        with caplog.at_level(logging.WARNING, logger="code_indexer.cli"):
            with _index_test_env(
                tmp_path,
                cfg,
                resolve_api_key_side_effect=_api_key_for_provider,
                mock_indexer=mock_indexer,
            ) as runner:
                from code_indexer.cli import cli

                runner.invoke(cli, ["index"])

        assert mock_indexer.smart_index.call_count == 1, (
            f"Expected smart_index called exactly once (only voyage-ai; cohere skipped), "
            f"got {mock_indexer.smart_index.call_count} call(s)."
        )

        skip_messages = [
            r.message
            for r in caplog.records
            if "cohere" in r.message.lower() and "skip" in r.message.lower()
        ]
        assert skip_messages, (
            "Expected a warning log containing 'skip' and 'cohere'. "
            f"Captured log records: {[(r.levelname, r.message) for r in caplog.records]}"
        )

    def test_index_backward_compat_singular_provider(self, tmp_path):
        """When config has only embedding_provider (no embedding_providers list),
        the backward compat path works and only one provider is indexed."""
        cfg = _make_config(
            codebase_dir=str(tmp_path),
            embedding_provider="voyage-ai",
            embedding_providers=None,  # singular provider — no list
        )
        mock_indexer = _make_indexer()

        with _index_test_env(
            tmp_path,
            cfg,
            resolve_api_key_side_effect=lambda p: "voyage-key",
            mock_indexer=mock_indexer,
        ) as runner:
            from code_indexer.cli import cli

            result = runner.invoke(cli, ["index"])

        assert mock_indexer.smart_index.call_count == 1, (
            f"Expected smart_index called exactly once (backward compat singular provider), "
            f"got {mock_indexer.smart_index.call_count} call(s).\n{result.output}"
        )
