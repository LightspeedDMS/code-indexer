"""
Unit tests for local repo complete skip from scheduled auto-refresh cycle.

REQUIREMENT: Local repos (those with repo_url starting with "local://" or any
bare filesystem path that is NOT https://, git@, ssh://, or git://) must be
COMPLETELY skipped from the scheduled refresh cycle.

This means:
- NO job submission for local repos
- NO reconciliation for local repos
- NO change detection for local repos
- NO indexing for local repos

Local repos are ONLY refreshed when explicitly triggered via:
- trigger_refresh_for_repo(alias_name)

This replaces Story #224 C1 behavior: local repos were previously submitted
to _submit_refresh_job() in _scheduler_loop(). This caused:
- Repos with .code-indexer/ but no config.json -> "Changes detected" ->
  `cidx index` -> FAILS with "no configuration found"
- Repos with valid config -> "No changes detected" -> wasteful job submission,
  reconciliation, and change detection

APPROACH: The skip happens at the iteration point in _scheduler_loop(),
NOT inside _execute_refresh(). This prevents even submitting a background
job for local repos.

Test Scenarios covered here (Scenarios 1-3):
1. Local repos (local://) are NOT submitted in scheduled refresh cycle
2. Repos with bare filesystem paths are also NOT submitted
3. Local repos ARE refreshed when trigger_refresh_for_repo() is called explicitly
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Golden repos directory."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_registry():
    """Registry mock with sensible defaults."""
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    return registry


@pytest.fixture
def mock_config_source():
    """Config source mock - short interval so tests don't hang."""
    cs = MagicMock()
    cs.get_global_refresh_interval.return_value = 3600
    return cs


