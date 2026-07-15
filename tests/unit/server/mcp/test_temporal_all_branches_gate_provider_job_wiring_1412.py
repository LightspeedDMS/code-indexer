"""Unit tests for Story #1412 - _provider_temporal_index_job must read the
temporal_all_branches_enabled gate from get_config_service() and pass it
through to _build_temporal_index_cmd, so a gate-off server config downgrades
a stored all_branches=True request to single-branch indexing.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_repo(tmp_path: Path, initial_config: dict) -> Path:
    repo_dir = tmp_path / "golden-repos" / "my-repo"
    repo_dir.mkdir(parents=True)
    ci_dir = repo_dir / ".code-indexer"
    ci_dir.mkdir()
    (ci_dir / "config.json").write_text(json.dumps(initial_config))
    return repo_dir


def _make_gate_config(enabled: bool):
    cfg = MagicMock()
    cfg.voyageai_api_key = "voyage-key"
    cfg.cohere_api_key = None
    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = enabled
    cfg.indexing_config = mock_indexing
    return cfg


class TestProviderTemporalIndexJobGateWiring:
    """AC5/Story #1412: gate-off server config must downgrade the command."""

    def test_gate_off_config_omits_all_branches_flag(self, tmp_path) -> None:
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        captured_cmds = []

        def fake_run_popen(command, **kwargs):
            captured_cmds.append(command)

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=fake_run_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _make_gate_config(False)

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                temporal_options={"all_branches": True},
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--all-branches" not in cmd, (
            f"Gate-off config must omit '--all-branches'. Got: {cmd}"
        )

    def test_gate_on_config_includes_all_branches_flag(self, tmp_path) -> None:
        """
        Proves real config-to-command wiring (not just the fail-closed
        default): a gate-ON server config must cause --all-branches to be
        included when temporal_options requests it.
        """
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        captured_cmds = []

        def fake_run_popen(command, **kwargs):
            captured_cmds.append(command)

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=fake_run_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _make_gate_config(True)

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                temporal_options={"all_branches": True},
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--all-branches" in cmd, (
            f"Gate-on config must include '--all-branches'. Got: {cmd}"
        )
