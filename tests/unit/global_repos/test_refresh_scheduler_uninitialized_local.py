"""
Unit tests for Bug #268: RefreshScheduler skips uninitialized local repos.

Per-user Langfuse repos (e.g. langfuse_User_Name-global) are local:// repos
that may not have .code-indexer/ initialized yet when the scheduler fires.

Before this fix, _execute_refresh() called _has_local_changes() which returned
True (no versioned dirs = first version needed), then _index_source() ran
`cidx index` which failed with:
  "Command 'index' is not available in no configuration found - project needs
  initialization."

Fix: In _execute_refresh(), when handling a local repo, check if the source
directory has .code-indexer/ initialized. If not, log and return gracefully
instead of attempting to index.

Acceptance Criteria:
AC1: _execute_refresh() returns a skipped/graceful result (success=True,
     not an exception) for uninitialized local repos (no .code-indexer/).
AC2: _execute_refresh() still proceeds normally for initialized local repos
     (those with .code-indexer/ present).
AC3: No failed jobs accumulate for uninitialized local repos.
     (_index_source() is never called for uninitialized repos).

Note: The scheduler loop STILL submits local repos (Story #224 behavior
preserved). The graceful skip happens inside _execute_refresh(), not in
_scheduler_loop().
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


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
    registry.update_refresh_timestamp = MagicMock()
    registry.update_enable_temporal = MagicMock()
    registry.update_enable_scip = MagicMock()
    return registry


@pytest.fixture
def mock_config_source():
    """Config source mock."""
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


def _make_repo_info(alias_name: str, repo_url: str = "local://langfuse-user"):
    """Build a minimal repo_info dict for a local repo."""
    return {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "enable_temporal": False,
        "enable_scip": False,
    }


# ---------------------------------------------------------------------------
# AC1: Uninitialized local repos are skipped gracefully
# ---------------------------------------------------------------------------


class TestUninitializedLocalRepoSkippedGracefully:
    """
    AC1: _execute_refresh() must return success=True (not raise) for local repos
    whose source directory has no .code-indexer/ initialization.
    """

    def test_uninitialized_local_repo_returns_success_not_raises(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When a local repo source dir exists but has no .code-indexer/,
        _execute_refresh() must return {"success": True, ...} without raising.

        Before the fix: _has_local_changes() returns True (no versioned dirs),
        then _index_source() calls `cidx index` which raises RuntimeError
        "no configuration found".

        After the fix: the uninitialized state is detected before calling
        _index_source(), and a graceful skip result is returned.
        """
        alias_name = "langfuse-user-global"
        # Source dir EXISTS but has no .code-indexer/ inside
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        # Deliberately no .code-indexer/ created here

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        # The test must NOT call _index_source() (which would fail) nor raise.
        # We capture calls to _index_source to verify it is never called.
        index_source_calls = []

        def fail_if_called(*args, **kwargs):
            index_source_calls.append(args)
            raise RuntimeError(
                "cidx index: Command 'index' is not available in "
                "no configuration found - project needs initialization."
            )

        with patch.object(scheduler, "_index_source", side_effect=fail_if_called):
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    # Must NOT raise - must return a success dict
                    result = scheduler._execute_refresh(alias_name)

        assert result["success"] is True, (
            "AC1: _execute_refresh() must return success=True for uninitialized "
            "local repos, not raise an exception."
        )
        assert index_source_calls == [], (
            "AC1: _index_source() must NOT be called for uninitialized local repos. "
            "The uninitialized state should be detected before reaching indexing."
        )

    def test_uninitialized_local_repo_message_indicates_skipped(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        The result message for an uninitialized local repo must indicate
        it was skipped due to not being initialized (not a generic failure).
        """
        alias_name = "langfuse-user-global"
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        # No .code-indexer/ — uninitialized

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        with patch.object(scheduler, "_index_source"):
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    result = scheduler._execute_refresh(alias_name)

        message = result.get("message", "").lower()
        # The message should convey that initialization is missing
        # Accept any of: "not initialized", "uninitialized", "no .code-indexer",
        # "skipped", "initialization"
        skip_keywords = ["not initialized", "uninitialized", "no .code-indexer",
                         "skipped", "initialization", "not yet initialized"]
        assert any(kw in message for kw in skip_keywords), (
            f"AC1: Result message '{result.get('message')}' does not indicate "
            f"that the local repo was skipped due to missing initialization. "
            f"Expected one of: {skip_keywords}"
        )

    def test_langfuse_user_alias_pattern_is_skipped_when_uninitialized(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Specifically test the Langfuse per-user alias pattern mentioned in Bug #268:
        langfuse_{User}_{email}-global repos must be skipped gracefully when
        their source dir has no .code-indexer/.
        """
        alias_name = "langfuse_Claude_Code_seba.battig_lightspeeddms.com-global"
        repo_name = alias_name.replace("-global", "")
        source_dir = golden_repos_dir / repo_name
        source_dir.mkdir(parents=True)
        # No .code-indexer/ — this is the exact failure scenario from Bug #268

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "local://langfuse_Claude_Code_seba.battig_lightspeeddms.com",
            "enable_temporal": False,
            "enable_scip": False,
        }
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        index_source_calls = []

        with patch.object(
            scheduler, "_index_source",
            side_effect=lambda *a, **k: index_source_calls.append(a)
        ):
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    result = scheduler._execute_refresh(alias_name)

        assert result["success"] is True, (
            "AC1: Langfuse per-user repo must not fail when .code-indexer/ is absent."
        )
        assert index_source_calls == [], (
            "AC1: _index_source() must not be called for uninitialized Langfuse repos."
        )


