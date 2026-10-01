"""A cancelled local clear must not report a successful full rebuild."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli


@pytest.mark.parametrize("early_cancel", [True, False])
@pytest.mark.parametrize("clear", [True, False])
def test_local_cancel_exit_code(tmp_path, early_cancel, clear):
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir()
    config = MagicMock()
    config.codebase_dir = str(tmp_path)
    config.embedding_provider = "voyage-ai"
    config.daemon = None
    config.vector_store = None
    config.voyage_ai.parallel_requests = 1
    config.get_embedding_providers.return_value = ["voyage-ai"]

    indexer = MagicMock()
    indexer.get_git_status.return_value = {
        "git_available": False,
        "project_id": "example-project",
    }
    indexer.get_indexing_status.return_value = {
        "status": "in_progress",
        "can_resume": False,
    }
    indexer.slot_tracker = None
    indexer.smart_index.return_value = (
        None
        if early_cancel
        else SimpleNamespace(
            cancelled=True,
            files_processed=1,
            chunks_created=1,
            failed_files=0,
            duration=1.0,
        )
    )

    provider = MagicMock()
    provider.health_check.return_value = True
    provider.get_provider_name.return_value = "voyage-ai"
    backend = MagicMock()
    backend.health_check.return_value = True
    backend.get_vector_store_client.return_value.resolve_collection_name.return_value = "example-text"

    with (
        patch("code_indexer.cli.ConfigManager") as config_manager,
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
            return_value="example-key",
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create", return_value=provider
        ),
        patch("code_indexer.cli.BackendFactory.create", return_value=backend),
        patch("code_indexer.services.smart_indexer.SmartIndexer", return_value=indexer),
        patch(
            "code_indexer.services.chunk_migration_cli.acquire_index_mutation_lock",
            return_value=nullcontext(),
        ),
        patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
        patch(
            "code_indexer.progress.multi_threaded_display.MultiThreadedProgressManager"
        ),
        patch(
            "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
            return_value=[],
        ),
    ):
        config_manager.create_with_backtrack.return_value.load.return_value = config
        config_manager.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )
        args = ["index", "--clear"] if clear else ["index"]
        result = CliRunner().invoke(cli, args)

    indexer.smart_index.assert_called_once()
    assert "cancelled" in result.output
    assert result.exit_code == (1 if clear else 0), result.output
