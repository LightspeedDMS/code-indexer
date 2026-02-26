"""
Unit tests for per-repo next_refresh gating and back-propagation (Story #284 AC1).

Tests:
- Per-repo gating: skips repos where now < next_refresh, submits due repos
- Back-propagation: next_refresh updated after refresh with jitter
- AC1: After multiple cycles, timestamps diverge
- Jitter bounds on back-propagated next_refresh
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


def _run_gating_logic(
    registry: GlobalRegistry,
    scheduler: RefreshScheduler,
    submitted_aliases: List[str],
) -> None:
    """
    Execute the per-repo gating loop logic (mirrors _scheduler_loop() internals).
    Submits via track_submit captured in submitted_aliases.
    """
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
        submitted_aliases.append(alias_name)


class TestPerRepoGating:
    """Tests for per-repo next_refresh gating logic (Story #284)."""

    def test_repo_not_due_is_skipped(self, tmp_path):
        """A repo whose next_refresh is in the future is NOT submitted."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "future-repo")

        registry.update_next_refresh("future-repo-global", time.time() + 9999)

        submitted_aliases: List[str] = []
        _run_gating_logic(registry, scheduler, submitted_aliases)

        assert "future-repo-global" not in submitted_aliases

    def test_repo_past_due_is_submitted(self, tmp_path):
        """A repo whose next_refresh is in the past is submitted for refresh."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "past-repo")

        registry.update_next_refresh("past-repo-global", time.time() - 100)

        submitted_aliases: List[str] = []
        _run_gating_logic(registry, scheduler, submitted_aliases)

        assert "past-repo-global" in submitted_aliases

    def test_mixed_repos_only_due_ones_submitted(self, tmp_path):
        """With due and not-due repos, only due ones are submitted."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)

        for alias in ["due-repo", "not-due-repo", "also-due"]:
            _register_git_repo(golden_repos_dir, registry, alias)

        registry.update_next_refresh("due-repo-global", time.time() - 100)
        registry.update_next_refresh("not-due-repo-global", time.time() + 9999)
        registry.update_next_refresh("also-due-global", time.time() - 50)

        submitted_aliases: List[str] = []
        _run_gating_logic(registry, scheduler, submitted_aliases)

        assert "due-repo-global" in submitted_aliases
        assert "also-due-global" in submitted_aliases
        assert "not-due-repo-global" not in submitted_aliases

    def test_repo_with_null_next_refresh_skipped_in_gating(self, tmp_path):
        """A repo with no next_refresh is skipped by the gating logic."""
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "unscheduled-repo")

        # next_refresh remains None (not set)
        submitted_aliases: List[str] = []
        _run_gating_logic(registry, scheduler, submitted_aliases)

        assert "unscheduled-repo-global" not in submitted_aliases


class TestBackPropagation:
    """
    AC1: next_refresh updated after each refresh cycle with jitter,
    causing timestamps to diverge over multiple cycles.
    """

    def test_next_refresh_updated_after_submit(self, tmp_path):
        """
        After a repo is processed as due, its next_refresh is updated to
        now + interval + jitter (back-propagated into the future).
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)
        _register_git_repo(golden_repos_dir, registry, "bp-repo")

        registry.update_next_refresh("bp-repo-global", time.time() - 100)

        refresh_interval = 3600

        before = time.time()
        repos = registry.list_global_repos()
        git_repos = [
            r for r in repos
            if r.get("alias_name") and _is_git_repo_url(r.get("repo_url", ""))
        ]
        for repo in git_repos:
            alias_name = repo.get("alias_name")
            next_refresh_str = repo.get("next_refresh")
            if next_refresh_str is None:
                continue
            try:
                next_refresh = float(next_refresh_str)
            except (ValueError, TypeError):
                continue
            now = time.time()
            if now < next_refresh:
                continue
            jitter = scheduler._calculate_jitter(refresh_interval)
            registry.update_next_refresh(alias_name, now + refresh_interval + jitter)

        after = time.time()

        updated = registry.list_global_repos()
        new_nr = float(updated[0]["next_refresh"])
        max_jitter = refresh_interval * scheduler.JITTER_PERCENTAGE

        assert new_nr >= before + refresh_interval - max_jitter - 1
        assert new_nr <= after + refresh_interval + max_jitter + 1

    def test_back_propagation_produces_diverging_timestamps(self, tmp_path):
        """
        AC1: After 3 simulated cycles, repos accumulate different jitter amounts,
        causing their next_refresh timestamps to diverge.
        """
        scheduler, golden_repos_dir, registry = _make_scheduler(tmp_path)

        aliases = ["repo-a", "repo-b", "repo-c", "repo-d"]
        for alias in aliases:
            _register_git_repo(golden_repos_dir, registry, alias)

        # Set all due now
        for alias in aliases:
            registry.update_next_refresh(f"{alias}-global", time.time() - 1)

        refresh_interval = 3600

        # Simulate 3 back-propagation cycles
        for _ in range(3):
            repos = registry.list_global_repos()
            git_repos = [
                r for r in repos
                if r.get("alias_name") and _is_git_repo_url(r.get("repo_url", ""))
            ]
            for repo in git_repos:
                alias_name = repo.get("alias_name")
                jitter = scheduler._calculate_jitter(refresh_interval)
                registry.update_next_refresh(
                    alias_name, time.time() + refresh_interval + jitter
                )

        final_repos = registry.list_global_repos()
        final_next_refreshes = [
            float(r["next_refresh"]) for r in final_repos
            if r.get("next_refresh") is not None
        ]

        assert len(final_next_refreshes) == len(aliases)
        # After 3 cycles of random jitter, probability of all identical is ~0
        assert len(set(final_next_refreshes)) > 1, (
            "Expected timestamps to diverge after 3 jitter cycles"
        )

    def test_back_propagated_value_within_jitter_bounds(self, tmp_path):
        """
        The back-propagated next_refresh falls within [interval-max_jitter, interval+max_jitter].
        """
        scheduler, _, _ = _make_scheduler(tmp_path)
        refresh_interval = 3600
        max_jitter = refresh_interval * scheduler.JITTER_PERCENTAGE

        for _ in range(50):
            now = time.time()
            jitter = scheduler._calculate_jitter(refresh_interval)
            new_nr = now + refresh_interval + jitter

            lower = now + refresh_interval - max_jitter
            upper = now + refresh_interval + max_jitter

            assert new_nr >= lower - 0.001, (
                f"next_refresh {new_nr} below lower bound {lower}"
            )
            assert new_nr <= upper + 0.001, (
                f"next_refresh {new_nr} above upper bound {upper}"
            )
