"""Bug #1316: cross-node MUTATION staleness on golden-repo cache HITS.

Bug #1314 fixed cache MISSES (`_resolve_golden_repo`: cache-first,
reload-on-backend-miss) so a repo registered by ANOTHER worker/node becomes
visible without a restart. This residual bug is about cache HITS: once a
repo is cached locally, `_resolve_golden_repo` trusts the cached object
forever, even if ANOTHER worker/node has since MUTATED one of its fields
(`default_branch`, `temporal_options`) in the shared backend.

Two mutation-path decisions read a field whose OUTCOME depends on
freshness and are fixed here via `_resolve_golden_repo_authoritative`
(unconditional shared-backend read, bypassing the cache-hit path):

1. `change_branch` / `change_branch_async`: compare `target_branch` against
   `golden_repo.default_branch` to short-circuit "already on branch". A
   stale cached `default_branch` produces a false no-op (should proceed)
   or a redundant job submission (should no-op).
2. `add_indexes_to_golden_repo`'s `background_worker`: reads
   `repo.temporal_options` to build the `cidx index --index-commits`
   command. A stale cached `temporal_options` builds the index with the
   WRONG max_commits/since_date/diff_context/all_branches.

No mocked DB layer for scenarios 1-2 (memory: feedback_faithful_db_mocks)
-- real `GoldenRepoMetadataSqliteBackend` via `GoldenRepoManager.__init__`.
Scenario 3 mocks `_sqlite_backend.get_repo` directly (the manager's own
already-abstracted contract), following the established precedent in
test_golden_repo_manager_add_indexes_temporal_pg_env_wiring_1313.py where
the surrounding indexing pipeline requires patching subprocess.run /
run_with_popen_progress anyway.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _register_repo(
    manager: GoldenRepoManager, alias: str, clone_path, default_branch: str = "main"
) -> None:
    clone_path.mkdir(parents=True, exist_ok=True)
    manager._sqlite_backend.add_repo(
        alias=alias,
        repo_url="https://example.com/cross-node-repo.git",
        default_branch=default_branch,
        clone_path=str(clone_path),
        created_at="2026-01-01T00:00:00+00:00",
        enable_temporal=False,
        temporal_options=None,
    )


@pytest.fixture
def manager(tmp_path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


class TestChangeBranchAsyncCrossNodeMutationStaleness:
    def test_avoids_redundant_job_when_cross_node_mutation_already_applied_target_branch(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        alias = "cross-node-async-branch"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo(manager, alias, clone_path, default_branch="main")

        # Populate THIS worker's cache with the stale value.
        assert manager.golden_repo_exists(alias) is True
        assert alias in manager.golden_repos
        assert manager.golden_repos[alias].default_branch == "main"

        # Simulate a cross-node mutation: another worker completed the
        # branch change directly against the shared backend.
        manager._sqlite_backend.update_default_branch(alias, "develop")

        manager.background_job_manager = Mock()

        result = manager.change_branch_async(
            alias=alias, target_branch="develop", submitter_username="test"
        )

        assert result == {"success": True, "job_id": None}
        manager.background_job_manager.submit_job.assert_not_called()


class TestChangeBranchCrossNodeMutationStaleness:
    def test_does_not_false_short_circuit_when_backend_branch_differs_from_stale_cache(
        self, manager: GoldenRepoManager, tmp_path
    ) -> None:
        alias = "cross-node-sync-branch"
        clone_path = tmp_path / "golden-repos" / alias
        _register_repo(manager, alias, clone_path, default_branch="main")

        # Populate THIS worker's cache with the stale value ("main").
        assert manager.golden_repo_exists(alias) is True
        assert manager.golden_repos[alias].default_branch == "main"

        # Cross-node mutation: another worker/node moved the TRUE backend
        # value to "staging". Stale cache still says "main".
        manager._sqlite_backend.update_default_branch(alias, "staging")

        # Caller wants to go back to "main". With the bug, stale cache
        # falsely reports default_branch == "main" == target_branch and
        # short-circuits as a no-op WITHOUT touching git.
        manager._cb_git_fetch_and_validate = Mock()
        manager._cb_checkout_and_pull = Mock()
        manager._cb_cidx_index = Mock()
        manager._cb_cow_snapshot = Mock(return_value=str(clone_path) + "-snapshot")
        manager._cb_fts_branch_cleanup = Mock()
        manager._cb_hnsw_branch_cleanup = Mock()
        manager._cb_swap_alias = Mock()
        manager.resource_config = None
        manager._refresh_scheduler = None

        # A bare GoldenRepoManager (no full server-app lifespan) never
        # creates description_refresh_tracking / dependency_map_tracking --
        # those tables are created only by DatabaseSchema.initialize_database()
        # during full startup. Stub these two incidental invalidation calls
        # as no-ops; they are unrelated to the authoritative default_branch
        # read under test here.
        manager._sqlite_backend.invalidate_description_refresh_tracking = Mock()
        manager._sqlite_backend.invalidate_dependency_map_tracking = Mock()

        result = manager.change_branch(alias=alias, target_branch="main")

        manager._cb_git_fetch_and_validate.assert_called_once()
        assert result["message"] != "Already on branch 'main'"


class TestAddIndexesTemporalOptionsCrossNodeMutationStaleness:
    def _make_manager(self, tmp_path, cached_temporal_options):
        with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
            manager = GoldenRepoManager.__new__(GoldenRepoManager)

        repo_path = tmp_path / "golden-repos" / "test-repo"
        (repo_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

        manager.data_dir = str(tmp_path)
        manager.golden_repos_dir = str(tmp_path / "golden-repos")

        golden_repo = Mock()
        golden_repo.alias = "test-repo"
        golden_repo.clone_path = str(repo_path)
        golden_repo.temporal_options = cached_temporal_options
        golden_repo.enable_temporal = False

        manager.golden_repos = {"test-repo": golden_repo}
        manager.get_actual_repo_path = Mock(return_value=str(repo_path))
        manager._sqlite_backend = Mock()
        manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
        manager._sqlite_backend.get_repo = Mock(
            return_value={
                "alias": "test-repo",
                "repo_url": "https://example.com/test-repo.git",
                "default_branch": "main",
                "clone_path": str(repo_path),
                "created_at": "2026-01-01T00:00:00+00:00",
                "enable_temporal": False,
                "temporal_options": {"max_commits": 999},
                "category_id": None,
                "category_auto_assigned": False,
                "wiki_enabled": False,
            }
        )
        manager._global_repos_backend = Mock()

        captured_workers = []

        def capture_and_run(operation_type, func, submitter_username, **kwargs):
            captured_workers.append(func)
            return "job-add-indexes-test"

        manager.background_job_manager = Mock()
        manager.background_job_manager.submit_job.side_effect = capture_and_run
        manager._captured_workers = captured_workers
        manager._refresh_scheduler = None

        return manager, repo_path

    def _run_captured_worker(self, manager) -> None:
        assert len(manager._captured_workers) == 1
        manager._captured_workers[0]()

    def _mock_subprocess_run(self, command, **kwargs):
        return Mock(returncode=0, stdout="", stderr="")

    def test_temporal_command_uses_fresh_backend_temporal_options_not_stale_cache(
        self, tmp_path
    ) -> None:
        # This worker's cache holds a STALE temporal_options value from an
        # earlier read. Another node has since saved fresh options
        # ({"max_commits": 999}) via save_temporal_options -- reflected in
        # the mocked _sqlite_backend.get_repo, not in the stale cache entry.
        manager, repo_path = self._make_manager(
            tmp_path, cached_temporal_options={"max_commits": 100}
        )

        calls = []

        def _fake_run_with_popen_progress(*, command, phase_name, env=None, **kwargs):
            calls.append({"phase_name": phase_name, "command": command, "env": env})
            return 100

        with (
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=self._mock_subprocess_run,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=_fake_run_with_popen_progress,
            ),
        ):
            manager.add_indexes_to_golden_repo(
                alias="test-repo", index_types=["temporal"]
            )
            self._run_captured_worker(manager)

        by_phase = {c["phase_name"]: c["command"] for c in calls}
        temporal_command = by_phase["temporal"]
        assert "--max-commits" in temporal_command
        idx = temporal_command.index("--max-commits")
        assert temporal_command[idx + 1] == "999", (
            f"expected fresh max_commits=999 from authoritative backend read, "
            f"got command: {temporal_command}"
        )
        assert "100" not in temporal_command
