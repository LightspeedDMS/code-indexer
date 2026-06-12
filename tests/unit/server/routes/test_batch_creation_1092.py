"""
Tests for Story #1092: Batch create returns immediately by skipping per-repo git validation.

These tests verify:
1. add_golden_repo() skips _validate_git_repository when skip_pre_flight_git_validation=True
2. add_golden_repo() still calls _validate_git_repository by default (no regression)
3. generate_unique_alias() uses prebuilt set when existing_aliases= kwarg provided
4. generate_unique_alias() calls list_golden_repos() when no existing_aliases provided
5. _batch_create_repos() calls list_golden_repos() exactly ONCE for any batch size
6. _batch_create_repos() passes skip_pre_flight_git_validation=True to every add_golden_repo()
"""

from unittest.mock import MagicMock, patch


class TestAddGoldenRepoSkipValidationFlag:
    """Tests for skip_pre_flight_git_validation parameter on add_golden_repo()."""

    def _make_manager(self, tmp_path):
        """Return a minimally-wired GoldenRepoManager using data_dir."""
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )

        manager = GoldenRepoManager(data_dir=str(tmp_path))
        return manager

    def _patch_job_manager(self, manager):
        """Patch background_job_manager.submit_job to return a fake job_id."""
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "fake-job-id"
        manager.background_job_manager = mock_bjm
        return mock_bjm

    def test_add_golden_repo_skips_validation_when_flag_true(self, tmp_path):
        """When skip_pre_flight_git_validation=True, _validate_git_repository NOT called."""
        manager = self._make_manager(tmp_path)
        self._patch_job_manager(manager)

        with (
            patch.object(
                manager, "_validate_git_repository", return_value=True
            ) as mock_validate,
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False

            manager.add_golden_repo(
                repo_url="https://github.com/org/repo.git",
                alias="my-alias",
                skip_pre_flight_git_validation=True,
            )

            mock_validate.assert_not_called()

    def test_add_golden_repo_runs_validation_by_default(self, tmp_path):
        """When skip_pre_flight_git_validation not supplied, _validate_git_repository IS called."""
        manager = self._make_manager(tmp_path)
        self._patch_job_manager(manager)

        with (
            patch.object(
                manager, "_validate_git_repository", return_value=True
            ) as mock_validate,
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False

            manager.add_golden_repo(
                repo_url="https://github.com/org/repo.git",
                alias="my-alias-2",
            )

            mock_validate.assert_called_once()


class TestGenerateUniqueAliasPrebuiltSet:
    """Tests for existing_aliases= parameter on generate_unique_alias()."""

    def test_generate_unique_alias_uses_prebuilt_set_when_provided(self):
        """When existing_aliases is given, list_golden_repos() must NOT be called."""
        from code_indexer.server.web.routes import generate_unique_alias

        mock_manager = MagicMock()
        # existing_aliases already contains "myrepo"
        result = generate_unique_alias(
            "myrepo", mock_manager, existing_aliases={"myrepo"}
        )

        mock_manager.list_golden_repos.assert_not_called()
        assert result == "myrepo-2"

    def test_generate_unique_alias_uses_prebuilt_set_no_conflict(self):
        """When existing_aliases is given and no conflict, returns base alias."""
        from code_indexer.server.web.routes import generate_unique_alias

        mock_manager = MagicMock()
        result = generate_unique_alias(
            "newrepo", mock_manager, existing_aliases={"otherrepo"}
        )

        mock_manager.list_golden_repos.assert_not_called()
        assert result == "newrepo"

    def test_generate_unique_alias_calls_list_golden_repos_when_no_set(self):
        """When existing_aliases is not provided, list_golden_repos() IS called."""
        from code_indexer.server.web.routes import generate_unique_alias

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []

        generate_unique_alias("myrepo", mock_manager)

        mock_manager.list_golden_repos.assert_called_once()

    def test_generate_unique_alias_prebuilt_set_multiple_collisions(self):
        """When existing_aliases has multiple collisions, suffix increments correctly."""
        from code_indexer.server.web.routes import generate_unique_alias

        mock_manager = MagicMock()
        result = generate_unique_alias(
            "myrepo",
            mock_manager,
            existing_aliases={"myrepo", "myrepo-2", "myrepo-3"},
        )

        mock_manager.list_golden_repos.assert_not_called()
        assert result == "myrepo-4"


class TestBatchCreateReposEfficiency:
    """Tests for batch efficiency: single list_golden_repos() call and validation skip."""

    def _make_repos(self, count: int):
        """Build a list of count dummy repo dicts."""
        return [
            {
                "clone_url": f"https://github.com/org/repo{i}.git",
                "alias": f"org/repo{i}",
                "branch": "main",
                "platform": "github",
            }
            for i in range(count)
        ]

    def test_batch_create_repos_calls_list_golden_repos_once_for_20_repo_batch(self):
        """list_golden_repos() is called exactly once regardless of batch size."""
        from code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.return_value = "job-abc"

        repos = self._make_repos(20)
        _batch_create_repos(repos, "admin", mock_manager)

        mock_manager.list_golden_repos.assert_called_once()

    def test_batch_create_repos_calls_list_golden_repos_once_for_single_repo(self):
        """list_golden_repos() is called exactly once even for a single repo."""
        from code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.return_value = "job-abc"

        repos = self._make_repos(1)
        _batch_create_repos(repos, "admin", mock_manager)

        mock_manager.list_golden_repos.assert_called_once()

    def test_batch_create_repos_passes_skip_validation_flag(self):
        """Every add_golden_repo() call receives skip_pre_flight_git_validation=True."""
        from code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        mock_manager.add_golden_repo.return_value = "job-xyz"

        repos = self._make_repos(5)
        _batch_create_repos(repos, "admin", mock_manager)

        assert mock_manager.add_golden_repo.call_count == 5
        for actual_call in mock_manager.add_golden_repo.call_args_list:
            kwargs = actual_call.kwargs
            assert kwargs.get("skip_pre_flight_git_validation") is True, (
                f"Expected skip_pre_flight_git_validation=True in call {actual_call}"
            )

    def test_batch_create_repos_empty_batch_no_list_call(self):
        """Empty batch: list_golden_repos() is still called once (pre-hoist)."""
        from code_indexer.server.web.routes import _batch_create_repos

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []

        _batch_create_repos([], "admin", mock_manager)

        # The hoist call happens before the loop so it is called once
        mock_manager.list_golden_repos.assert_called_once()
