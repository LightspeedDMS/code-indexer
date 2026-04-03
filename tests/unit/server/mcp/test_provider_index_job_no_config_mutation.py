"""Tests for _provider_index_job config mutation deletion (Story #620).

Verifies that:
- _provider_index_job does NOT read/write/restore config.json (mutation pattern deleted)
- bulk_add_provider_index permanently writes provider to embedding_providers list
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_repo(tmp_path: Path, initial_config: dict) -> Path:
    """Create a minimal repo directory with .code-indexer/config.json."""
    repo_dir = tmp_path / "golden-repos" / "my-repo"
    repo_dir.mkdir(parents=True)
    ci_dir = repo_dir / ".code-indexer"
    ci_dir.mkdir()
    (ci_dir / "config.json").write_text(json.dumps(initial_config))
    return repo_dir


def _mock_server_config(cohere_key="cohere-key", voyage_key=None) -> MagicMock:
    """Return a minimal mock server config with API keys."""
    cfg = MagicMock()
    cfg.cohere_api_key = cohere_key
    cfg.voyageai_api_key = voyage_key
    return cfg


class TestProviderIndexJobNoConfigMutation:
    """_provider_index_job must NOT mutate config.json."""

    def test_provider_index_job_does_not_write_embedding_provider_to_config(
        self, tmp_path
    ):
        """config.json must be byte-for-byte equal after _provider_index_job runs."""
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai", "cohere"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)
        config_file = repo_dir / ".code-indexer" / "config.json"

        from code_indexer.server.mcp.handlers import _provider_index_job

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()

            _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
            )

        result = json.loads(config_file.read_text())
        assert result == initial_config

    def test_provider_index_job_config_unchanged_on_success(self, tmp_path):
        """Config.json values remain exactly intact after a successful job run."""
        initial_config = {"embedding_provider": "voyage-ai", "sentinel": 99}
        repo_dir = _make_repo(tmp_path, initial_config)
        config_file = repo_dir / ".code-indexer" / "config.json"

        from code_indexer.server.mcp.handlers import _provider_index_job

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(0, 0),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()

            _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
            )

        result = json.loads(config_file.read_text())
        assert result == initial_config


class TestBulkAddProviderIndexPermanentWrite:
    """bulk_add_provider_index must permanently append provider to embedding_providers."""

    def test_bulk_add_writes_provider_to_embedding_providers(self, tmp_path):
        """bulk_add_provider_index permanently adds provider to embedding_providers in config.json."""
        initial_config = {"embedding_provider": "voyage-ai"}
        repo_dir = _make_repo(tmp_path, initial_config)
        config_file = repo_dir / ".code-indexer" / "config.json"

        from code_indexer.server.mcp.handlers import _append_provider_to_config

        _append_provider_to_config(str(repo_dir), "cohere")

        result = json.loads(config_file.read_text())
        assert "cohere" in result["embedding_providers"]

    def test_bulk_add_is_idempotent_no_duplicates(self, tmp_path):
        """Calling _append_provider_to_config twice does not add duplicates."""
        initial_config = {"embedding_provider": "voyage-ai"}
        repo_dir = _make_repo(tmp_path, initial_config)
        config_file = repo_dir / ".code-indexer" / "config.json"

        from code_indexer.server.mcp.handlers import _append_provider_to_config

        _append_provider_to_config(str(repo_dir), "cohere")
        _append_provider_to_config(str(repo_dir), "cohere")

        result = json.loads(config_file.read_text())
        assert result["embedding_providers"].count("cohere") == 1
