"""
Unit tests for RefreshScheduler._resolve_global_alias() and
trigger_refresh_for_repo() alias resolution.

TDD Red Phase: These tests define the expected behavior for:
  - _resolve_global_alias() accepting bare alias ("my-repo") -> "my-repo-global"
  - _resolve_global_alias() accepting already-global alias ("my-repo-global") -> "my-repo-global"
  - _resolve_global_alias() raising ValueError for nonexistent repos
  - trigger_refresh_for_repo() accepting bare alias and resolving internally

This keeps the -global suffix convention encapsulated in the scheduler layer,
not in callers (handlers, REST endpoints, Web UI).
"""

from unittest.mock import patch, MagicMock, call

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a golden repos directory structure."""
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    return golden_repos_dir


@pytest.fixture
def config_mgr(tmp_path):
    """Create a ConfigManager instance."""
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    """Create a QueryTracker instance."""
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    """Create a CleanupManager instance."""
    return CleanupManager(query_tracker)


@pytest.fixture
def mock_background_job_manager():
    """Create a mock BackgroundJobManager."""
    manager = MagicMock()
    manager.submit_job = MagicMock(return_value="resolve-job-001")
    return manager


def _make_scheduler(golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
                    background_job_manager=None, registry=None):
    """Helper to create a RefreshScheduler with optional mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        background_job_manager=background_job_manager,
        registry=registry,
    )


class TestResolveGlobalAlias:
    """Tests for RefreshScheduler._resolve_global_alias().

    This method must:
    - Accept bare alias ("my-repo") and return "my-repo-global" if found in registry
    - Accept global alias ("my-repo-global") as-is if directly found in registry
    - Raise ValueError for aliases not found in either format
    """

    def test_resolve_bare_alias_returns_global_form(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """_resolve_global_alias('my-repo') must return 'my-repo-global' when found."""
        mock_registry = MagicMock()
        # Bare alias NOT found in registry (get_global_repo returns None)
        # Global alias IS found
        def registry_get_global_repo(alias_name):
            if alias_name == "my-repo-global":
                return {"alias_name": "my-repo-global"}
            return None

        mock_registry.get_global_repo = MagicMock(side_effect=registry_get_global_repo)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            registry=mock_registry
        )

        result = scheduler._resolve_global_alias("my-repo")

        assert result == "my-repo-global"

    def test_resolve_already_global_alias_returns_as_is(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """_resolve_global_alias('my-repo-global') must return 'my-repo-global' unchanged."""
        mock_registry = MagicMock()
        # Global alias IS found directly
        mock_registry.get_global_repo = MagicMock(
            return_value={"alias_name": "my-repo-global"}
        )

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            registry=mock_registry
        )

        result = scheduler._resolve_global_alias("my-repo-global")

        assert result == "my-repo-global"

    def test_resolve_unknown_alias_raises_value_error(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """_resolve_global_alias('nonexistent') must raise ValueError."""
        mock_registry = MagicMock()
        # Neither bare nor global form found
        mock_registry.get_global_repo = MagicMock(return_value=None)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            registry=mock_registry
        )

        with pytest.raises(ValueError, match="nonexistent"):
            scheduler._resolve_global_alias("nonexistent")

    def test_resolve_checks_bare_alias_first(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """_resolve_global_alias must try the alias as-is first (already-global fast path)."""
        mock_registry = MagicMock()
        # Already-global alias found on first try
        mock_registry.get_global_repo = MagicMock(
            return_value={"alias_name": "cidx-meta-global"}
        )

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            registry=mock_registry
        )

        result = scheduler._resolve_global_alias("cidx-meta-global")

        # Only one registry call needed (as-is, no suffix appended)
        mock_registry.get_global_repo.assert_called_once_with("cidx-meta-global")
        assert result == "cidx-meta-global"

    def test_resolve_bare_alias_includes_alias_in_error_message(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """ValueError message must include the original alias for clear diagnostics."""
        mock_registry = MagicMock()
        mock_registry.get_global_repo = MagicMock(return_value=None)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            registry=mock_registry
        )

        with pytest.raises(ValueError) as exc_info:
            scheduler._resolve_global_alias("no-such-repo")

        assert "no-such-repo" in str(exc_info.value)


