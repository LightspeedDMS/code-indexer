"""
Unit tests for RefreshScheduler initial spread and new repo offset (Story #284 AC2 & AC5).

Tests:
- _assign_initial_spread(): single repo, N repos staggered, all in future,
  empty list, spread within interval, persisted to registry
- New repo with NULL next_refresh: gets staggered offset, NOT immediate refresh
- Local repo exclusion from spread
"""

import time
from pathlib import Path
from typing import List

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler, _is_git_repo_url
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


def _make_scheduler(tmp_path: Path) -> tuple[RefreshScheduler, Path, GlobalRegistry]:
    """Create a RefreshScheduler with required setup."""
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir(parents=True)

    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    tracker = QueryTracker()
    cleanup_mgr = CleanupManager(tracker)
    registry = GlobalRegistry(str(golden_repos_dir))

    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=tracker,
        cleanup_manager=cleanup_mgr,
        registry=registry,
    )
    return scheduler, golden_repos_dir, registry


def _register_git_repo(
    golden_repos_dir: Path,
    registry: GlobalRegistry,
    alias: str,
    repo_url: str = "https://github.com/test/repo.git",
) -> Path:
    """Register a git repo in alias + registry, create master clone dir."""
    global_alias = f"{alias}-global"
    master_path = golden_repos_dir / alias
    master_path.mkdir(parents=True, exist_ok=True)

    alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
    alias_mgr.create_alias(global_alias, str(master_path))
    registry.register_global_repo(
        alias,
        global_alias,
        repo_url,
        str(master_path),
    )
    return master_path


class TestAssignInitialSpread:
    """Tests for _assign_initial_spread() method (Story #284 AC2)."""

    def test_single_repo_gets_full_interval_offset(self, tmp_path):
        """
        With 1 repo, spacing = interval/1 = interval.
        next_refresh should be approximately now + interval.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "repo-a")

        repos = registry.list_global_repos()
        refresh_interval = 3600

        before = time.time()
        scheduler._assign_initial_spread(repos, refresh_interval)
        after = time.time()

        updated = registry.list_global_repos()
        assert len(updated) == 1
        next_refresh = float(updated[0]["next_refresh"])

        assert next_refresh >= before + refresh_interval - 1
        assert next_refresh <= after + refresh_interval + 1

    def test_multiple_repos_get_distinct_offsets(self, tmp_path):
        """With N repos, each gets a distinct next_refresh time slot."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)

        for alias in ["repo-a", "repo-b", "repo-c", "repo-d"]:
            _register_git_repo(golden_repos_dir, registry, alias)

        repos = registry.list_global_repos()
        scheduler._assign_initial_spread(repos, 3600)

        updated = registry.list_global_repos()
        next_refreshes = [float(r["next_refresh"]) for r in updated]

        assert len(set(next_refreshes)) == len(next_refreshes), (
            "Expected all repos to have distinct next_refresh timestamps"
        )

    def test_all_initial_offsets_are_in_future(self, tmp_path):
        """All assigned next_refresh values are strictly in the future (> now)."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)

        for alias in ["repo-a", "repo-b", "repo-c"]:
            _register_git_repo(golden_repos_dir, registry, alias)

        repos = registry.list_global_repos()
        now = time.time()
        scheduler._assign_initial_spread(repos, 3600)

        for repo in registry.list_global_repos():
            next_refresh = float(repo["next_refresh"])
            assert next_refresh > now, (
                f"Repo {repo['alias_name']} has next_refresh in the past"
            )

    def test_empty_list_does_not_raise(self, tmp_path):
        """_assign_initial_spread() with empty list does not raise."""
        scheduler, _, _ = _make_scheduler(tmp_path)
        scheduler._assign_initial_spread([], 3600)  # Should not raise

    def test_initial_spread_within_one_interval(self, tmp_path):
        """
        All next_refresh values fall within (now+spacing, now+interval].
        The earliest slot is spacing = interval/N from now.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)

        for alias in ["repo-a", "repo-b", "repo-c"]:
            _register_git_repo(golden_repos_dir, registry, alias)

        repos = registry.list_global_repos()
        refresh_interval = 3600
        n = len(repos)
        spacing = refresh_interval / n

        before = time.time()
        scheduler._assign_initial_spread(repos, refresh_interval)
        after = time.time()

        for repo in registry.list_global_repos():
            next_refresh = float(repo["next_refresh"])
            assert next_refresh >= before + spacing - 1, (
                f"next_refresh {next_refresh} too early (< before+spacing)"
            )
            assert next_refresh <= after + refresh_interval + 1, (
                f"next_refresh {next_refresh} too far in future"
            )

    def test_initial_spread_persists_to_registry(self, tmp_path):
        """
        After _assign_initial_spread(), registry reflects the new next_refresh values.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "my-repo")

        repos = registry.list_global_repos()
        assert repos[0].get("next_refresh") is None, (
            "Repo should start without next_refresh"
        )

        scheduler._assign_initial_spread(repos, 3600)

        updated = registry.list_global_repos()
        assert updated[0]["next_refresh"] is not None, (
            "next_refresh should be set after _assign_initial_spread()"
        )


class TestNewRepoInitialOffset:
    """
    AC2 & AC5: Repos with NULL next_refresh get staggered offsets,
    NOT submitted for immediate refresh.
    """

    def test_null_next_refresh_triggers_spread_not_refresh(self, tmp_path):
        """
        When scheduler sees a repo with next_refresh=None, it assigns spread
        but does NOT submit that repo for refresh in the same cycle.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "new-repo")

        repos = registry.list_global_repos()
        assert repos[0].get("next_refresh") is None

        submitted_aliases: List[str] = []

        def track_submit(alias_name, **kwargs):
            submitted_aliases.append(alias_name)
            return None

        scheduler._submit_refresh_job = track_submit

        # Simulate one scheduler loop iteration logic
        repos = registry.list_global_repos()
        refresh_interval = 3600

        git_repos = [
            r for r in repos
            if r.get("alias_name") and _is_git_repo_url(r.get("repo_url", ""))
        ]

        unscheduled = [r for r in git_repos if r.get("next_refresh") is None]
        if unscheduled:
            scheduler._assign_initial_spread(unscheduled, refresh_interval)

        # Re-read to get updated next_refresh values
        repos = registry.list_global_repos()
        git_repos = [
            r for r in repos
            if r.get("alias_name") and _is_git_repo_url(r.get("repo_url", ""))
        ]

        now = time.time()
        for repo in git_repos:
            alias_name = repo.get("alias_name")
            next_refresh_str = repo.get("next_refresh")
            if next_refresh_str is None:
                continue
            try:
                next_refresh = float(next_refresh_str)
            except (ValueError, TypeError):
                continue
            if now < next_refresh:
                continue
            scheduler._submit_refresh_job(alias_name)

        assert "new-repo-global" not in submitted_aliases, (
            "Newly spread repo should NOT be submitted in the same cycle"
        )

    def test_local_repo_excluded_from_spread(self, tmp_path):
        """Local repos (non-git URLs) are excluded from initial spread."""
        _, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(
            golden_repos_dir,
            registry,
            "local-repo",
            repo_url="local://some/path",
        )

        repos = registry.list_global_repos()
        git_repos = [
            r for r in repos
            if r.get("alias_name") and _is_git_repo_url(r.get("repo_url", ""))
        ]

        assert len(git_repos) == 0, (
            "Local repo should be excluded from git repo list"
        )