@pytest.fixture
def scheduler(golden_repos_dir, mock_registry, mock_config_source):
    """RefreshScheduler with injected mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# Helper: run one pass of scheduler loop then stop
# ---------------------------------------------------------------------------


def _run_one_loop_pass(scheduler):
    """Run exactly one iteration of _scheduler_loop() then stop."""
    original_wait = scheduler._stop_event.wait

    def stop_after_one(timeout=None):
        scheduler._running = False
        return True

    scheduler._running = True
    scheduler._stop_event.clear()
    scheduler._stop_event.wait = stop_after_one
    try:
        scheduler._scheduler_loop()
    finally:
        scheduler._stop_event.wait = original_wait


# ---------------------------------------------------------------------------
# Scenario 1: local:// repos are NOT submitted in scheduled refresh cycle
# ---------------------------------------------------------------------------


class TestLocalRepoNotSubmittedInScheduledCycle:
    """
    Scenario 1: Local repos with local:// URL must not be submitted to
    _submit_refresh_job() in the scheduled _scheduler_loop().
    """

    def test_local_repo_not_submitted_in_scheduler_loop(
        self, scheduler, mock_registry
    ):
        """
        _scheduler_loop() must NOT call _submit_refresh_job for local:// repos.

        This is the key behavioral change: local repos are skipped at the
        iteration level, before any job submission occurs.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local://cidx-meta",
            }
        ]

        submitted = []

        with patch.object(
            scheduler, "_submit_refresh_job",
            side_effect=lambda a, **kw: submitted.append(a)
        ):
            _run_one_loop_pass(scheduler)

        assert "cidx-meta-global" not in submitted, (
            "LOCAL REPO SKIP: local:// repos must NOT be submitted to "
            "_submit_refresh_job() in _scheduler_loop(). "
            "The scheduler must skip local repos at the iteration level."
        )

    def test_langfuse_local_repo_not_submitted(self, scheduler, mock_registry):
        """
        Per-user Langfuse repos (local://) must also be skipped from scheduled
        refresh. These are the repos causing failures: they have local:// URLs
        and may not be initialized, causing wasteful job submissions and failures.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "langfuse-user-global",
                "repo_url": "local://langfuse-user",
            },
            {
                "alias_name": "langfuse_Claude_email_com-global",
                "repo_url": "local://langfuse_Claude_email_com",
            },
        ]

        submitted = []

        with patch.object(
            scheduler, "_submit_refresh_job",
            side_effect=lambda a, **kw: submitted.append(a)
        ):
            _run_one_loop_pass(scheduler)

        assert submitted == [], (
            f"LOCAL REPO SKIP: No local:// repos should be submitted. "
            f"Got submissions: {submitted}"
        )

    def test_local_repo_skip_logs_info_message(
        self, scheduler, mock_registry, caplog
    ):
        """
        When a local:// repo is skipped, an INFO log message must be emitted
        so operators can see the skip in server logs.
        """
        import logging

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local://cidx-meta",
            }
        ]

        with patch.object(scheduler, "_submit_refresh_job"):
            with caplog.at_level(logging.INFO, logger="code_indexer"):
                _run_one_loop_pass(scheduler)

        # Some log message should mention the local repo being skipped
        all_log_text = " ".join(caplog.messages).lower()
        assert "cidx-meta" in all_log_text or "local" in all_log_text, (
            "LOCAL REPO SKIP: A log message must be emitted when a local repo "
            "is skipped from the scheduled refresh cycle."
        )


# ---------------------------------------------------------------------------
# Scenario 2: Bare filesystem path repos are also skipped
# ---------------------------------------------------------------------------


class TestBareFilesystemPathRepoNotSubmitted:
    """
    Scenario 2: Repos with bare filesystem paths (not https://, git@, etc.)
    must also be skipped from scheduled refresh, just like local:// repos.

    Examples: /home/.../scip-python-mock, /tmp/delegation-functions-test
    """

    def test_bare_filesystem_path_not_submitted(self, scheduler, mock_registry):
        """
        Repos with bare filesystem paths (no git:// prefix) must be skipped.

        These appear in golden_repos_metadata when repos are registered via
        filesystem paths rather than git remote URLs.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "scip-python-mock-global",
                "repo_url": "/home/user/dev/scip-python-mock",
            },
            {
                "alias_name": "delegation-test-global",
                "repo_url": "/tmp/delegation-functions-test",
            },
        ]

        submitted = []

        with patch.object(
            scheduler, "_submit_refresh_job",
            side_effect=lambda a, **kw: submitted.append(a)
        ):
            _run_one_loop_pass(scheduler)

        assert submitted == [], (
            f"BARE PATH SKIP: Repos with bare filesystem paths must NOT be submitted "
            f"to scheduled refresh. Got submissions: {submitted}"
        )

    def test_empty_repo_url_not_submitted(self, scheduler, mock_registry):
        """
        Repos with empty or missing repo_url must also be skipped.
        These cannot be git repos and should be treated as local.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "unknown-repo-global",
                "repo_url": "",
            },
            {
                "alias_name": "no-url-repo-global",
                # repo_url key missing entirely
            },
        ]

        submitted = []

        with patch.object(
            scheduler, "_submit_refresh_job",
            side_effect=lambda a, **kw: submitted.append(a)
        ):
            _run_one_loop_pass(scheduler)

        assert submitted == [], (
            f"EMPTY URL SKIP: Repos with empty/missing repo_url must NOT be submitted. "
            f"Got submissions: {submitted}"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Local repos ARE refreshed via explicit trigger_refresh_for_repo()
# ---------------------------------------------------------------------------


class TestLocalRepoExplicitTriggerStillWorks:
    """
    Scenario 3: Even though local repos are skipped in the scheduled cycle,
    they MUST still be refreshable via explicit trigger_refresh_for_repo() calls.

    This is the writer service path: DependencyMapService, LangfuseTraceSyncService,
    and MetaDescriptionHook trigger refreshes explicitly after writing.
    """

    def test_trigger_refresh_for_repo_works_for_local_repo(
        self, golden_repos_dir, mock_config_source
    ):
        """
        trigger_refresh_for_repo() must work for local:// repos.

        The scheduled skip must NOT affect explicit trigger calls.
        """
        registry = GlobalRegistry(str(golden_repos_dir))
        alias_manager = AliasManager(str(golden_repos_dir / "aliases"))

        local_dir = golden_repos_dir / "cidx-meta"
        local_dir.mkdir(parents=True)
        alias_manager.create_alias("cidx-meta-global", str(local_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",
            str(local_dir),
            allow_reserved=True,
        )

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=registry,
        )

        with patch.object(sched, "_execute_refresh") as mock_execute:
            mock_execute.return_value = {"success": True, "message": "Refresh complete"}
            sched.trigger_refresh_for_repo("cidx-meta-global")

        mock_execute.assert_called_once_with("cidx-meta-global"), (
            "EXPLICIT TRIGGER: trigger_refresh_for_repo() must call _execute_refresh "
            "for local:// repos. Explicit triggers bypass the scheduled skip."
        )

    def test_trigger_refresh_accepts_bare_alias_for_local_repo(
        self, golden_repos_dir, mock_config_source
    ):
        """
        trigger_refresh_for_repo() with bare alias (no -global suffix) also works
        for local repos.
        """
        registry = GlobalRegistry(str(golden_repos_dir))
        alias_manager = AliasManager(str(golden_repos_dir / "aliases"))

        local_dir = golden_repos_dir / "cidx-meta"
        local_dir.mkdir(parents=True)
        alias_manager.create_alias("cidx-meta-global", str(local_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",
            str(local_dir),
            allow_reserved=True,
        )

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=registry,
        )

        with patch.object(sched, "_execute_refresh") as mock_execute:
            mock_execute.return_value = {"success": True}
            sched.trigger_refresh_for_repo("cidx-meta")

        mock_execute.assert_called_once()
