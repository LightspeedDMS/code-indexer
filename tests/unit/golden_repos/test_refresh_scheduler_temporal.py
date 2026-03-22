"""
Unit tests for Story #478 AC5: RefreshScheduler temporal command must apply all_branches.

The scheduler's _index_source method builds a temporal command from stored
temporal_options. Before Story #478, all_branches was missing from the command
even when configured. These tests verify that the real _index_source code
applies all stored temporal options including all_branches.

Testing approach: call the real _index_source method with minimal mocking.
Only subprocess.run is mocked (prevents actual child processes), and the
registry is mocked to supply deterministic repo_info data.
"""

from unittest.mock import MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(registry):
    """
    Build a RefreshScheduler with enough state to call _index_source.

    RefreshScheduler.__init__ requires several injected dependencies. We
    supply only what _index_source actually uses: self.registry.
    """
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = object.__new__(RefreshScheduler)
    scheduler.registry = registry
    return scheduler


def _make_registry(alias_name, enable_temporal=True, temporal_options=None, repo_url="git@github.com:org/repo.git"):
    """Build a mock registry that returns the given repo_info for get_global_repo."""
    registry = MagicMock()
    repo_info = {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "enable_temporal": enable_temporal,
        "temporal_options": temporal_options,
        "enable_scip": False,
    }
    registry.get_global_repo.return_value = repo_info
    return registry


def _capture_subprocess_cmds(scheduler, alias_name, source_path):
    """
    Call _index_source and capture all subprocess.run commands.

    Returns list of command lists that were passed to subprocess.run.
    """
    captured = []

    def recording_run(cmd, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=recording_run):
        scheduler._index_source(alias_name, str(source_path))

    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRefreshSchedulerTemporalAllBranches:
    """
    AC5: RefreshScheduler must include --all-branches when all_branches=True.

    The _index_source method reads temporal_options from the registry and builds
    the cidx index --index-commits command. Before Story #478 fix, all_branches
    was never applied. These tests verify the fix is present and correct.
    """

    def test_all_branches_true_produces_flag(self, tmp_path):
        """
        AC5: all_branches=True in temporal_options produces --all-branches.

        This calls the real _index_source method on a real RefreshScheduler
        instance (with mocked registry and subprocess only).
        """
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"all_branches": True}
        )
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, (
            f"AC5: No temporal command issued. All commands: {cmds}"
        )
        temporal_cmd = temporal_cmds[0]
        assert "--all-branches" in temporal_cmd, (
            f"AC5: all_branches=True must produce '--all-branches'. Got: {temporal_cmd}"
        )

    def test_all_branches_false_omits_flag(self, tmp_path):
        """AC5: all_branches=False must NOT produce --all-branches."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"all_branches": False}
        )
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--all-branches" not in temporal_cmd, (
            f"AC5: all_branches=False must NOT produce '--all-branches'. Got: {temporal_cmd}"
        )

    def test_scheduler_temporal_no_clear_flag(self, tmp_path):
        """
        AC5: Scheduled refreshes must NOT use --clear.

        Only admin-triggered rebuilds use --clear. Scheduled refreshes are
        incremental to avoid re-indexing all commits every cycle.
        """
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True)
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--clear" not in temporal_cmd, (
            f"AC5: scheduled refresh must NOT include '--clear'. Got: {temporal_cmd}"
        )

    def test_scheduler_applies_max_commits(self, tmp_path):
        """AC5: max_commits from temporal_options is applied by the scheduler."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"max_commits": 200}
        )
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd, f"Missing --max-commits. Got: {temporal_cmd}"
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "200"

    def test_scheduler_applies_diff_context(self, tmp_path):
        """AC5: diff_context from temporal_options is applied by the scheduler."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"diff_context": 3}
        )
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--diff-context" in temporal_cmd, f"Missing --diff-context. Got: {temporal_cmd}"
        idx = temporal_cmd.index("--diff-context")
        assert temporal_cmd[idx + 1] == "3"

    def test_scheduler_diff_context_zero_not_dropped(self, tmp_path):
        """
        Bug fix: diff_context=0 must NOT be silently dropped.

        Zero is a valid value meaning minimal storage (no surrounding context lines).
        The old implementation used a falsy check `if temporal_options.get("diff_context"):`
        which evaluates False for 0, causing the flag to be omitted entirely.
        The fix uses `if diff_context is not None:` which correctly includes 0.
        """
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"diff_context": 0}
        )
        scheduler = _make_scheduler(registry)

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--diff-context" in temporal_cmd, (
            f"Bug: diff_context=0 must produce '--diff-context 0', not be silently dropped. "
            f"Got: {temporal_cmd}"
        )
        idx = temporal_cmd.index("--diff-context")
        assert temporal_cmd[idx + 1] == "0", (
            f"diff_context=0 must produce '--diff-context 0'. Got value: {temporal_cmd[idx + 1]}"
        )
