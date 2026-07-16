"""
Bug #1414: golden repo temporal_options split-brain -- Web UI edits never
reach the scheduled-refresh command builder.

Root cause: RefreshScheduler._index_source read `temporal_options` from
self.registry.get_global_repo(alias_name) -- the `global_repos` table, which
is frozen at registration time. The Web UI's ONLY write path
(GoldenRepoManager.save_temporal_options) writes exclusively to
`golden_repos_metadata` (self.golden_repo_metadata). The two tables agree
only at registration; any later Web UI edit is silently ignored by every
scheduled refresh, forever.

The most dangerous instance (the #1406-class scenario) is `all_branches`:
if the server-wide temporal_all_branches_enabled gate is ON and an operator
uses the Web UI to explicitly DISABLE all_branches on a repo, the stale
registry copy (still True) makes the scheduler keep doing multi-branch
indexing against explicit operator intent, invisibly, forever.

These tests call the REAL _index_source method (only subprocess.run and
run_with_popen_progress are mocked, matching the established pattern in
test_refresh_scheduler_temporal.py) with `registry` seeded with STALE
registration-time values and `golden_repo_metadata` seeded with EDITED
values, and assert the built command reflects the EDITED values.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(registry, golden_meta):
    """Build a RefreshScheduler with enough state to call _index_source,
    with BOTH the registry (global_repos) and golden_repo_metadata
    (golden_repos_metadata) backends explicitly injected."""
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
    """Build a mock registry (global_repos) -- may hold STALE
    temporal_options, mirroring registration-time values never updated
    since."""
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
    """Build a mock golden_repo_metadata backend (golden_repos_metadata) --
    the Web UI's sole write target, holding the CURRENT/EDITED value."""
    golden_meta = MagicMock()
    golden_meta.get_repo.return_value = {"temporal_options": temporal_options}
    return golden_meta


def _capture_subprocess_cmds(scheduler, alias_name, source_path, gate_enabled=None):
    """Call _index_source and capture all commands issued to subprocess.run
    and run_with_popen_progress."""
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

    patches = [
        patch("subprocess.run", side_effect=recording_run),
        patch.object(
            psr_mod, "run_with_popen_progress", side_effect=recording_popen_progress
        ),
    ]
    if gate_enabled is not None:
        mock_indexing = MagicMock()
        mock_indexing.temporal_all_branches_enabled = gate_enabled
        mock_server_cfg = MagicMock()
        mock_server_cfg.indexing_config = mock_indexing
        mock_cfg_svc = MagicMock()
        mock_cfg_svc.get_config.return_value = mock_server_cfg
        patches.append(
            patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service",
                return_value=mock_cfg_svc,
            )
        )

    with patches[0], patches[1]:
        if len(patches) == 3:
            with patches[2]:
                scheduler._index_source(alias_name, str(source_path))
        else:
            scheduler._index_source(alias_name, str(source_path))

    return captured


# ---------------------------------------------------------------------------
# Tests: edited golden_repos_metadata values must win over stale registry
# ---------------------------------------------------------------------------


