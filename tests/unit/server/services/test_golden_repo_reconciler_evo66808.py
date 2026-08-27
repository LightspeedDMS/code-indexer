"""
EVO-66808: the golden-repo reconcile repoints a STALE `global_repos.index_path`.

A `global_repos` row can carry an absolute `index_path` baked at registration
under an older data-dir layout (e.g. `/opt/code-indexer/.cidx-server/data/
golden-repos/<name>`) that no longer exists after the data dir moved (e.g.
`/data/data/golden-repos/<name>`). A resolver that trusts that stale path lands
on a non-existent root -> ENOENT ("os error 2"); observed live on evolution-global.

The reconcile's healthy-repo pass now heals it via a TARGETED
`update_index_path` (no other registry field disturbed), CONSERVATIVELY: only
when the stored path is absent on disk AND the actual clone dir exists, so a
valid index_path is never churned. Mirrors the Bug #1317 pointer-repair tests.
"""

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.services.golden_repo_reconciler import (
    reconcile_golden_repo_registry,
)
from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend


@pytest.mark.e2e
class TestGoldenRepoReconcilerEvo66808:
    @pytest.fixture
    def temp_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def manager(self, temp_data_dir):
        mgr = GoldenRepoManager(data_dir=temp_data_dir)
        from code_indexer.server.storage.database_manager import DatabaseSchema

        DatabaseSchema(mgr.db_path).initialize_database()

        mock_bjm = MagicMock(spec=BackgroundJobManager)
        mock_bjm.submit_job.side_effect = lambda **kwargs: (
            f"job-{kwargs.get('repo_alias', 'x')}"
        )
        mgr.background_job_manager = mock_bjm
        return mgr

    def _register_repo(self, manager: GoldenRepoManager, alias: str) -> str:
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
        return clone_path

    def _make_globally_active(
        self, manager: GoldenRepoManager, alias: str, index_path: str
    ) -> None:
        """Register a `{alias}-global` row with an explicit index_path (no alias
        pointer file), so is_globally_active() is True and the row's index_path
        is exactly what we set."""
        golden_repo = manager.golden_repos[alias]
        GlobalReposSqliteBackend(manager.db_path).register_repo(
            alias_name=f"{alias}-global",
            repo_name=alias,
            repo_url=golden_repo.repo_url,
            index_path=index_path,
        )

    def test_reconcile_repoints_stale_index_path_to_the_live_clone(self, manager):
        clone_path = self._register_repo(manager, "evolution")
        stale = "/opt/code-indexer/.cidx-server/data/golden-repos/evolution"
        assert not os.path.exists(stale)
        self._make_globally_active(manager, "evolution", stale)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.index_paths_repaired == ["evolution"]
        assert result.index_paths_repair_failed == []
        row = GlobalReposSqliteBackend(manager.db_path).get_repo("evolution-global")
        assert row is not None
        assert row["index_path"] == clone_path

    def test_reconcile_leaves_a_valid_index_path_untouched(self, manager):
        clone_path = self._register_repo(manager, "valid-repo")
        # index_path already points at the live clone -> nothing to repair.
        self._make_globally_active(manager, "valid-repo", clone_path)

        result = reconcile_golden_repo_registry(manager)

        assert result.aborted is False
        assert result.index_paths_repaired == []
        row = GlobalReposSqliteBackend(manager.db_path).get_repo("valid-repo-global")
        assert row["index_path"] == clone_path

    def test_index_path_repair_preserves_other_registry_fields(self, manager):
        """The targeted update must NOT reset enable_temporal / enable_scip /
        temporal_options as a full register_repo upsert would."""
        clone_path = self._register_repo(manager, "flags-repo")
        backend = GlobalReposSqliteBackend(manager.db_path)
        backend.register_repo(
            alias_name="flags-repo-global",
            repo_name="flags-repo",
            repo_url="https://github.com/test/flags-repo.git",
            index_path="/opt/old/golden-repos/flags-repo",
            enable_temporal=True,
            enable_scip=True,
        )

        result = reconcile_golden_repo_registry(manager)

        assert result.index_paths_repaired == ["flags-repo"]
        row = backend.get_repo("flags-repo-global")
        assert row["index_path"] == clone_path
        assert bool(row["enable_temporal"]) is True
        assert bool(row["enable_scip"]) is True