class TestTriggerRefreshAcceptsBareAlias:
    """Tests that trigger_refresh_for_repo() accepts bare alias and resolves internally.

    After the refactor, callers (handlers, REST, Web UI) pass bare alias.
    The resolution happens inside trigger_refresh_for_repo().
    """

    def test_trigger_refresh_with_bare_alias_submits_global_alias_to_bjm(
        self,
        golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
        mock_background_job_manager
    ):
        """trigger_refresh_for_repo('my-repo') must resolve and submit 'my-repo-global' to BJM."""
        mock_registry = MagicMock()

        def registry_get_global_repo(alias_name):
            if alias_name == "my-repo-global":
                return {"alias_name": "my-repo-global"}
            return None

        mock_registry.get_global_repo = MagicMock(side_effect=registry_get_global_repo)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            background_job_manager=mock_background_job_manager,
            registry=mock_registry,
        )

        with patch.object(scheduler, "_submit_refresh_job", return_value="job-bare-001") as mock_submit:
            job_id = scheduler.trigger_refresh_for_repo("my-repo")

        # Must call _submit_refresh_job with the resolved global alias
        mock_submit.assert_called_once_with("my-repo-global", submitter_username="system", force_reset=False)
        assert job_id == "job-bare-001"

    def test_trigger_refresh_with_global_alias_passes_through(
        self,
        golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
        mock_background_job_manager
    ):
        """trigger_refresh_for_repo('my-repo-global') passes through unchanged."""
        mock_registry = MagicMock()
        mock_registry.get_global_repo = MagicMock(
            return_value={"alias_name": "my-repo-global"}
        )

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            background_job_manager=mock_background_job_manager,
            registry=mock_registry,
        )

        with patch.object(scheduler, "_submit_refresh_job", return_value="job-global-001") as mock_submit:
            job_id = scheduler.trigger_refresh_for_repo("my-repo-global")

        mock_submit.assert_called_once_with("my-repo-global", submitter_username="system", force_reset=False)
        assert job_id == "job-global-001"

    def test_trigger_refresh_with_nonexistent_alias_raises_value_error(
        self,
        golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
        mock_background_job_manager
    ):
        """trigger_refresh_for_repo('bad-alias') must raise ValueError for unknown repo."""
        mock_registry = MagicMock()
        mock_registry.get_global_repo = MagicMock(return_value=None)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            background_job_manager=mock_background_job_manager,
            registry=mock_registry,
        )

        with pytest.raises(ValueError, match="bad-alias"):
            scheduler.trigger_refresh_for_repo("bad-alias")

    def test_trigger_refresh_returns_job_id_from_bjm_with_bare_alias(
        self,
        golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
        mock_background_job_manager
    ):
        """trigger_refresh_for_repo must return the job_id from BackgroundJobManager."""
        mock_registry = MagicMock()

        def registry_get_global_repo(alias_name):
            if alias_name == "code-indexer-global":
                return {"alias_name": "code-indexer-global"}
            return None

        mock_registry.get_global_repo = MagicMock(side_effect=registry_get_global_repo)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            background_job_manager=mock_background_job_manager,
            registry=mock_registry,
        )

        job_id = scheduler.trigger_refresh_for_repo("code-indexer")

        assert job_id == "resolve-job-001"
        mock_background_job_manager.submit_job.assert_called_once()
        # Verify the resolved global alias was passed to submit_job
        call_kwargs = mock_background_job_manager.submit_job.call_args
        assert call_kwargs.kwargs["repo_alias"] == "code-indexer-global"

    def test_trigger_refresh_with_bare_alias_no_bjm_calls_execute_refresh(
        self,
        golden_repos_dir, config_mgr, query_tracker, cleanup_manager
    ):
        """In CLI mode (no BJM), bare alias resolves and _execute_refresh is called."""
        mock_registry = MagicMock()

        def registry_get_global_repo(alias_name):
            if alias_name == "cli-repo-global":
                return {"alias_name": "cli-repo-global"}
            return None

        mock_registry.get_global_repo = MagicMock(side_effect=registry_get_global_repo)

        scheduler = _make_scheduler(
            golden_repos_dir, config_mgr, query_tracker, cleanup_manager,
            registry=mock_registry,
        )

        with patch.object(scheduler, "_execute_refresh") as mock_execute:
            result = scheduler.trigger_refresh_for_repo("cli-repo")

        mock_execute.assert_called_once_with("cli-repo-global", force_reset=False)
        assert result is None
