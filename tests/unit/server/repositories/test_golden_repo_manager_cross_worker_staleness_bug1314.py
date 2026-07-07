"""Bug #1314: cross-worker/cross-node golden-repo staleness.

Root cause (confirmed live on a staging cluster, v11.23.0, postgres, 3 nodes
x 2 uvicorn workers behind HAProxy): golden-repo MANAGEMENT operations
resolved the repo from `GoldenRepoManager.golden_repos`, an in-memory dict
populated ONCE in `__init__` (via `_load_metadata_from_sqlite`) and never
reloaded per request. A repo registered AFTER a worker started is visible
only to the ONE worker that served `add_golden_repo` -- every other worker
returns "Golden repository '<alias>' not found" for
`add_golden_repo_index`, `refresh_golden_repo`, and `get_golden_repo_indexes`.

This test reproduces the staleness by constructing a single `GoldenRepoManager`
instance (representing "this worker") and inserting a repo directly into its
REAL SQLite backend (`manager._sqlite_backend.add_repo(...)`) WITHOUT touching
`manager.golden_repos` -- exactly what happens when a DIFFERENT worker/node
handles `add_golden_repo` and writes to the shared backend (SQLite solo /
PostgreSQL cluster) while this worker's own in-memory cache never sees it.

No mocked DB layer is used (memory: feedback_faithful_db_mocks) -- this
exercises the real `GoldenRepoMetadataSqliteBackend` via
`GoldenRepoManager.__init__`'s normal wiring.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _register_repo_directly(
    manager: GoldenRepoManager, alias: str, clone_path: Path
) -> None:
    """Simulate a DIFFERENT worker registering a golden repo.

    Writes straight to the shared SQLite backend WITHOUT touching this
    manager's in-memory `golden_repos` cache, reproducing the exact
    cross-worker staleness condition from Bug #1314.
    """
    clone_path.mkdir(parents=True, exist_ok=True)
    manager._sqlite_backend.add_repo(
        alias=alias,
        repo_url="https://example.com/cross-worker-repo.git",
        default_branch="main",
        clone_path=str(clone_path),
        created_at="2026-01-01T00:00:00+00:00",
        enable_temporal=False,
        temporal_options=None,
    )


@pytest.fixture
def manager(tmp_path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


class TestCrossWorkerGoldenRepoStalenessBug1314:
    """Reproduces and verifies the fix for the cross-worker staleness bug."""

    def test_reproduction_setup_is_valid_repo_absent_from_local_cache(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        """Sanity check: the repo must be missing from the in-memory cache
        for this to actually reproduce the cross-worker staleness bug."""
        alias = "cross-worker-setup-check"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        assert alias not in manager.golden_repos, (
            "test setup invalid: alias must be absent from this worker's "
            "in-memory cache to reproduce Bug #1314"
        )
        # But the shared backend already has it (the other worker's write).
        assert manager._sqlite_backend.get_repo(alias) is not None

    def test_golden_repo_exists_resolves_repo_registered_by_another_worker(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        alias = "cross-worker-exists"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        assert manager.golden_repo_exists(alias) is True

    def test_get_actual_repo_path_resolves_repo_registered_by_another_worker(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        alias = "cross-worker-path"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        resolved_path = manager.get_actual_repo_path(alias)

        assert resolved_path == str(clone_path)

    def test_get_golden_repo_indexes_resolves_repo_registered_by_another_worker(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        alias = "cross-worker-indexes"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        result = manager.get_golden_repo_indexes(alias)

        assert result["alias"] == alias
        assert "indexes" in result

    def test_add_indexes_to_golden_repo_eager_check_resolves_cross_worker_repo(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        """Reproduces the named `add_golden_repo_index` staleness symptom."""
        alias = "cross-worker-add-index"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        manager.background_job_manager = Mock()
        manager.background_job_manager.submit_job = Mock(return_value="job-1")
        manager._refresh_scheduler = None

        job_id = manager.add_indexes_to_golden_repo(
            alias=alias, index_types=["semantic"]
        )

        assert job_id == "job-1"

    def test_change_branch_async_eager_check_resolves_cross_worker_repo(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        """Reproduces the named `refresh_golden_repo` staleness symptom
        (change_branch_async is the manager-level entry point used by the
        branch-change / refresh job submission path)."""
        alias = "cross-worker-refresh"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        manager.background_job_manager = Mock()
        manager.background_job_manager.submit_job = Mock(return_value="job-2")

        result = manager.change_branch_async(
            alias=alias, target_branch="develop", submitter_username="test-admin"
        )

        assert result["success"] is True
        assert result["job_id"] == "job-2"

    def test_find_by_canonical_url_finds_repo_registered_by_another_worker(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        """Duplicate-URL detection during add_golden_repo must also see
        repos registered by other workers -- otherwise a duplicate alias
        for the same URL could be silently allowed cluster-wide."""
        alias = "cross-worker-canonical"
        clone_path = tmp_path / "golden-repos" / alias
        clone_path.mkdir(parents=True, exist_ok=True)
        manager._sqlite_backend.add_repo(
            alias=alias,
            repo_url="https://github.com/example/cross-worker-canonical.git",
            default_branch="main",
            clone_path=str(clone_path),
            created_at="2026-01-01T00:00:00+00:00",
            enable_temporal=False,
            temporal_options=None,
        )

        matches = manager.find_by_canonical_url(
            "github.com/example/cross-worker-canonical"
        )

        assert len(matches) == 1
        assert matches[0]["alias"] == alias

    def test_add_golden_repo_rejects_duplicate_alias_registered_by_another_worker(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        """add_golden_repo's duplicate-alias guard must see repos registered
        by other workers, else a second worker could stomp/duplicate the
        same alias registered elsewhere in the cluster."""
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoError,
        )

        alias = "cross-worker-dup-alias"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo_directly(manager, alias, clone_path)

        manager.background_job_manager = Mock()

        with pytest.raises(GoldenRepoError):
            manager.add_golden_repo(repo_url="local://somewhere", alias=alias)