# ---------------------------------------------------------------------------
# AC2: Initialized local repos still proceed normally
# ---------------------------------------------------------------------------


class TestInitializedLocalRepoProceedsNormally:
    """
    AC2: Local repos WITH .code-indexer/ initialized must still go through
    the full refresh cycle (no regression from the Bug #268 fix).
    """

    def test_initialized_local_repo_calls_has_local_changes(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When a local repo has .code-indexer/ initialized, _execute_refresh()
        must call _has_local_changes() as before (no regression).
        """
        alias_name = "cidx-meta-global"
        source_dir = golden_repos_dir / "cidx-meta"
        source_dir.mkdir(parents=True)
        # Create .code-indexer/ to mark as initialized
        (source_dir / ".code-indexer").mkdir()

        repo_info = _make_repo_info(alias_name, repo_url="local://cidx-meta")
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        mtime_calls = []

        def capture_mtime(src, alias):
            mtime_calls.append((src, alias))
            return False  # No changes

        with patch.object(scheduler, "_has_local_changes", side_effect=capture_mtime):
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    result = scheduler._execute_refresh(alias_name)

        assert len(mtime_calls) == 1, (
            "AC2: _has_local_changes() must be called for initialized local repos. "
            "The Bug #268 fix must not skip initialized repos."
        )
        assert result["success"] is True

    def test_initialized_local_repo_with_changes_calls_index_source(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When a local repo is initialized AND has changes, _index_source() IS called.
        This verifies the full indexing path is preserved for initialized repos.
        """
        alias_name = "cidx-meta-global"
        source_dir = golden_repos_dir / "cidx-meta"
        source_dir.mkdir(parents=True)
        # Mark as initialized
        (source_dir / ".code-indexer").mkdir()
        # Add a file so there are changes
        (source_dir / "test.md").write_text("# test content")

        repo_info = _make_repo_info(alias_name, repo_url="local://cidx-meta")
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        index_source_calls = []

        def capture_index_source(alias_name, source_path):
            index_source_calls.append((alias_name, source_path))
            raise RuntimeError("Stop after capture")  # Prevent full execution

        with patch.object(scheduler, "_has_local_changes", return_value=True):
            with patch.object(
                scheduler, "_index_source", side_effect=capture_index_source
            ):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(
                        scheduler, "_reconcile_registry_with_filesystem"
                    ):
                        with pytest.raises(RuntimeError):
                            scheduler._execute_refresh(alias_name)

        assert len(index_source_calls) == 1, (
            "AC2: _index_source() must be called for initialized local repos with changes. "
            "The Bug #268 fix must not prevent indexing for properly initialized repos."
        )

    def test_git_repo_is_unaffected_by_bug268_fix(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Git repos must be completely unaffected by the Bug #268 fix.
        Git repos never have the local:// prefix so no initialization check applies.
        """
        alias_name = "some-repo-global"
        source_dir = golden_repos_dir / "some-repo"
        source_dir.mkdir(parents=True)
        # Git repos may or may not have .code-indexer/ — doesn't matter for this check

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/repo.git",
            "enable_temporal": False,
            "enable_scip": False,
        }
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = False

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=mock_updater,
        ) as mock_git_cls:
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    result = scheduler._execute_refresh(alias_name)

        mock_git_cls.assert_called_once(), (
            "AC2 (regression): GitPullUpdater must still be used for git repos."
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# AC3: No failed jobs accumulate for uninitialized local repos
# ---------------------------------------------------------------------------


class TestNoFailedJobsForUninitializedLocalRepos:
    """
    AC3: _index_source() must never be called for uninitialized local repos,
    preventing the "no configuration found" error from being raised and
    preventing failed jobs from accumulating in BackgroundJobManager.
    """

    def test_no_exception_raised_for_uninitialized_local_repo(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        _execute_refresh() must not raise ANY exception for uninitialized local repos.

        Before the fix: RuntimeError is raised from _index_source() when
        `cidx index` fails with "no configuration found".
        After the fix: graceful return, no exception.
        """
        alias_name = "langfuse-user-global"
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        # No .code-indexer/ = uninitialized

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        with patch.object(
            scheduler, "_detect_existing_indexes", return_value={}
        ):
            with patch.object(
                scheduler, "_reconcile_registry_with_filesystem"
            ):
                # This must NOT raise - any exception would cause a failed job
                try:
                    result = scheduler._execute_refresh(alias_name)
                except Exception as exc:
                    pytest.fail(
                        f"AC3: _execute_refresh() raised {type(exc).__name__}: {exc}. "
                        f"Uninitialized local repos must return gracefully, not raise. "
                        f"Failed jobs accumulate in BackgroundJobManager when exceptions "
                        f"are raised (see Bug #84 fix in refresh_scheduler.py line 721)."
                    )

        assert result["success"] is True

    def test_multiple_uninitialized_local_repos_none_fail(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When multiple uninitialized local repos exist, all must return gracefully.
        This simulates the production scenario where several per-user Langfuse repos
        exist but none have been written to yet.
        """
        aliases = [
            "langfuse_User1_email1_com-global",
            "langfuse_User2_email2_com-global",
            "langfuse_User3_email3_com-global",
        ]

        # Create all source dirs without .code-indexer/
        for alias in aliases:
            repo_name = alias.replace("-global", "")
            (golden_repos_dir / repo_name).mkdir(parents=True)

        results = []
        for alias in aliases:
            repo_name = alias.replace("-global", "")
            source_dir = golden_repos_dir / repo_name
            repo_info = {
                "alias_name": alias,
                "repo_url": f"local://{repo_name}",
                "enable_temporal": False,
                "enable_scip": False,
            }
            mock_registry.get_global_repo.return_value = repo_info
            scheduler.alias_manager.read_alias = MagicMock(
                return_value=str(source_dir)
            )

            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    try:
                        result = scheduler._execute_refresh(alias)
                        results.append(result)
                    except Exception as exc:
                        pytest.fail(
                            f"AC3: _execute_refresh() raised for uninitialized repo "
                            f"{alias}: {type(exc).__name__}: {exc}"
                        )

        assert len(results) == 3, "All 3 uninitialized repos must return a result"
        for result in results:
            assert result["success"] is True, (
                f"AC3: All uninitialized repos must return success=True. Got: {result}"
            )

    def test_scheduler_loop_does_not_produce_failures_for_uninitialized_local_repos(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When the scheduler loop fires with a mix of initialized and uninitialized
        local repos, the uninitialized ones must complete without raising exceptions.

        This tests the end-to-end scenario from Bug #268:
        - cidx-meta is initialized (has .code-indexer/) — processes normally
        - langfuse user repo is not initialized — must skip gracefully

        The scheduler loop catches exceptions and logs them, but we verify
        that _execute_refresh() itself does not raise for uninitialized repos.
        """
        # cidx-meta: initialized
        cidx_meta_dir = golden_repos_dir / "cidx-meta"
        cidx_meta_dir.mkdir(parents=True)
        (cidx_meta_dir / ".code-indexer").mkdir()

        # Langfuse user repo: NOT initialized
        langfuse_dir = golden_repos_dir / "langfuse-user"
        langfuse_dir.mkdir(parents=True)
        # Deliberately no .code-indexer/

        repos = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local://cidx-meta",
                "enable_temporal": False,
                "enable_scip": False,
            },
            {
                "alias_name": "langfuse-user-global",
                "repo_url": "local://langfuse-user",
                "enable_temporal": False,
                "enable_scip": False,
            },
        ]

        exception_raised_for = []

        # Patch _execute_refresh to track exceptions for each alias
        original_execute_refresh = scheduler._execute_refresh

        def tracked_execute_refresh(alias_name):
            # Wire up the read_alias based on alias
            if alias_name == "cidx-meta-global":
                scheduler.alias_manager.read_alias = MagicMock(
                    return_value=str(cidx_meta_dir)
                )
                mock_registry.get_global_repo.return_value = repos[0]
            else:
                scheduler.alias_manager.read_alias = MagicMock(
                    return_value=str(langfuse_dir)
                )
                mock_registry.get_global_repo.return_value = repos[1]

            try:
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(
                        scheduler, "_reconcile_registry_with_filesystem"
                    ):
                        with patch.object(
                            scheduler, "_has_local_changes", return_value=False
                        ):
                            return original_execute_refresh(alias_name)
            except Exception as exc:
                exception_raised_for.append((alias_name, exc))
                raise

        mock_registry.list_global_repos.return_value = repos

        # Simulate the scheduler loop's behavior for both repos
        for repo in repos:
            alias = repo["alias_name"]
            try:
                tracked_execute_refresh(alias)
            except Exception:
                pass  # Expected for uninitialized repos BEFORE the fix

        # After the fix: no exceptions should be raised for langfuse-user-global
        uninitialized_failures = [
            (alias, exc) for alias, exc in exception_raised_for
            if "langfuse" in alias
        ]
        assert uninitialized_failures == [], (
            f"AC3: Uninitialized local repos must not raise exceptions in the "
            f"scheduler loop. Got failures: {uninitialized_failures}"
        )
