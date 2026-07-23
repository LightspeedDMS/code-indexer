"""Tests for Story #1404 launch-site wiring in
global_repos/refresh_scheduler.py::_index_source (launch site 4 of the 4
corrected sites -- named explicitly per the spec-corrections section, NOT
under server/).

Must thread the resolved global temporal indexing floor date into the
constructed `cidx index --index-commits` command, composed with the
pre-existing per-repo temporal_options["since_date"] (read from
golden_repos_metadata, per Bug #1414's fix) as "more restrictive wins" --
exactly one --since-date flag is ever emitted, never two, and it is
omitted entirely when both are unset (Scenario 5 no-op preserved).

Mirrors test_refresh_scheduler_temporal_options_split_brain_1414.py's exact
mocking pattern (registry + golden_repo_metadata backends, subprocess.run /
run_with_popen_progress capture).
"""

from unittest.mock import MagicMock, patch


def _make_scheduler(registry, golden_meta):
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = object.__new__(RefreshScheduler)
    scheduler.registry = registry
    scheduler.golden_repo_metadata = golden_meta
    return scheduler


def _make_registry(
    alias_name,
    enable_temporal=True,
    temporal_options=None,
    repo_url="git@github.com:org/repo.git",
    enable_scip=False,
):
    registry = MagicMock()
    repo_info = {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "enable_temporal": enable_temporal,
        "temporal_options": temporal_options,
        "enable_scip": enable_scip,
    }
    registry.get_global_repo.return_value = repo_info
    return registry


def _make_golden_meta(temporal_options):
    golden_meta = MagicMock()
    golden_meta.get_repo.return_value = {"temporal_options": temporal_options}
    return golden_meta


def _capture_subprocess_cmds(scheduler, alias_name, source_path, floor_date=None):
    """Call _index_source and capture all commands issued to subprocess.run
    and run_with_popen_progress, with a mocked get_config_service returning
    the given global temporal indexing floor date."""
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

    mock_indexing = MagicMock()
    mock_indexing.temporal_all_branches_enabled = False
    mock_temporal_indexing = MagicMock()
    mock_temporal_indexing.index_floor_date = floor_date
    mock_server_cfg = MagicMock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_server_cfg.temporal_indexing_config = mock_temporal_indexing
    mock_cfg_svc = MagicMock()
    mock_cfg_svc.get_config.return_value = mock_server_cfg

    with (
        patch("subprocess.run", side_effect=recording_run),
        patch.object(
            psr_mod, "run_with_popen_progress", side_effect=recording_popen_progress
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=mock_cfg_svc,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_cfg_svc,
        ),
    ):
        scheduler._index_source(alias_name, str(source_path))

    return captured


class TestIndexSourceFloorDateWiring:
    def test_global_floor_date_applied_no_per_repo_override(self, tmp_path) -> None:
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True, temporal_options={})
        golden_meta = _make_golden_meta({})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(
            scheduler, alias, tmp_path, floor_date="2025-01-01"
        )

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        cmd = temporal_cmds[0]
        assert "--since-date" in cmd
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"

    def test_unset_floor_date_omits_flag(self, tmp_path) -> None:
        """Scenario 5: unset floor = full-history no-op."""
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True, temporal_options={})
        golden_meta = _make_golden_meta({})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path, floor_date=None)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        cmd = temporal_cmds[0]
        assert "--since-date" not in cmd

    def test_per_repo_more_restrictive_than_global_wins(self, tmp_path) -> None:
        """Scenario 6: 'more restrictive wins' -- exactly one --since-date."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"since_date": "2020-01-01"}
        )
        golden_meta = _make_golden_meta({"since_date": "2025-06-01"})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(
            scheduler, alias, tmp_path, floor_date="2024-01-01"
        )

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        cmd = temporal_cmds[0]
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"

    def test_global_more_restrictive_than_per_repo_wins(self, tmp_path) -> None:
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"since_date": "2024-01-01"}
        )
        golden_meta = _make_golden_meta({"since_date": "2024-01-01"})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(
            scheduler, alias, tmp_path, floor_date="2025-06-01"
        )

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        cmd = temporal_cmds[0]
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"
