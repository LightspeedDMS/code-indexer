"""
Unit tests for RefreshScheduler startup reconciliation (Story #236).

Tests the reconcile_golden_repos() method:
- AC4: Reverse CoW clone restores missing master from latest versioned snapshot
- AC5: Description generation queued for repos with missing descriptions
- AC6: Reconciliation is idempotent via marker file
- AC7: Reconciliation failures don't block startup

The reconciliation runs ONCE on server startup (idempotent via marker file).
It detects repos where golden-repos/{alias}/ is missing but .versioned/{alias}/v_*/
exists, and restores the master via reverse CoW clone (cp --reflink=auto -a).
"""

import pytest
from unittest.mock import Mock, patch

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    """Create RefreshScheduler with a mock registry (no snapshot_manager)."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


@pytest.fixture
def mock_clone_backend():
    """Module-level mock CloneBackend fixture for Story #1034 Commit 4 tests."""
    backend = Mock()
    backend.create_clone_at_path.return_value = "/restored/path"
    return backend


@pytest.fixture
def mock_snapshot_manager_with_backend(mock_clone_backend):
    """Module-level mock VersionedSnapshotManager with _clone_backend set."""
    sm = Mock()
    sm._clone_backend = mock_clone_backend
    return sm


@pytest.fixture
def scheduler_with_backend(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    mock_snapshot_manager_with_backend,
):
    """Create RefreshScheduler with an injected snapshot_manager (and thus clone_backend)."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
        snapshot_manager=mock_snapshot_manager_with_backend,
    )


def _make_subprocess_mock(cp_calls_list):
    """Return a mock subprocess.run that captures cp calls."""

    def mock_subprocess_run(cmd, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "cp":
            cp_calls_list.append(cmd)
        result = Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    return mock_subprocess_run


# ---------------------------------------------------------------------------
# AC6: Idempotency via marker file
# ---------------------------------------------------------------------------


class TestReconciliationIdempotency:
    """AC6: Reconciliation must be skipped when marker file already exists."""

    def test_reconcile_skipped_when_marker_file_exists(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC6: If marker file exists, reconciliation is skipped entirely.
        No subprocess calls, no filesystem operations.
        """
        marker = golden_repos_dir / ".reconciliation_complete_v1"
        marker.write_text("Completed at 2026-02-20T10:00:00")

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/repo.git"},
        ]

        with patch("subprocess.run") as mock_subprocess:
            scheduler.reconcile_golden_repos()

        mock_subprocess.assert_not_called()

    def test_reconcile_creates_marker_file_after_completion(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC6: After reconciliation completes, marker file must be created.
        """
        marker = golden_repos_dir / ".reconciliation_complete_v1"
        assert not marker.exists()

        mock_registry.list_global_repos.return_value = []

        scheduler.reconcile_golden_repos()

        assert marker.exists(), "Marker file must be created after reconciliation"
        content = marker.read_text()
        assert len(content) > 0, "Marker file must have content (timestamp)"

    def test_reconcile_runs_when_no_marker_file(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC6: Without marker file, reconciliation runs normally.
        Uses scheduler_with_backend (Story #1034 Commit 4) to verify clone_backend is called.
        """
        marker = golden_repos_dir / ".reconciliation_complete_v1"
        assert not marker.exists()

        (golden_repos_dir / ".versioned" / "my-repo" / "v_9000000").mkdir(
            parents=True, exist_ok=True
        )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        assert mock_clone_backend.create_clone_at_path.call_count >= 1, (
            "Reconciliation should have called create_clone_at_path without marker file"
        )


# ---------------------------------------------------------------------------
# AC4: Reverse CoW clone restores missing masters
# ---------------------------------------------------------------------------


class TestReverseCoWRestore:
    """AC4: Reverse CoW clone restores missing master from latest versioned snapshot."""

    def test_reconcile_skips_repos_with_existing_master(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: Repos with existing master directories are not restored.
        """
        (golden_repos_dir / "my-repo").mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        mock_clone_backend.create_clone_at_path.assert_not_called()

    def test_reconcile_restores_missing_master_via_reverse_cow(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: When master is missing but versioned copies exist,
        reverse CoW clone must be executed via CloneBackend.create_clone_at_path
        from latest versioned snapshot to master path.
        """
        repo_name = "my-repo"
        master_path = golden_repos_dir / repo_name
        assert not master_path.exists()

        versioned_dir = golden_repos_dir / ".versioned" / repo_name
        v1 = versioned_dir / "v_1000000"
        v2 = versioned_dir / "v_2000000"  # latest
        v1.mkdir(parents=True)
        v2.mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        mock_clone_backend.create_clone_at_path.assert_called_once_with(
            str(v2),
            str(master_path),
            preserve_attrs=True,
            timeout=600,
        )

    def test_reconcile_uses_latest_versioned_snapshot_as_source(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: When multiple versioned snapshots exist, the LATEST one
        (highest timestamp) must be used as the source for reverse CoW.
        """
        repo_name = "my-repo"
        versioned_dir = golden_repos_dir / ".versioned" / repo_name
        v_old = versioned_dir / "v_1000000"
        v_mid = versioned_dir / "v_1500000"
        v_latest = versioned_dir / "v_9999999"  # highest = latest
        v_old.mkdir(parents=True)
        v_mid.mkdir(parents=True)
        v_latest.mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        mock_clone_backend.create_clone_at_path.assert_called_once()
        call_args = mock_clone_backend.create_clone_at_path.call_args
        assert str(v_latest) == call_args.args[0], (
            f"Expected LATEST versioned dir {v_latest} as source, got: {call_args.args[0]}"
        )
        assert str(v_old) != call_args.args[0], (
            f"Old versioned dir {v_old} should not be used as source"
        )

    def test_reconcile_runs_fix_config_on_restored_master(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: After reverse CoW clone, cidx fix-config --force must be run
        on the restored master to fix .code-indexer/ paths.
        """
        repo_name = "my-repo"
        master_path = golden_repos_dir / repo_name

        (golden_repos_dir / ".versioned" / repo_name / "v_9999999").mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        fix_config_calls: list = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "fix-config" in cmd:
                fix_config_calls.append({"cmd": cmd, "cwd": kwargs.get("cwd", "")})
            result = Mock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            scheduler_with_backend.reconcile_golden_repos()

        assert len(fix_config_calls) >= 1, (
            "cidx fix-config --force must be called after reverse CoW clone"
        )
        fix_call = fix_config_calls[0]
        assert "--force" in fix_call["cmd"], (
            f"Expected --force in fix-config cmd: {fix_call['cmd']}"
        )
        assert str(master_path) in fix_call["cwd"], (
            f"fix-config must run in master dir {master_path}, got: {fix_call['cwd']}"
        )

    def test_reconcile_skips_repo_with_no_versioned_copies(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: If master is missing AND there are no versioned copies, skip.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "orphan-repo-global",
                "repo_url": "git@github.com:org/orphan-repo.git",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        mock_clone_backend.create_clone_at_path.assert_not_called()

    def test_reconcile_skips_local_repos(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: Local repos (repo_url starts with 'local://') are skipped.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local:///path/to/cidx-meta",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        mock_clone_backend.create_clone_at_path.assert_not_called()

    def test_reconcile_handles_multiple_repos_missing_masters(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: Multiple repos with missing masters are all restored.
        """
        repo_names = ["repo-a", "repo-b", "repo-c"]

        for repo_name in repo_names:
            (golden_repos_dir / ".versioned" / repo_name / "v_5000000").mkdir(
                parents=True
            )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": f"{repo_name}-global",
                "repo_url": f"git@github.com:org/{repo_name}.git",
            }
            for repo_name in repo_names
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        assert mock_clone_backend.create_clone_at_path.call_count == len(repo_names), (
            f"Expected {len(repo_names)} create_clone_at_path calls, "
            f"got {mock_clone_backend.create_clone_at_path.call_count}"
        )

    def test_reconcile_does_not_restore_repos_with_existing_masters(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC4: Mixed state - only repos without masters should be restored.
        """
        (golden_repos_dir / "has-master").mkdir(parents=True)
        (golden_repos_dir / ".versioned" / "no-master" / "v_9000000").mkdir(
            parents=True, exist_ok=True
        )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "has-master-global",
                "repo_url": "git@github.com:org/has-master.git",
            },
            {
                "alias_name": "no-master-global",
                "repo_url": "git@github.com:org/no-master.git",
            },
        ]

        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        assert mock_clone_backend.create_clone_at_path.call_count == 1, (
            f"Expected 1 create_clone_at_path call (only for no-master), "
            f"got {mock_clone_backend.create_clone_at_path.call_count}"
        )
        call_args = mock_clone_backend.create_clone_at_path.call_args
        assert "no-master" in call_args.args[1], (
            f"create_clone_at_path destination should reference no-master, got: {call_args.args[1]}"
        )


# ---------------------------------------------------------------------------
# AC5: Description generation queued for repos with missing descriptions
# ---------------------------------------------------------------------------


class TestDescriptionGeneration:
    """
    AC5: Reconciliation queues description generation for repos missing
    cidx-meta description files. This ensures repos restored from versioned
    snapshots get their descriptions generated.
    """

    def test_reconcile_queues_description_for_repo_missing_description(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC5: A repo with a master but missing cidx-meta description file
        has description generation queued via ClaudeCliManager.
        """
        repo_name = "my-repo"
        alias_name = f"{repo_name}-global"

        # Master exists
        (golden_repos_dir / repo_name).mkdir(parents=True)
        # cidx-meta description does NOT exist
        cidx_meta_dir = golden_repos_dir / "cidx-meta"
        assert not (cidx_meta_dir / f"{alias_name}.md").exists()

        mock_registry.list_global_repos.return_value = [
            {"alias_name": alias_name, "repo_url": "git@github.com:org/my-repo.git"},
        ]

        mock_claude_manager = Mock()
        mock_claude_manager.submit_work = Mock()

        with patch("subprocess.run"):
            scheduler.reconcile_golden_repos(claude_cli_manager=mock_claude_manager)

        # Description generation must be queued
        mock_claude_manager.submit_work.assert_called_once()

    def test_reconcile_does_not_queue_description_when_file_exists(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC5: When cidx-meta description file already exists, no queueing.
        """
        repo_name = "my-repo"
        alias_name = f"{repo_name}-global"

        (golden_repos_dir / repo_name).mkdir(parents=True)

        # Create the description file — cidx-meta uses SHORT alias (my-repo.md), not alias_name
        cidx_meta_dir = golden_repos_dir / "cidx-meta"
        cidx_meta_dir.mkdir(parents=True, exist_ok=True)
        (cidx_meta_dir / f"{repo_name}.md").write_text(
            "# Description\nThis is my-repo."
        )

        mock_registry.list_global_repos.return_value = [
            {"alias_name": alias_name, "repo_url": "git@github.com:org/my-repo.git"},
        ]

        mock_claude_manager = Mock()
        mock_claude_manager.submit_work = Mock()

        with patch("subprocess.run"):
            scheduler.reconcile_golden_repos(claude_cli_manager=mock_claude_manager)

        mock_claude_manager.submit_work.assert_not_called()

    def test_reconcile_queues_descriptions_for_multiple_repos(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC5: Multiple repos missing descriptions all get queued.
        """
        repo_names = ["repo-a", "repo-b", "repo-c"]

        for repo_name in repo_names:
            (golden_repos_dir / repo_name).mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": f"{repo_name}-global",
                "repo_url": f"git@github.com:org/{repo_name}.git",
            }
            for repo_name in repo_names
        ]

        mock_claude_manager = Mock()
        mock_claude_manager.submit_work = Mock()

        with patch("subprocess.run"):
            scheduler.reconcile_golden_repos(claude_cli_manager=mock_claude_manager)

        assert mock_claude_manager.submit_work.call_count == len(repo_names), (
            f"Expected {len(repo_names)} description queues, "
            f"got {mock_claude_manager.submit_work.call_count}"
        )

    def test_reconcile_skips_description_queue_when_no_claude_manager(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC5: When no ClaudeCliManager provided, description queueing is skipped
        gracefully (no error).
        """
        (golden_repos_dir / "my-repo").mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        # No claude_cli_manager provided - must not raise
        with patch("subprocess.run"):
            scheduler.reconcile_golden_repos()  # No claude_cli_manager arg


# ---------------------------------------------------------------------------
# AC7: Failure resilience
# ---------------------------------------------------------------------------


class TestReconciliationFailureResilience:
    """AC7: Reconciliation failures don't block startup or other repos."""

    def test_reconcile_failure_on_one_repo_does_not_block_others(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        AC7: If one repo's restoration fails, reconciliation continues
        for other repos and startup is not blocked.
        """
        repo_names = ["good-repo", "bad-repo", "another-good-repo"]

        for repo_name in repo_names:
            (golden_repos_dir / ".versioned" / repo_name / "v_9999999").mkdir(
                parents=True
            )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": f"{repo_name}-global",
                "repo_url": f"git@github.com:org/{repo_name}.git",
            }
            for repo_name in repo_names
        ]

        call_count = [0]

        def backend_side_effect(source, dest, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("Disk full simulation")
            return dest

        mock_clone_backend.create_clone_at_path.side_effect = backend_side_effect

        # Must not raise even when one repo fails
        with patch("subprocess.run"):
            scheduler_with_backend.reconcile_golden_repos()

        # All 3 attempted: 2 succeed, 1 fails mid-clone
        assert mock_clone_backend.create_clone_at_path.call_count >= 2, (
            f"Expected at least 2 create_clone_at_path attempts, "
            f"got {mock_clone_backend.create_clone_at_path.call_count}"
        )

        # Marker file must be created (overall reconciliation completed)
        marker = golden_repos_dir / ".reconciliation_complete_v1"
        assert marker.exists(), "Marker file must be created even if some repos fail"

    def test_reconcile_does_not_raise_on_exception(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC7: reconcile_golden_repos() must not propagate exceptions.
        Any failure must be logged and swallowed.
        """
        (golden_repos_dir / ".versioned" / "fail-repo" / "v_9999999").mkdir(
            parents=True, exist_ok=True
        )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "fail-repo-global",
                "repo_url": "git@github.com:org/fail-repo.git",
            },
        ]

        with patch("subprocess.run", side_effect=RuntimeError("catastrophic failure")):
            # Must not raise
            scheduler.reconcile_golden_repos()

        # Marker file should still be created
        marker = golden_repos_dir / ".reconciliation_complete_v1"
        assert marker.exists()


# ---------------------------------------------------------------------------
# Story #1034 Commit 4: _restore_master_from_versioned routes via CloneBackend
# ---------------------------------------------------------------------------


class TestRestoreMasterFromVersionedClonesViaBackend:
    """
    Story #1034 Commit 4: _restore_master_from_versioned must delegate the
    filesystem clone to CloneBackend.create_clone_at_path when snapshot_manager
    is injected, instead of calling subprocess.run directly.
    """

    def test_restore_uses_clone_backend_create_clone_at_path(
        self,
        scheduler_with_backend,
        golden_repos_dir,
        mock_registry,
        mock_clone_backend,
    ):
        """
        Story #1034 Commit 4: when snapshot_manager is injected, restore must
        call clone_backend.create_clone_at_path(latest_version, master_path,
        preserve_attrs=True, timeout=<cow_timeout>) instead of subprocess.run.
        """
        repo_name = "my-repo"
        master_path = golden_repos_dir / repo_name
        assert not master_path.exists()

        versioned_dir = golden_repos_dir / ".versioned" / repo_name
        v_old = versioned_dir / "v_1000000"
        v_latest = versioned_dir / "v_9000000"
        v_old.mkdir(parents=True)
        v_latest.mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        with patch("subprocess.run") as mock_subprocess:
            scheduler_with_backend.reconcile_golden_repos()

        mock_clone_backend.create_clone_at_path.assert_called_once_with(
            str(v_latest),
            str(master_path),
            preserve_attrs=True,
            timeout=600,
        )
        # Direct subprocess.run for cp must NOT be called (backend handles it)
        cp_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list) and c.args[0][:1] == ["cp"]
        ]
        assert len(cp_calls) == 0, (
            f"subprocess.run cp must not be called when backend is used, got: {cp_calls}"
        )

    def test_restore_without_snapshot_manager_still_raises_on_missing_backend(
        self,
        scheduler,
        golden_repos_dir,
        mock_registry,
    ):
        """
        Story #1034 Commit 4: when snapshot_manager is None (no backend injection),
        _restore_master_from_versioned raises RuntimeError (wiring bug guard) and
        reconcile_golden_repos swallows it (AC7 resilience).
        The marker file is still created (reconciliation completed, just with failures).
        """
        repo_name = "my-repo"
        (golden_repos_dir / ".versioned" / repo_name / "v_9000000").mkdir(
            parents=True, exist_ok=True
        )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_url": "git@github.com:org/my-repo.git",
            },
        ]

        # No subprocess mock — should raise RuntimeError inside restore,
        # which reconcile_golden_repos swallows per AC7.
        scheduler.reconcile_golden_repos()

        marker = golden_repos_dir / ".reconciliation_complete_v1"
        assert marker.exists(), (
            "Marker file must still be created after swallowed failure"
        )
