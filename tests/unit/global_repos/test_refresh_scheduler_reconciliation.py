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
from pathlib import Path
from unittest.mock import Mock, patch, call

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
    """Create RefreshScheduler with a mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
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
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC6: Without marker file, reconciliation runs normally.
        """
        marker = golden_repos_dir / ".reconciliation_complete_v1"
        assert not marker.exists()

        (golden_repos_dir / ".versioned" / "my-repo" / "v_9000000").mkdir(
            parents=True, exist_ok=True
        )

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(cp_calls) >= 1, "Reconciliation should have run without marker file"


# ---------------------------------------------------------------------------
# AC4: Reverse CoW clone restores missing masters
# ---------------------------------------------------------------------------


class TestReverseCoWRestore:
    """AC4: Reverse CoW clone restores missing master from latest versioned snapshot."""

    def test_reconcile_skips_repos_with_existing_master(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC4: Repos with existing master directories are not restored.
        """
        (golden_repos_dir / "my-repo").mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(cp_calls) == 0, (
            f"No cp --reflink expected for repos with existing masters, got: {cp_calls}"
        )

    def test_reconcile_restores_missing_master_via_reverse_cow(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC4: When master is missing but versioned copies exist,
        reverse CoW clone (cp --reflink=auto -a) must be executed
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
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        captured_cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(captured_cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(captured_cp_calls) == 1, (
            f"Expected 1 cp --reflink call, got {len(captured_cp_calls)}: {captured_cp_calls}"
        )
        cp_cmd = captured_cp_calls[0]
        assert "--reflink=auto" in cp_cmd, f"Expected --reflink=auto in: {cp_cmd}"
        assert "-a" in cp_cmd, f"Expected -a in: {cp_cmd}"
        assert str(v2) in cp_cmd, (
            f"Expected latest versioned dir {v2} as source in: {cp_cmd}"
        )
        assert str(master_path) in cp_cmd, (
            f"Expected master path {master_path} as destination in: {cp_cmd}"
        )

    def test_reconcile_uses_latest_versioned_snapshot_as_source(
        self, scheduler, golden_repos_dir, mock_registry
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
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        captured_cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(captured_cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(captured_cp_calls) == 1
        cp_cmd = captured_cp_calls[0]
        assert str(v_latest) in cp_cmd, (
            f"Expected LATEST versioned dir {v_latest} in cp cmd: {cp_cmd}"
        )
        assert str(v_old) not in cp_cmd, (
            f"Old versioned dir {v_old} should not be in cp cmd: {cp_cmd}"
        )

    def test_reconcile_runs_fix_config_on_restored_master(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC4: After reverse CoW clone, cidx fix-config --force must be run
        on the restored master to fix .code-indexer/ paths.
        """
        repo_name = "my-repo"
        master_path = golden_repos_dir / repo_name

        (golden_repos_dir / ".versioned" / repo_name / "v_9999999").mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
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
            scheduler.reconcile_golden_repos()

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
        self, scheduler, golden_repos_dir, mock_registry
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

        cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(cp_calls) == 0, (
            f"Expected no cp calls for orphan repo, got: {cp_calls}"
        )

    def test_reconcile_skips_local_repos(
        self, scheduler, golden_repos_dir, mock_registry
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

        cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(cp_calls) == 0, (
            f"Local repos must not trigger reverse CoW: {cp_calls}"
        )

    def test_reconcile_handles_multiple_repos_missing_masters(
        self, scheduler, golden_repos_dir, mock_registry
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

        cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(cp_calls) == len(repo_names), (
            f"Expected {len(repo_names)} cp calls, got {len(cp_calls)}"
        )

    def test_reconcile_does_not_restore_repos_with_existing_masters(
        self, scheduler, golden_repos_dir, mock_registry
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

        cp_calls: list = []

        with patch("subprocess.run", side_effect=_make_subprocess_mock(cp_calls)):
            scheduler.reconcile_golden_repos()

        assert len(cp_calls) == 1, (
            f"Expected 1 cp call (only for no-master), got {len(cp_calls)}: {cp_calls}"
        )
        assert "no-master" in " ".join(cp_calls[0]), (
            f"cp call should reference no-master, got: {cp_calls[0]}"
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

        # Create the description file
        cidx_meta_dir = golden_repos_dir / "cidx-meta"
        cidx_meta_dir.mkdir(parents=True, exist_ok=True)
        (cidx_meta_dir / f"{repo_name}.md").write_text("# Description\nThis is my-repo.")

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
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
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
        self, scheduler, golden_repos_dir, mock_registry
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

        cp_calls: list = []
        call_count = [0]

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "cp":
                cp_calls.append(cmd)
                call_count[0] += 1
                if call_count[0] == 2:
                    raise OSError("Disk full simulation")
            result = Mock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        # Must not raise even when one repo fails
        with patch("subprocess.run", side_effect=mock_subprocess_run):
            scheduler.reconcile_golden_repos()

        # All 3 attempted: 2 succeed, 1 fails mid-cp
        assert len(cp_calls) >= 2, (
            f"Expected at least 2 cp attempts, got {len(cp_calls)}"
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
