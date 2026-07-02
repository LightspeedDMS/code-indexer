"""Tests for Bug #642 Step 2: RefreshScheduler max_commits fallback from temporal_meta.json.

TDD: Tests written BEFORE implementation to drive the design.

When temporal_options is NULL in the registry (e.g. after migration where options
were not written back to DB), the scheduler must fall back to reading max_commits
from temporal_meta.json in the repo's index directory.

Covers:
- test_null_temporal_options_reads_max_commits_from_meta_json
- test_null_temporal_options_reads_max_commits_from_provider_aware_path
- test_null_temporal_options_no_meta_json_no_max_commits_flag
- test_temporal_options_present_uses_temporal_options_not_meta_json
"""

import json
from unittest.mock import MagicMock, patch


import code_indexer.services.progress_subprocess_runner as psr_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(registry):
    """Build a RefreshScheduler with enough state to call _index_source."""
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = object.__new__(RefreshScheduler)
    scheduler.registry = registry
    return scheduler


def _make_registry(
    alias_name,
    enable_temporal=True,
    temporal_options=None,
    repo_url="git@github.com:org/repo.git",
):
    """Build a mock registry that returns the given repo_info."""
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
    """Call _index_source and capture all commands issued."""
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
    ):
        captured.append(list(command))

    with (
        patch("subprocess.run", side_effect=recording_run),
        patch.object(
            psr_mod, "run_with_popen_progress", side_effect=recording_popen_progress
        ),
    ):
        scheduler._index_source(alias_name, str(source_path))

    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRefreshSchedulerTemporalMetaFallback:
    """Bug #642 Step 2: NULL temporal_options must fall back to temporal_meta.json."""

    def test_null_temporal_options_reads_max_commits_from_legacy_meta_json(
        self, tmp_path
    ):
        """When temporal_options is NULL, read max_commits from legacy temporal_meta.json.

        The legacy path is: source_path/.code-indexer/index/code-indexer-temporal/
        """
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        scheduler = _make_scheduler(registry)

        # Create legacy temporal_meta.json with total_commits (conservative fallback)
        legacy_meta_dir = tmp_path / ".code-indexer" / "index" / "code-indexer-temporal"
        legacy_meta_dir.mkdir(parents=True)
        meta = {"last_commit": "abc123", "total_commits": 7, "indexed_at": "2024-01-01"}
        (legacy_meta_dir / "temporal_meta.json").write_text(json.dumps(meta))

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. All commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd, (
            "Bug #642: NULL temporal_options with temporal_meta.json must produce "
            f"--max-commits fallback. Got: {temporal_cmd}"
        )
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "7", (
            f"--max-commits value must be total_commits=7 from meta. Got: {temporal_cmd[idx + 1]}"
        )

    def test_null_temporal_options_reads_max_commits_from_provider_aware_meta_json(
        self, tmp_path
    ):
        """When temporal_options is NULL, read max_commits from provider-aware path.

        The new path is: source_path/.code-indexer/index/code-indexer-temporal-{model}/
        The scheduler must also scan this path when legacy path is absent.
        """
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        scheduler = _make_scheduler(registry)

        # Create provider-aware temporal_meta.json with max_commits (preferred field)
        provider_meta_dir = (
            tmp_path / ".code-indexer" / "index" / "code-indexer-temporal-voyage_code_3"
        )
        provider_meta_dir.mkdir(parents=True)
        meta = {
            "last_commit": "def456",
            "total_commits": 50,
            "max_commits": 12,
            "indexed_at": "2024-06-01",
        }
        (provider_meta_dir / "temporal_meta.json").write_text(json.dumps(meta))

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. All commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd, (
            "Bug #642: NULL temporal_options with provider-aware temporal_meta.json must "
            f"produce --max-commits fallback. Got: {temporal_cmd}"
        )
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "12", (
            f"--max-commits value must be max_commits=12 from meta. Got: {temporal_cmd[idx + 1]}"
        )

    def test_null_temporal_options_no_meta_json_no_max_commits_flag(self, tmp_path):
        """When temporal_options is NULL and no temporal_meta.json exists, no --max-commits."""
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        scheduler = _make_scheduler(registry)

        # No temporal_meta.json created

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. All commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" not in temporal_cmd, (
            "When no temporal_meta.json exists, --max-commits must NOT appear. "
            f"Got: {temporal_cmd}"
        )

    def test_temporal_options_present_uses_temporal_options_not_meta_json(
        self, tmp_path
    ):
        """When temporal_options is present, it takes precedence over temporal_meta.json."""
        alias = "my-repo-global"
        registry = _make_registry(
            alias, enable_temporal=True, temporal_options={"max_commits": 99}
        )
        scheduler = _make_scheduler(registry)

        # Create temporal_meta.json with different value — should be ignored
        meta_dir = tmp_path / ".code-indexer" / "index" / "code-indexer-temporal"
        meta_dir.mkdir(parents=True)
        meta = {"last_commit": "abc", "total_commits": 5, "indexed_at": "2024-01-01"}
        (meta_dir / "temporal_meta.json").write_text(json.dumps(meta))

        cmds = _capture_subprocess_cmds(scheduler, alias, tmp_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. All commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd, (
            f"temporal_options with max_commits=99 must produce --max-commits. Got: {temporal_cmd}"
        )
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "99", (
            f"temporal_options value (99) must take priority over meta_json (5). "
            f"Got: {temporal_cmd[idx + 1]}"
        )
