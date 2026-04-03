"""Tests for Bug #607: _provider_index_job used --provider flag that doesn't exist in cidx index.

Story #613: subprocess.run replaced with run_with_popen_progress (uses Popen internally).
Mocks must target code_indexer.services.progress_subprocess_runner.subprocess.Popen
and gather_repo_metrics, NOT subprocess.run.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers import _provider_index_job
from code_indexer.services.progress_subprocess_runner import IndexingSubprocessError

_TIMEOUT_SECS = 3600

_POPEN_PATH = "code_indexer.services.progress_subprocess_runner.subprocess.Popen"
_GATHER_METRICS_PATH = (
    "code_indexer.services.progress_subprocess_runner.gather_repo_metrics"
)
_RUN_WITH_POPEN_PATH = (
    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
)


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


def _mock_popen_proc(returncode: int = 0, stderr_lines=None) -> MagicMock:
    """Return a mock Popen process that exits with the given returncode."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter([])
    mock_proc.stderr.readlines.return_value = stderr_lines or []
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = None
    mock_proc.poll.return_value = returncode
    return mock_proc


class TestProviderIndexJobConfigMutation:
    """Tests that _provider_index_job correctly mutates and restores config.json."""

    def test_sets_embedding_provider_before_running_cidx(self, tmp_path):
        """Verify config.json has the target provider set while cidx runs."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")
        config_path = repo_path / ".code-indexer" / "config.json"

        observed_provider_during_run = []

        def capture_provider(cmd, **kwargs):
            config = json.loads(config_path.read_text())
            observed_provider_during_run.append(config.get("embedding_provider"))
            return _mock_popen_proc()

        with (
            patch(_POPEN_PATH, side_effect=capture_provider),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
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
            patch(_POPEN_PATH, return_value=_mock_popen_proc()),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
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
            patch(
                _POPEN_PATH,
                return_value=_mock_popen_proc(returncode=1, stderr_lines=["failed"]),
            ),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
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
        """Verify config.json is restored after timeout and timeout message returned in stderr."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")
        config_path = repo_path / ".code-indexer" / "config.json"

        def raise_timeout(*args, **kwargs):
            raise IndexingSubprocessError(
                f"Failed to provider index: Timed out after {_TIMEOUT_SECS}s"
            )

        with (
            patch(_RUN_WITH_POPEN_PATH, side_effect=raise_timeout),
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
            "embedding_provider must be restored after timeout"
        )


class TestProviderIndexJobEnvVars:
    """Tests that _provider_index_job passes correct API key env vars to subprocess."""

    def test_passes_co_api_key_for_cohere(self, tmp_path):
        """Verify CO_API_KEY is set in the subprocess env when provider is 'cohere'."""
        repo_path = _make_repo(tmp_path, provider="voyage-ai")

        captured_env = {}

        def capture_env(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _mock_popen_proc()

        with (
            patch(_POPEN_PATH, side_effect=capture_env),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
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

        def capture_env(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return _mock_popen_proc()

        with (
            patch(_POPEN_PATH, side_effect=capture_env),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
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

        def capture_cmd(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return _mock_popen_proc()

        with (
            patch(_POPEN_PATH, side_effect=capture_cmd),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
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

        def capture_cmd(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return _mock_popen_proc()

        with (
            patch(_POPEN_PATH, side_effect=capture_cmd),
            patch(_GATHER_METRICS_PATH, return_value=(10, 5)),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=_make_config_service(),
            ),
        ):
            _provider_index_job(str(repo_path), "cohere", clear=True)

        assert "--clear" in captured_cmd, "--clear must be passed when clear=True"
