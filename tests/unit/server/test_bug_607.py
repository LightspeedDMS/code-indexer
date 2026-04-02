"""Tests for Bug #607: _provider_index_job used --provider flag that doesn't exist in cidx index."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers import _provider_index_job

_TIMEOUT_SECS = 3600


def _make_repo(tmp_path: Path, provider: str = "voyage-ai") -> Path:
    """Create a minimal fake repo with .code-indexer/config.json."""
    code_indexer_dir = tmp_path / ".code-indexer"
    code_indexer_dir.mkdir()
    config = {"embedding_provider": provider, "other_key": "preserved"}
    (code_indexer_dir / "config.json").write_text(json.dumps(config))
    return tmp_path


def _make_config_service(
    voyageai_key: str = "vk-test", cohere_key: str = "ck-test"
) -> MagicMock:
    """Create a mock config service with the expected attribute structure."""
    mock_config = MagicMock()
    mock_config.voyageai_api_key = voyageai_key
    mock_config.cohere_api_key = cohere_key
    mock_service = MagicMock()
    mock_service.get_config.return_value = mock_config
    return mock_service


def _successful_run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")


def _failing_run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="failed"
    )


class TestProviderIndexJobConfigMutation:
    """Tests that _provider_index_job correctly mutates and restores config.json."""

    def test_sets_embedding_provider_before_running_cidx(self, tmp_path):
        """Verify config.json has the target provider set while cidx runs."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")
        config_path = repo_path / ".code-indexer" / "config.json"

        observed_provider_during_run = []

        def capture_provider(*args, **kwargs):
            config = json.loads(config_path.read_text())
            observed_provider_during_run.append(config.get("embedding_provider"))
            return _successful_run(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=capture_provider),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            _provider_index_job(str(repo_path), "cohere")

        assert observed_provider_during_run == ["cohere"], (
            "embedding_provider must be set to target provider while cidx runs"
        )

    def test_restores_original_provider_after_success(self, tmp_path):
        """Verify config.json is restored to original provider after successful run, preserving other fields."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")
        config_path = repo_path / ".code-indexer" / "config.json"

        with (
            patch("subprocess.run", side_effect=_successful_run),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            result = _provider_index_job(str(repo_path), "cohere")

        assert result["success"] is True
        restored_config = json.loads(config_path.read_text())
        assert restored_config["embedding_provider"] == "voyage-ai", (
            "embedding_provider must be restored to original value after success"
        )
        assert restored_config["other_key"] == "preserved", (
            "Other config keys must be preserved after restore"
        )

    def test_restores_original_provider_after_failure(self, tmp_path):
        """Verify config.json is restored to original provider even when cidx fails."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")
        config_path = repo_path / ".code-indexer" / "config.json"

        with (
            patch("subprocess.run", side_effect=_failing_run),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            result = _provider_index_job(str(repo_path), "cohere")

        assert result["success"] is False
        restored_config = json.loads(config_path.read_text())
        assert restored_config["embedding_provider"] == "voyage-ai", (
            "embedding_provider must be restored to original value even after failure"
        )

    def test_restores_original_provider_after_timeout_exception(self, tmp_path):
        """Verify config.json is restored after TimeoutExpired and timeout message returned in stderr."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")
        config_path = repo_path / ".code-indexer" / "config.json"

        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["cidx"], timeout=_TIMEOUT_SECS)

        with (
            patch("subprocess.run", side_effect=raise_timeout),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            result = _provider_index_job(str(repo_path), "cohere")

        assert result["success"] is False
        assert "timed out" in result["stderr"].lower(), (
            "Timeout result must contain 'timed out' in stderr"
        )
        restored_config = json.loads(config_path.read_text())
        assert restored_config["embedding_provider"] == "voyage-ai", (
            "embedding_provider must be restored after TimeoutExpired"
        )


class TestProviderIndexJobEnvVars:
    """Tests that _provider_index_job passes correct API key env vars to subprocess."""

    def test_passes_co_api_key_for_cohere(self, tmp_path):
        """Verify CO_API_KEY is set in the subprocess env when provider is 'cohere'."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")

        captured_env = {}

        def capture_env(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _successful_run(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=capture_env),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(cohere_key="ck-my-cohere-key"),
            ),
        ):
            _provider_index_job(str(repo_path), "cohere")

        assert "CO_API_KEY" in captured_env, (
            "CO_API_KEY must be set in subprocess env for cohere"
        )
        assert captured_env["CO_API_KEY"] == "ck-my-cohere-key"

    def test_passes_voyage_api_key_for_voyage_ai(self, tmp_path):
        """Verify VOYAGE_API_KEY is set in the subprocess env when provider is 'voyage-ai'."""
        repo_path = _make_repo(tmp_path, provider="cohere")

        captured_env = {}

        def capture_env(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _successful_run(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=capture_env),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(voyageai_key="vk-my-voyage-key"),
            ),
        ):
            _provider_index_job(str(repo_path), "voyage-ai")

        assert "VOYAGE_API_KEY" in captured_env, (
            "VOYAGE_API_KEY must be set in subprocess env for voyage-ai"
        )
        assert captured_env["VOYAGE_API_KEY"] == "vk-my-voyage-key"

    def test_base_command_is_cidx_index_without_provider_flag(self, tmp_path):
        """Verify base command is exactly ['cidx', 'index'] with no --provider flag (Bug #607 regression guard)."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")

        captured_cmd = []

        def capture_cmd(*args, **kwargs):
            captured_cmd.extend(args[0])
            return _successful_run(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=capture_cmd),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            _provider_index_job(str(repo_path), "cohere")

        assert captured_cmd[:2] == [
            "cidx",
            "index",
        ], "Base command must be exactly ['cidx', 'index'] in order"
        assert "--provider" not in captured_cmd, (
            "cidx index must NOT be called with --provider flag (it does not exist)"
        )

    def test_clear_flag_passed_when_requested(self, tmp_path):
        """Verify --clear is appended to cidx index when clear=True."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")

        captured_cmd = []

        def capture_cmd(*args, **kwargs):
            captured_cmd.extend(args[0])
            return _successful_run(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=capture_cmd),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            _provider_index_job(str(repo_path), "cohere", clear=True)

        assert "--clear" in captured_cmd, "--clear must be passed when clear=True"
