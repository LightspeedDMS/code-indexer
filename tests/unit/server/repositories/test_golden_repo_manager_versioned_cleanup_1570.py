"""
Regression tests for Bug #1570 -- removing a golden repo orphans its entire
`.versioned/{alias}/` snapshot tree forever.

Half 1 (this file): `remove_golden_repo`'s cascade must delete
`.versioned/{alias}/` for the repo being removed, and must do so BEFORE
`GlobalActivator.deactivate_golden_repo` deletes the `-global` alias
pointer (a non-local clone backend may need the pointer to identify the
on-disk namespace; deleting the pointer first would strand it).

These tests use a REAL GoldenRepoManager, REAL SQLite backend, REAL
GlobalActivator/AliasManager, a REAL VersionedSnapshotManager (local
filesystem CoW mode, no clone_backend/flexclone -- exactly how
lifespan.py wires it in solo/SQLite deployments), and REAL directories on
disk standing in for versioned snapshots. Only BackgroundJobManager (a
pure threading/dispatch concern) is mocked so the worker closure runs
synchronously in the test thread.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_activation import GlobalActivator
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)


@pytest.fixture
def temp_data_dir(tmp_path):
    yield str(tmp_path)


@pytest.fixture
def manager(temp_data_dir):
    """Real GoldenRepoManager with a full DB schema (incl. global_repos) and
    a real, LOCAL-mode VersionedSnapshotManager wired -- mirroring exactly
    how lifespan.py wires golden_repo_manager._snapshot_manager in a
    solo/SQLite deployment (Bug #1570 was reproduced on exactly that
    topology). Only the BackgroundJobManager dispatch boundary is mocked.
    """
    mgr = GoldenRepoManager(data_dir=temp_data_dir)

    mock_bjm = MagicMock(spec=BackgroundJobManager)
    mock_bjm.submit_job.return_value = "test-job-id-1570"
    mgr.background_job_manager = mock_bjm

    DatabaseSchema(mgr.db_path).initialize_database()

    mgr._snapshot_manager = VersionedSnapshotManager(
        versioned_base=mgr.golden_repos_dir
    )
    return mgr


def captured_worker(manager: GoldenRepoManager):
    """Return the func passed to the most recent submit_job() call."""
    return manager.background_job_manager.submit_job.call_args[1]["func"]


def alias_manager_for(manager: GoldenRepoManager) -> AliasManager:
    return AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))


def versioned_namespace_dir(manager: GoldenRepoManager, alias: str) -> Path:
    return Path(manager.golden_repos_dir) / ".versioned" / alias


def register_globally_active_repo_with_versioned_snapshot(
    manager: GoldenRepoManager, alias: str
) -> str:
    """Register `alias` with a real on-disk clone, a real global activation,
    AND a real `.versioned/{alias}/v_<ts>` snapshot directory containing a
    real file -- exactly the state a repo with query-time versioning has
    accumulated by the time it is removed.
    """
    clone_path = os.path.join(manager.golden_repos_dir, alias)
    os.makedirs(clone_path, exist_ok=True)
    with open(os.path.join(clone_path, "README.md"), "w") as handle:
        handle.write("payload\n")

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

    # A real versioned snapshot, created through the real VersionedSnapshotManager
    # (not a hand-built directory) so the on-disk shape matches production exactly.
    snapshot_path = manager._snapshot_manager.create_snapshot(alias, clone_path)
    assert Path(snapshot_path).exists()
    assert Path(snapshot_path).is_relative_to(versioned_namespace_dir(manager, alias))

    # Preconditions.
    assert manager.is_globally_active(alias)
    assert alias_manager_for(manager).alias_exists(f"{alias}-global")
    assert versioned_namespace_dir(manager, alias).exists()
    return clone_path


class TestVersionedSnapshotCleanupOnRemovalBug1570:
    def test_remove_golden_repo_deletes_versioned_snapshot_tree(self, manager):
        """Bug #1570 Half 1: removing a golden repo must delete its entire
        `.versioned/{alias}/` namespace directory, not just the base clone.
        """
        alias = "leaky-repo"
        register_globally_active_repo_with_versioned_snapshot(manager, alias)
        ns_dir = versioned_namespace_dir(manager, alias)

        manager.remove_golden_repo(alias)
        result = captured_worker(manager)()

        assert result["success"] is True
        assert not ns_dir.exists(), (
            "the .versioned/{alias}/ snapshot tree must not survive golden "
            "repo removal -- Bug #1570"
        )

    def test_remove_golden_repo_cleans_up_versioned_tree_without_snapshot_manager(
        self, manager
    ):
        """Fallback path: even when no VersionedSnapshotManager is wired
        (e.g. it failed to initialize at startup), removal must still
        reclaim the deterministic local `.versioned/{alias}/` directory."""
        alias = "leaky-repo-no-manager"
        register_globally_active_repo_with_versioned_snapshot(manager, alias)
        ns_dir = versioned_namespace_dir(manager, alias)
        assert ns_dir.exists()

        manager._snapshot_manager = None  # simulate startup wiring failure

        manager.remove_golden_repo(alias)
        result = captured_worker(manager)()

        assert result["success"] is True
        assert not ns_dir.exists()

    def test_versioned_tree_deleted_before_alias_pointer_is_deleted(self, manager):
        """Ordering (Bug #1570): the versioned root must be resolved/deleted
        BEFORE the `-global` alias pointer is removed, since a non-local
        clone backend may need the (about-to-be-deleted) pointer to
        identify the on-disk namespace. Observed via the real shutil.rmtree
        dependency, exactly like the Bug #1523 ordering test."""
        alias = "ordering-repo"
        register_globally_active_repo_with_versioned_snapshot(manager, alias)
        ns_dir = versioned_namespace_dir(manager, alias)
        aliases = alias_manager_for(manager)

        observed = {}
        real_rmtree = shutil.rmtree

        def recording_rmtree(path, *args, **kwargs):
            if str(path) == str(ns_dir):
                observed["alias_exists_during_versioned_cleanup"] = (
                    aliases.alias_exists(f"{alias}-global")
                )
            return real_rmtree(path, *args, **kwargs)

        manager.remove_golden_repo(alias)
        worker = captured_worker(manager)

        with patch.object(shutil, "rmtree", side_effect=recording_rmtree):
            result = worker()

        assert result["success"] is True
        assert observed, "versioned namespace directory was never rmtree'd"
        assert observed["alias_exists_during_versioned_cleanup"] is True