class TestTemporalOptionsReadFromGoldenRepoMetadataNotStaleRegistry:
    def test_edited_max_commits_reflected_stale_registry_ignored(self, tmp_path):
        """Web UI edited max_commits from 5 (stale, registry) to 200
        (current, golden_repos_metadata) -- the built command must use 200."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"max_commits": 5}
        )
        golden_meta = _make_golden_meta({"max_commits": 200})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "200", (
            f"Bug #1414: must use EDITED golden_repos_metadata value (200), "
            f"not stale registry value (5). Got: {temporal_cmd}"
        )

    def test_edited_since_date_reflected_stale_registry_ignored(self, tmp_path):
        alias = "my-repo-global"
        registry = _make_registry(
            alias,
            enable_temporal=True,
            temporal_options={"since_date": "2020-01-01"},
        )
        golden_meta = _make_golden_meta({"since_date": "2024-06-01"})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--since-date" in temporal_cmd
        idx = temporal_cmd.index("--since-date")
        assert temporal_cmd[idx + 1] == "2024-06-01", (
            f"Bug #1414: must use EDITED golden_repos_metadata since_date, "
            f"not stale registry value. Got: {temporal_cmd}"
        )

    def test_edited_diff_context_reflected_stale_registry_ignored(self, tmp_path):
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"diff_context": 1}
        )
        golden_meta = _make_golden_meta({"diff_context": 9})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--diff-context" in temporal_cmd
        idx = temporal_cmd.index("--diff-context")
        assert temporal_cmd[idx + 1] == "9", (
            f"Bug #1414: must use EDITED golden_repos_metadata diff_context "
            f"(9), not stale registry value (1). Got: {temporal_cmd}"
        )

    def test_gate_on_all_branches_disabled_via_ui_stale_registry_true_omits_flag(
        self, tmp_path
    ):
        """The #1406-class scenario: server gate is ON, operator explicitly
        DISABLES all_branches via the Web UI (golden_repos_metadata now
        False), but global_repos still holds the stale registration-time
        True. The scheduled refresh must honor the operator's disable and
        NOT append --all-branches."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"all_branches": True}
        )
        golden_meta = _make_golden_meta({"all_branches": False})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path, gate_enabled=True)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--all-branches" not in temporal_cmd, (
            f"Bug #1414 (#1406-class): operator explicitly disabled "
            f"all_branches via Web UI (golden_repos_metadata=False); the "
            f"stale registry copy (True) must NOT override that. "
            f"Got: {temporal_cmd}"
        )

    def test_gate_on_all_branches_enabled_via_ui_stale_registry_false_includes_flag(
        self, tmp_path
    ):
        """Symmetric case: operator ENABLES all_branches via the Web UI
        after registration (golden_repos_metadata=True), while the stale
        registry copy still holds the registration-time False. The
        scheduled refresh must honor the operator's enable."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"all_branches": False}
        )
        golden_meta = _make_golden_meta({"all_branches": True})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path, gate_enabled=True)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--all-branches" in temporal_cmd, (
            f"Bug #1414: operator explicitly ENABLED all_branches via Web "
            f"UI (golden_repos_metadata=True); the stale registry copy "
            f"(False) must NOT override that. Got: {temporal_cmd}"
        )


# ---------------------------------------------------------------------------
# Tests: Bug #642 NULL fallback preserved; enable_temporal/enable_scip
# remain registry-sourced (unchanged, out of scope for this bug).
# ---------------------------------------------------------------------------


class TestBug642FallbackAndRegistryFieldsUnaffected:
    def test_golden_meta_temporal_options_none_falls_back_to_meta_json(self, tmp_path):
        """Bug #642 NULL fallback must still apply when the AUTHORITATIVE
        (golden_repos_metadata) temporal_options is None -- not the
        registry's."""
        import json

        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"max_commits": 999}
        )
        golden_meta = _make_golden_meta(None)
        scheduler = _make_scheduler(registry, golden_meta)

        meta_dir = tmp_path / ".code-indexer" / "index" / "code-indexer-temporal"
        meta_dir.mkdir(parents=True)
        meta = {"last_commit": "abc", "total_commits": 42, "indexed_at": "2024-01-01"}
        (meta_dir / "temporal_meta.json").write_text(json.dumps(meta))

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "42", (
            f"Bug #642 fallback must fire off the AUTHORITATIVE (golden_repos_"
            f"metadata) None, reading temporal_meta.json's total_commits=42 -- "
            f"not the registry's stale max_commits=999. Got: {temporal_cmd}"
        )

    def test_enable_temporal_still_sourced_from_registry(self, tmp_path):
        """enable_temporal is explicitly OUT OF SCOPE for Bug #1414 -- it
        must remain registry-sourced, unchanged. A repo with
        enable_temporal=False in the registry must NOT produce a temporal
        command at all, even if golden_repos_metadata holds temporal
        options."""
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=False)
        golden_meta = _make_golden_meta({"max_commits": 50})
        scheduler = _make_scheduler(registry, golden_meta)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert not temporal_cmds, (
            f"enable_temporal=False in the registry must suppress temporal "
            f"indexing entirely (unchanged by Bug #1414). Got commands: {cmds}"
        )
