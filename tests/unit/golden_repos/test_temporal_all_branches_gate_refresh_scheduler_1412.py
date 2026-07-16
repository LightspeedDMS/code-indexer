"""
Unit tests for Story #1412 - defense-in-depth #2: RefreshScheduler's
_index_source temporal command builder (refresh_scheduler.py ~line 2437)
must NOT append --all-branches when the server-wide
temporal_all_branches_enabled gate is off, even if a golden repo's stored
temporal_options.all_branches is True. A WARNING must be logged recording
the gate-driven downgrade to single-branch.

Reuses the _make_scheduler/_make_registry/_capture_subprocess_cmds helper
pattern from test_refresh_scheduler_temporal.py.
"""

import logging
from unittest.mock import MagicMock, patch


def _make_scheduler(registry):
    """Bug #1414: _index_source now reads temporal_options from
    self.golden_repo_metadata (golden_repos_metadata table), not from the
    registry. These tests model the "at registration both tables agree" /
    unedited-repo case, so mirror the registry's stored temporal_options
    into a golden_repo_metadata double here -- this keeps every test's
    values and assertions unchanged while exercising the new read path.
    """
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = object.__new__(RefreshScheduler)
    scheduler.registry = registry
    repo_info = registry.get_global_repo.return_value
    golden_meta = MagicMock()
    golden_meta.get_repo.return_value = {
        "temporal_options": repo_info.get("temporal_options") if repo_info else None
    }
    scheduler.golden_repo_metadata = golden_meta
    return scheduler


def _make_registry(
    alias_name, temporal_options, repo_url="git@github.com:org/repo.git"
):
    registry = MagicMock()
    repo_info = {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "enable_temporal": True,
        "temporal_options": temporal_options,
        "enable_scip": False,
    }
    registry.get_global_repo.return_value = repo_info
    return registry


def _make_gate_config(enabled: bool):
    mock_svc = MagicMock()
    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = enabled
    mock_server_cfg = MagicMock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_svc.get_config.return_value = mock_server_cfg
    return mock_svc


def _capture_subprocess_cmds(scheduler, alias_name, source_path, gate_enabled):
    captured = []

    def recording_run(cmd, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result

    def recording_popen_progress(
        command,
        phase_name,
        allocator,
        progress_callback,
        all_stdout,
        all_stderr,
        cwd,
        error_label=None,
        env=None,
    ):
        captured.append(list(command))

    import code_indexer.services.progress_subprocess_runner as psr_mod

    with (
        patch("subprocess.run", side_effect=recording_run),
        patch.object(
            psr_mod, "run_with_popen_progress", side_effect=recording_popen_progress
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=_make_gate_config(gate_enabled),
        ),
    ):
        scheduler._index_source(alias_name, str(source_path))

    return captured


class TestRefreshSchedulerTemporalAllBranchesGateOff:
    """Defense-in-depth: gate off must skip --all-branches + log WARNING."""

    def test_gate_off_stored_all_branches_true_omits_flag(self, tmp_path):
        alias = "my-repo-global"
        registry = _make_registry(alias, temporal_options={"all_branches": True})
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path, gate_enabled=False)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        assert "--all-branches" not in temporal_cmds[0], (
            f"Gate off must omit '--all-branches' even with stored "
            f"all_branches=True. Got: {temporal_cmds[0]}"
        )

    def test_gate_off_stored_all_branches_true_logs_warning(self, tmp_path, caplog):
        alias = "my-repo-global"
        registry = _make_registry(alias, temporal_options={"all_branches": True})
        scheduler = _make_scheduler(registry)

        with caplog.at_level(logging.WARNING):
            _capture_subprocess_cmds(scheduler, alias, tmp_path, gate_enabled=False)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            alias in r.getMessage() and "all_branches" in r.getMessage()
            for r in warnings
        ), (
            f"Expected a WARNING naming the repo and all_branches. Got: {[r.getMessage() for r in warnings]}"
        )

    def test_gate_on_stored_all_branches_true_includes_flag(self, tmp_path):
        alias = "my-repo-global"
        registry = _make_registry(alias, temporal_options={"all_branches": True})
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path, gate_enabled=True)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        assert "--all-branches" in temporal_cmds[0], (
            f"Gate on + stored all_branches=True must produce "
            f"'--all-branches'. Got: {temporal_cmds[0]}"
        )
