"""Unit tests for Story #1412 - defense-in-depth #1: GoldenRepoManager's
add_index_to_golden_repo temporal command builder (golden_repo_manager.py
~line 3546) must NOT append --all-branches when the server-wide
temporal_all_branches_enabled gate is off, even if a golden repo's stored
temporal_options.all_branches is True (legacy value, or gate flipped off
after the option was set). A WARNING must be logged recording the
gate-driven downgrade to single-branch.

Reuses the registered_manager/repo_path fixture pattern and
_capture_temporal_commands helper style from test_add_index_commands.py.
"""

import logging
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _make_success_popen(collected_cmds):
    class MockProcess:
        def __init__(self, cmd, **kwargs):
            collected_cmds.append(list(cmd))
            self.stdout = StringIO("")
            self.stderr = StringIO("")
            self.returncode = 0

        def wait(self):
            pass

    return MockProcess


@pytest.fixture
def manager(tmp_path):
    return GoldenRepoManager(data_dir=str(tmp_path))


@pytest.fixture
def repo_path(tmp_path):
    p = tmp_path / "repos" / "test-repo"
    p.mkdir(parents=True)
    return p


@pytest.fixture
def registered_manager(manager, repo_path):
    from datetime import datetime, timezone
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    created_at = datetime.now(timezone.utc).isoformat()
    repo = GoldenRepo(
        alias="test-repo",
        repo_url="git@github.com:org/test-repo.git",
        clone_path=str(repo_path),
        default_branch="main",
        created_at=created_at,
    )
    manager.golden_repos["test-repo"] = repo
    manager._sqlite_backend.add_repo(
        alias="test-repo",
        repo_url="git@github.com:org/test-repo.git",
        default_branch="main",
        clone_path=str(repo_path),
        created_at=created_at,
        enable_temporal=False,
        temporal_options=None,
    )
    manager.background_job_manager = MagicMock()
    return manager


def _make_gate_config(enabled: bool):
    mock_svc = MagicMock()
    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = enabled
    mock_server_cfg = MagicMock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_svc.get_config.return_value = mock_server_cfg
    return mock_svc


def _capture_temporal_commands(
    registered_manager, repo_path, temporal_options, gate_enabled
):
    registered_manager.save_temporal_options("test-repo", temporal_options)
    captured_cmds = []

    def fake_submit_job(operation_type, func, **kwargs):
        collected = []

        def recording_run(cmd, **kw):
            collected.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = "ok"
            r.stderr = ""
            return r

        with (
            patch("subprocess.run", side_effect=recording_run),
            patch("subprocess.Popen", side_effect=_make_success_popen(collected)),
            patch.object(
                registered_manager,
                "get_actual_repo_path",
                return_value=str(repo_path),
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=_make_gate_config(gate_enabled),
            ),
        ):
            func()
        captured_cmds.extend(collected)
        return "fake-job-id"

    with patch.object(
        registered_manager.background_job_manager,
        "submit_job",
        side_effect=fake_submit_job,
    ):
        registered_manager.add_index_to_golden_repo(
            alias="test-repo",
            index_type="temporal",
        )

    return [c for c in captured_cmds if c[:2] == ["cidx", "index"]]


class TestAddIndexTemporalAllBranchesGateOff:
    """Defense-in-depth: gate off must skip --all-branches + log WARNING."""

    def test_gate_off_stored_all_branches_true_omits_flag(
        self, registered_manager, repo_path
    ):
        cmds = _capture_temporal_commands(
            registered_manager,
            repo_path,
            temporal_options={"all_branches": True},
            gate_enabled=False,
        )
        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]
        assert "--all-branches" not in temporal_cmd, (
            f"Gate off must omit '--all-branches' even with stored "
            f"all_branches=True. Got: {temporal_cmd}"
        )

    def test_gate_off_stored_all_branches_true_logs_warning(
        self, registered_manager, repo_path, caplog
    ):
        with caplog.at_level(logging.WARNING):
            _capture_temporal_commands(
                registered_manager,
                repo_path,
                temporal_options={"all_branches": True},
                gate_enabled=False,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "test-repo" in r.getMessage() and "all_branches" in r.getMessage()
            for r in warnings
        ), (
            f"Expected a WARNING naming the repo and all_branches. Got: {[r.getMessage() for r in warnings]}"
        )
