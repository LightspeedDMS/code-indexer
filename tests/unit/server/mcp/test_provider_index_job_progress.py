"""Tests for _provider_index_job real progress reporting (Story #613).

Verifies that _provider_index_job uses run_with_popen_progress instead of
subprocess.run, enabling real progress callback forwarding, proper env injection,
and timeout enforcement.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestProviderIndexJobProgress:
    """_provider_index_job must forward real progress via run_with_popen_progress."""

    def _make_repo(self, tmp_path: Path) -> Path:
        """Create a minimal non-versioned repo directory structure."""
        repo_dir = tmp_path / "golden-repos" / "my-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".code-indexer").mkdir()
        config = {"embedding_provider": "cohere"}
        (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(config))
        return repo_dir

    def _mock_popen_proc(self):
        """Return a mock Popen process that exits successfully with no output."""
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr.readlines.return_value = []
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None
        mock_proc.poll.return_value = 0
        return mock_proc

    def test_progress_callback_receives_intermediate_values(self, tmp_path):
        """run_with_popen_progress must forward JSON progress lines to progress_callback."""
        repo_dir = self._make_repo(tmp_path)

        progress_calls = []

        def progress_callback(pct, phase=None, detail=None):
            progress_calls.append(pct)

        # Simulate two JSON progress lines from cidx index --progress-json
        progress_lines = [
            json.dumps({"current": 1, "total": 10, "info": "step 1"}) + "\n",
            json.dumps({"current": 5, "total": 10, "info": "step 5"}) + "\n",
            json.dumps({"current": 10, "total": 10, "info": "done"}) + "\n",
        ]

        mock_proc = MagicMock()
        mock_proc.stdout = iter(progress_lines)
        mock_proc.stderr.readlines.return_value = []
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None
        mock_proc.poll.return_value = 0

        from code_indexer.server.mcp.handlers import _provider_index_job

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
                progress_callback=progress_callback,
            )

        assert result["success"] is True
        # Must have received intermediate progress values (more than just start)
        assert len(progress_calls) >= 2, (
            f"Expected at least 2 progress calls, got {len(progress_calls)}: {progress_calls}"
        )
        # Progress values must be monotonically non-decreasing
        for i in range(1, len(progress_calls)):
            assert progress_calls[i] >= progress_calls[i - 1], (
                f"Progress regressed: {progress_calls}"
            )

    def test_env_with_co_api_key_reaches_popen(self, tmp_path):
        """CO_API_KEY from server config must be passed via env to run_with_popen_progress."""
        repo_dir = self._make_repo(tmp_path)

        mock_proc = self._mock_popen_proc()
        captured_kwargs = {}

        from code_indexer.server.mcp.handlers import _provider_index_job

        def fake_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(5, 3),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="my-cohere-key-12345", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True
        assert "env" in captured_kwargs, "env must be passed to Popen"
        env = captured_kwargs["env"]
        assert "CO_API_KEY" in env, f"CO_API_KEY missing from Popen env: {env}"
        assert env["CO_API_KEY"] == "my-cohere-key-12345"

    def test_timeout_passed_to_run_with_popen_progress(self, tmp_path):
        """_PROVIDER_INDEX_TIMEOUT_SECONDS must be forwarded to run_with_popen_progress."""
        repo_dir = self._make_repo(tmp_path)

        mock_proc = self._mock_popen_proc()

        from code_indexer.server.mcp.handlers import (
            _PROVIDER_INDEX_TIMEOUT_SECONDS,
            _provider_index_job,
        )

        captured_call_args = {}

        original_run_with_popen = None

        def capture_run_with_popen(*args, **kwargs):
            captured_call_args.update(kwargs)
            # Call the real function but with a mocked Popen
            return original_run_with_popen(*args, **kwargs)

        import code_indexer.services.progress_subprocess_runner as runner_module

        original_run_with_popen = runner_module.run_with_popen_progress

        with (
            patch.object(
                runner_module,
                "run_with_popen_progress",
                side_effect=capture_run_with_popen,
            ),
            patch.object(runner_module, "gather_repo_metrics", return_value=(5, 3)),
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True
        assert "timeout" in captured_call_args, (
            "timeout must be passed to run_with_popen_progress"
        )
        assert captured_call_args["timeout"] == _PROVIDER_INDEX_TIMEOUT_SECONDS, (
            f"Expected timeout={_PROVIDER_INDEX_TIMEOUT_SECONDS}, "
            f"got {captured_call_args['timeout']}"
        )
        assert _PROVIDER_INDEX_TIMEOUT_SECONDS == 3600

    def test_non_git_repo_degrades_gracefully(self, tmp_path):
        """When gather_repo_metrics returns (0,0) for a non-git repo, job completes."""
        repo_dir = self._make_repo(tmp_path)

        mock_proc = self._mock_popen_proc()

        from code_indexer.server.mcp.handlers import _provider_index_job

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            # Non-git repo: returns (0, 0)
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(0, 0),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            # Must not raise - should complete successfully
            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True, (
            f"Expected success=True for non-git repo, got: {result}"
        )

    def test_progress_callback_is_none_by_default(self, tmp_path):
        """When progress_callback is not provided, job runs without crashing."""
        repo_dir = self._make_repo(tmp_path)

        mock_proc = self._mock_popen_proc()

        from code_indexer.server.mcp.handlers import _provider_index_job

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            # No progress_callback passed - must not crash
            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True

    def test_indexing_subprocess_error_returns_failure(self, tmp_path):
        """When run_with_popen_progress raises IndexingSubprocessError, return failure."""
        repo_dir = self._make_repo(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
        )

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(5, 3),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch(
                "code_indexer.server.mcp.handlers._post_provider_index_snapshot"
            ) as mock_snapshot,
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            # Patch run_with_popen_progress at the handlers import level
            import code_indexer.services.progress_subprocess_runner as runner_module

            with patch.object(
                runner_module,
                "run_with_popen_progress",
                side_effect=IndexingSubprocessError("Failed to provider index: error!"),
            ):
                result = _provider_index_job(
                    repo_path=str(repo_dir),
                    provider_name="cohere",
                    clear=False,
                )

        assert result["success"] is False, f"Expected failure, got: {result}"
        assert "error" in result.get("stderr", "").lower() or result.get("stderr"), (
            f"Expected error message in stderr: {result}"
        )
        mock_snapshot.assert_not_called()

    def test_clear_flag_passes_clear_to_cmd(self, tmp_path):
        """When clear=True, --clear must appear in the command passed to Popen."""
        repo_dir = self._make_repo(tmp_path)

        mock_proc = self._mock_popen_proc()
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return mock_proc

        from code_indexer.server.mcp.handlers import _provider_index_job

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(5, 3),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=True,
            )

        assert result["success"] is True
        assert "--clear" in captured_cmd, (
            f"--clear must be in command when clear=True: {captured_cmd}"
        )
        assert "--progress-json" in captured_cmd, (
            f"--progress-json must always be in command: {captured_cmd}"
        )
