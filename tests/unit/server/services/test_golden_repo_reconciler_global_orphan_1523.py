"""
Tests for the reverse-direction reconcile pass added for Bug #1523.

Bug #1317's reconciler sweeps ONE direction: a `golden_repos` row whose
on-disk clone is absent. Bug #1523 produced the OPPOSITE shape -- a live
global-registry entry + `-global` alias pointer whose `golden_repos` row is
already gone. That sweep can never find it, because it iterates
`list_golden_repos()` and the wedged alias is no longer in that list.

The root-cause ordering fix in remove_golden_repo() stops NEW wedges, but it
cannot heal installations that are ALREADY wedged (the reported staging
incident) -- and no front-door recovery exists for them: removal reports
"not found" (row gone) and re-registration is blocked by the leftover clone
directory. This pass closes that direction.

Safety: the sweep is only meaningful because every global-registry entry is,
by construction, backed by a golden repo -- `register_global_repo` has
exactly ONE production writer (`GlobalActivator.activate_golden_repo`, only
ever called for golden repos) and `RESERVED_GLOBAL_NAMES` is empty. Two
guards still apply: each candidate is re-confirmed against the shared
backend individually, and an EMPTY `golden_repos` list suppresses the whole
pass (indistinguishable from a backend read failure).

Uses the REAL GoldenRepoManager, REAL SQLite backend and REAL
GlobalActivator/AliasManager; only the BackgroundJobManager dispatch
boundary is mocked.
"""

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_activation import GlobalActivator
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.services.golden_repo_reconciler import (
    reconcile_golden_repo_registry,
)


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def manager(temp_data_dir):
    mgr = GoldenRepoManager(data_dir=temp_data_dir)

    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(mgr.db_path).initialize_database()

    mock_bjm = MagicMock(spec=BackgroundJobManager)
    mock_bjm.submit_job.return_value = "job-1523"
    mgr.background_job_manager = mock_bjm
    return mgr


def alias_manager_for(manager: GoldenRepoManager) -> AliasManager:
    return AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))


def register_globally_active_repo(manager: GoldenRepoManager, alias: str) -> str:
    """Register `alias` with a real clone directory, a real `golden_repos`
    row, and a real global activation (registry entry + alias pointer)."""
    clone_path = os.path.join(manager.golden_repos_dir, alias)
    os.makedirs(clone_path, exist_ok=True)

    golden_repo = GoldenRepo(
        alias=alias,
        repo_url=f"https://github.com/test/{alias}.git",
        default_branch="main",
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
        temporal_options=None,
    )
    manager.golden_repos[alias] = golden_repo
    manager._sqlite_backend.add_repo(
        alias=golden_repo.alias,
        repo_url=golden_repo.repo_url,
        default_branch=golden_repo.default_branch,
        clone_path=golden_repo.clone_path,
        created_at=golden_repo.created_at,
        enable_temporal=golden_repo.enable_temporal,
        temporal_options=golden_repo.temporal_options,
    )
    GlobalActivator(manager.golden_repos_dir).activate_golden_repo(
        repo_name=alias,
        repo_url=golden_repo.repo_url,
        clone_path=clone_path,
    )
    assert manager.is_globally_active(alias)
    assert alias_manager_for(manager).alias_exists(f"{alias}-global")
    return clone_path


def wedge_global_orphan(manager: GoldenRepoManager, alias: str) -> None:
    """Reproduce the Bug #1523 end state: drop the `golden_repos` row while
    leaving the global registry entry, the `-global` alias pointer, and the
    clone directory in place."""
    manager._sqlite_backend.remove_repo(alias)
    manager.golden_repos.pop(alias, None)
    assert manager._sqlite_backend.get_repo(alias) is None
    assert manager.is_globally_active(alias)


class TestGlobalRegistryOrphanReconcileBug1523:
    """A global-registry entry with no `golden_repos` row must be swept."""

    def test_global_registry_orphan_with_no_row_is_removed(self, manager):
        register_globally_active_repo(manager, "healthy-one")
        register_globally_active_repo(manager, "healthy-two")
        register_globally_active_repo(manager, "wedged")
        wedge_global_orphan(manager, "wedged")

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.global_orphans_found == ["wedged"]
        assert result.global_orphans_removed == ["wedged"]
        assert result.global_orphans_failed == []

        # The wedged global half is gone -- no longer advertised/queryable.
        aliases = alias_manager_for(manager)
        assert not manager.is_globally_active("wedged")
        assert not aliases.alias_exists("wedged-global")

        # Healthy globally-active repos are untouched.
        for alias in ("healthy-one", "healthy-two"):
            assert manager.is_globally_active(alias)
            assert aliases.alias_exists(f"{alias}-global")

    def test_empty_golden_repo_registry_never_mass_removes_global_entries(
        self, manager
    ):
        """An empty `golden_repos` list makes EVERY global entry look orphaned
        -- indistinguishable from a shared-backend read failure. The pass must
        delete nothing rather than tear down the whole global registry."""
        register_globally_active_repo(manager, "repo-a")
        register_globally_active_repo(manager, "repo-b")
        wedge_global_orphan(manager, "repo-a")
        wedge_global_orphan(manager, "repo-b")
        assert manager.list_golden_repos() == []

        result = reconcile_golden_repo_registry(manager)

        assert result.global_orphans_removed == []
        aliases = alias_manager_for(manager)
        for alias in ("repo-a", "repo-b"):
            assert manager.is_globally_active(alias)
            assert aliases.alias_exists(f"{alias}-global")
