"""Unit tests for Story #1404 - _provider_temporal_index_job must read the
global temporal indexing floor date from get_config_service() and pass it
through to _build_temporal_index_cmd (global_floor_date=...), so a
configured floor date bounds a real provider temporal rebuild subprocess
launch (Scenario 3/4 launch-site wiring). Mirrors
test_temporal_all_branches_gate_provider_job_wiring_1412.py's exact
structure.
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


def _make_config(floor_date):
    cfg = MagicMock()
    cfg.voyageai_api_key = "voyage-key"
    cfg.cohere_api_key = None
    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = False
    cfg.indexing_config = mock_indexing
    mock_temporal_indexing = MagicMock()
    mock_temporal_indexing.index_floor_date = floor_date
    cfg.temporal_indexing_config = mock_temporal_indexing
    return cfg


class TestProviderTemporalIndexJobFloorDateWiring:
    def test_configured_floor_date_appears_in_command(self, tmp_path) -> None:
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
            mock_cfg_svc.return_value.get_config.return_value = _make_config(
                "2025-01-01"
            )

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                temporal_options={},
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--since-date" in cmd, f"Expected --since-date in command. Got: {cmd}"
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"

    def test_unset_floor_date_omits_flag(self, tmp_path) -> None:
        """Scenario 5: unset floor = full-history no-op."""
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
            mock_cfg_svc.return_value.get_config.return_value = _make_config(None)

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                temporal_options={},
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--since-date" not in cmd, f"Expected no --since-date. Got: {cmd}"

    def test_per_repo_since_date_more_restrictive_than_global_wins(
        self, tmp_path
    ) -> None:
        """Scenario 6: 'more restrictive wins' -- exactly one --since-date."""
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
            mock_cfg_svc.return_value.get_config.return_value = _make_config(
                "2024-01-01"
            )

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                temporal_options={"since_date": "2025-06-01"},
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"
