"""Tests for Story #1404 launch-site wiring in golden_repo_manager.py:

  - Site 1: GoldenRepoManager._execute_post_clone_workflow (registration)
  - Site 2: GoldenRepoManager.add_indexes_to_golden_repo

Both must thread the resolved global temporal indexing floor date into the
constructed `cidx index --index-commits` command, composed with the
pre-existing per-repo temporal_options["since_date"] as "more restrictive
wins" via resolve_effective_floor_date -- exactly one --since-date flag is
ever emitted, never two, and it is omitted entirely when both are unset
(Scenario 5 no-op preserved).

Mirrors test_golden_repo_manager_temporal_pg_env_wiring_1313.py (site 1) and
test_golden_repo_manager_add_indexes_temporal_pg_env_wiring_1313.py (site 2)
mocking patterns exactly, capturing the `command=` kwarg passed to
run_with_popen_progress instead of `env=`.
"""

from __future__ import annotations

from unittest.mock import Mock, MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    TemporalIndexingConfig,
)


def _server_config(floor_date):
    return ServerConfig(
        server_dir="/opt/cidx-server",
        temporal_indexing_config=TemporalIndexingConfig(index_floor_date=floor_date),
    )


def _mock_subprocess_run(command, **kwargs):
    return MagicMock(returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Site 1: _execute_post_clone_workflow
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_repo_manager(tmp_path):
    return GoldenRepoManager(data_dir=str(tmp_path))


@pytest.fixture
def mock_clone_path(tmp_path):
    clone_path = tmp_path / "test-repo"
    clone_path.mkdir()
    return clone_path


class TestExecutePostCloneWorkflowFloorDateWiring:
    def _run_workflow(
        self, mock_repo_manager, mock_clone_path, floor_date, temporal_options
    ):
        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "command": command})
            return 100

        with (
            patch("subprocess.run", side_effect=_mock_subprocess_run),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = _server_config(
                floor_date
            )
            mock_repo_manager._execute_post_clone_workflow(
                clone_path=str(mock_clone_path),
                force_init=False,
                enable_temporal=True,
                temporal_options=temporal_options,
            )

        by_phase = {c["phase_name"]: c["command"] for c in calls}
        return by_phase["temporal"]

    def test_global_floor_date_applied_no_per_repo_override(
        self, mock_repo_manager, mock_clone_path
    ) -> None:
        cmd = self._run_workflow(mock_repo_manager, mock_clone_path, "2025-01-01", None)
        assert "--since-date" in cmd
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"

    def test_unset_floor_date_omits_flag(
        self, mock_repo_manager, mock_clone_path
    ) -> None:
        cmd = self._run_workflow(mock_repo_manager, mock_clone_path, None, None)
        assert "--since-date" not in cmd

    def test_per_repo_more_restrictive_than_global_wins(
        self, mock_repo_manager, mock_clone_path
    ) -> None:
        cmd = self._run_workflow(
            mock_repo_manager,
            mock_clone_path,
            "2024-01-01",
            {"since_date": "2025-06-01"},
        )
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"

    def test_global_more_restrictive_than_per_repo_wins(
        self, mock_repo_manager, mock_clone_path
    ) -> None:
        cmd = self._run_workflow(
            mock_repo_manager,
            mock_clone_path,
            "2025-06-01",
            {"since_date": "2024-01-01"},
        )
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"


# ---------------------------------------------------------------------------
# Site 2: add_indexes_to_golden_repo
# ---------------------------------------------------------------------------


def _make_manager(tmp_path, temporal_options=None):
    """Build a minimal GoldenRepoManager with a real repo_path on disk."""
    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    repo_path = tmp_path / "golden-repos" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    manager.data_dir = str(tmp_path)
    manager.golden_repos_dir = str(tmp_path / "golden-repos")

    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = str(repo_path)
    golden_repo.temporal_options = temporal_options or {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value=str(repo_path))
    manager._sqlite_backend = Mock()
    manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
    manager._sqlite_backend.get_repo = Mock(
        return_value={
            "alias": golden_repo.alias,
            "repo_url": "https://example.com/test-repo.git",
            "default_branch": "main",
            "clone_path": golden_repo.clone_path,
            "created_at": "2026-01-01T00:00:00+00:00",
            "enable_temporal": golden_repo.enable_temporal,
            "temporal_options": golden_repo.temporal_options,
            "category_id": None,
            "category_auto_assigned": False,
        }
    )
    manager._global_repos_backend = Mock()

    captured_workers = []

    def capture_and_run(operation_type, func, submitter_username, **kwargs):
        captured_workers.append(func)
        return "job-add-indexes-test"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = capture_and_run
    manager._captured_workers = captured_workers
    manager._refresh_scheduler = None

    return manager, repo_path


def _run_captured_worker(manager) -> None:
    assert len(manager._captured_workers) == 1
    manager._captured_workers[0]()


class TestAddIndexesToGoldenRepoFloorDateWiring:
    def _run_add_indexes(self, tmp_path, floor_date, per_repo_since_date):
        temporal_options = (
            {"since_date": per_repo_since_date} if per_repo_since_date else {}
        )
        manager, repo_path = _make_manager(tmp_path, temporal_options)

        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "command": command})
            return 100

        with (
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=_mock_subprocess_run,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_cfg_svc,
        ):
            mock_get_cfg_svc.return_value.get_config.return_value = _server_config(
                floor_date
            )
            manager.add_indexes_to_golden_repo(
                alias="test-repo", index_types=["temporal"]
            )
            _run_captured_worker(manager)

        by_phase = {c["phase_name"]: c["command"] for c in calls}
        assert "temporal" in by_phase, f"expected a temporal-phase call, got: {calls}"
        return by_phase["temporal"]

    def test_global_floor_date_applied_no_per_repo_override(self, tmp_path) -> None:
        cmd = self._run_add_indexes(tmp_path, "2025-01-01", None)
        assert "--since-date" in cmd
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"

    def test_unset_floor_date_omits_flag(self, tmp_path) -> None:
        cmd = self._run_add_indexes(tmp_path, None, None)
        assert "--since-date" not in cmd

    def test_per_repo_more_restrictive_than_global_wins(self, tmp_path) -> None:
        cmd = self._run_add_indexes(tmp_path, "2024-01-01", "2025-06-01")
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"

    def test_global_more_restrictive_than_per_repo_wins(self, tmp_path) -> None:
        cmd = self._run_add_indexes(tmp_path, "2025-06-01", "2024-01-01")
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
