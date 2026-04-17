"""
Unit tests for RefreshScheduler._create_new_index method.

Tests the complete functionality of creating new versioned indexes including:
- CoW clone with proper timeouts
- Git status fix (update-index + restore)
- cidx fix-config execution
- cidx index execution
- Index validation before returning
- Error handling and cleanup on failure
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.utils.config_manager import ServerResourceConfig


def create_successful_subprocess_mock(expected_dest):
    """
    Create a mock subprocess.run that simulates successful execution (no .git dir).

    Automatically creates index directory when cp (CoW clone) is called,
    simulating a pre-indexed source repo being cloned. _create_snapshot does
    NOT call cidx index — it validates the index was inherited from the clone.
    """

    def subprocess_side_effect(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "cp":
            index_dir = Path(expected_dest) / ".code-indexer" / "index"
            index_dir.mkdir(parents=True, exist_ok=True)
        return MagicMock(returncode=0, stdout="", stderr="")

    return subprocess_side_effect


def create_successful_subprocess_mock_with_git(expected_dest):
    """
    Create a mock subprocess.run that creates both index dir and .git dir on CoW clone.

    The .git dir causes _create_snapshot to trigger git update-index and git restore
    calls, allowing tests to verify git_update_index_timeout and git_restore_timeout
    flow through from resource_config to subprocess.
    """

    def subprocess_side_effect(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "cp":
            index_dir = Path(expected_dest) / ".code-indexer" / "index"
            index_dir.mkdir(parents=True, exist_ok=True)
            git_dir = Path(expected_dest) / ".git"
            git_dir.mkdir(parents=True, exist_ok=True)
        return MagicMock(returncode=0, stdout="", stderr="")

    return subprocess_side_effect


def _make_scheduler(tmp_path, resource_config):
    """Create a RefreshScheduler with the given resource_config in tmp_path."""
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    tracker = QueryTracker()
    cleanup_mgr = CleanupManager(tracker)
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=tracker,
        cleanup_manager=cleanup_mgr,
        resource_config=resource_config,
    )


class TestCreateNewIndex:
    """Test suite for RefreshScheduler._create_new_index method."""

    @pytest.fixture
    def scheduler_with_config(self, tmp_path):
        """Create RefreshScheduler with custom resource config for testing."""
        resource_config = ServerResourceConfig(
            cow_clone_timeout=10,
            cidx_fix_config_timeout=10,
            git_update_index_timeout=10,
            git_restore_timeout=10,
        )
        return _make_scheduler(tmp_path, resource_config)

    def test_create_snapshot_creates_versioned_directory(
        self, tmp_path, scheduler_with_config
    ):
        """Test that _create_snapshot creates .versioned/repo_name/v_timestamp/ directory."""
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_path = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock(expected_path)
                new_index_path = scheduler_with_config._create_snapshot(
                    alias_name="test-repo-global", source_path=source_path
                )
                assert new_index_path == expected_path

    def test_create_snapshot_performs_cow_clone(self, tmp_path, scheduler_with_config):
        """Test that _create_snapshot performs CoW clone using cp --reflink=auto -a."""
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_dest = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock(expected_dest)
                scheduler_with_config._create_snapshot(
                    alias_name="test-repo-global", source_path=source_path
                )
                cp_calls = [
                    c
                    for c in mock_run.call_args_list
                    if c[0] and c[0][0] and c[0][0][0] == "cp"
                ]
                assert len(cp_calls) == 1
                assert cp_calls[0][0][0] == [
                    "cp",
                    "--reflink=auto",
                    "-a",
                    source_path,
                    expected_dest,
                ]

    def test_create_snapshot_skips_git_update_index_without_git_dir(
        self, tmp_path, scheduler_with_config
    ):
        """Test that _create_snapshot does not run git update-index when .git is absent."""
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_dest = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock(expected_dest)
                scheduler_with_config._create_snapshot(
                    alias_name="test-repo-global", source_path=source_path
                )
                git_update_calls = [
                    c
                    for c in mock_run.call_args_list
                    if c[0]
                    and c[0][0]
                    and len(c[0][0]) >= 2
                    and c[0][0][0] == "git"
                    and c[0][0][1] == "update-index"
                ]
                assert len(git_update_calls) == 0

    def test_create_snapshot_runs_cidx_fix_config(
        self, tmp_path, scheduler_with_config
    ):
        """Test that _create_snapshot runs cidx fix-config --force."""
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_dest = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock(expected_dest)
                scheduler_with_config._create_snapshot(
                    alias_name="test-repo-global", source_path=source_path
                )
                cidx_fix_calls = [
                    c
                    for c in mock_run.call_args_list
                    if c[0]
                    and c[0][0]
                    and len(c[0][0]) >= 2
                    and c[0][0][0] == "cidx"
                    and c[0][0][1] == "fix-config"
                ]
                assert len(cidx_fix_calls) == 1
                assert cidx_fix_calls[0][0][0] == ["cidx", "fix-config", "--force"]

    def test_create_snapshot_does_not_run_cidx_index(
        self, tmp_path, scheduler_with_config
    ):
        """Test that _create_snapshot does NOT run cidx index.

        _create_snapshot clones an already-indexed source repo via CoW and
        validates the inherited index. cidx index is run by _index_source, not here.
        """
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_dest = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock(expected_dest)
                scheduler_with_config._create_snapshot(
                    alias_name="test-repo-global", source_path=source_path
                )
                cidx_index_calls = [
                    c
                    for c in mock_run.call_args_list
                    if c[0]
                    and c[0][0]
                    and len(c[0][0]) >= 2
                    and c[0][0][0] == "cidx"
                    and c[0][0][1] == "index"
                ]
                assert len(cidx_index_calls) == 0

    def test_create_snapshot_validates_index_exists(
        self, tmp_path, scheduler_with_config
    ):
        """Test that _create_snapshot validates index directory exists after clone."""
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                with pytest.raises(RuntimeError, match="Index validation failed"):
                    scheduler_with_config._create_snapshot(
                        alias_name="test-repo-global", source_path=source_path
                    )

    def test_create_snapshot_cleans_up_on_failure(
        self, tmp_path, scheduler_with_config
    ):
        """Test that _create_snapshot cleans up partial artifacts on CoW clone failure."""
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.CalledProcessError(
                    1, "cp", stderr="Permission denied"
                )
                with pytest.raises(RuntimeError, match="CoW clone failed"):
                    scheduler_with_config._create_snapshot(
                        alias_name="test-repo-global", source_path=source_path
                    )

    def test_create_snapshot_uses_resource_config_timeouts(
        self, tmp_path, scheduler_with_config
    ):
        """Test all four configurable timeouts flow from resource_config to subprocess calls.

        Verifies cow_clone_timeout, cidx_fix_config_timeout, git_update_index_timeout,
        and git_restore_timeout each reach subprocess.run. Uses mock with .git dir to
        trigger the git update-index and git restore calls.
        """
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_dest = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock_with_git(
                    expected_dest
                )
                scheduler_with_config._create_snapshot(
                    alias_name="test-repo-global", source_path=source_path
                )
                calls = mock_run.call_args_list

                cp_calls = [c for c in calls if c[0] and c[0][0] and c[0][0][0] == "cp"]
                assert len(cp_calls) == 1
                assert cp_calls[0][1]["timeout"] == 10, "cow_clone_timeout must be 10"

                fix_calls = [
                    c
                    for c in calls
                    if c[0]
                    and c[0][0]
                    and len(c[0][0]) >= 2
                    and c[0][0][0] == "cidx"
                    and c[0][0][1] == "fix-config"
                ]
                assert len(fix_calls) == 1
                assert fix_calls[0][1]["timeout"] == 10, (
                    "cidx_fix_config_timeout must be 10"
                )

                upd_calls = [
                    c
                    for c in calls
                    if c[0]
                    and c[0][0]
                    and len(c[0][0]) >= 2
                    and c[0][0][0] == "git"
                    and c[0][0][1] == "update-index"
                ]
                assert len(upd_calls) == 1
                assert upd_calls[0][1]["timeout"] == 10, (
                    "git_update_index_timeout must be 10 (from resource_config)"
                )

                rst_calls = [
                    c
                    for c in calls
                    if c[0]
                    and c[0][0]
                    and len(c[0][0]) >= 2
                    and c[0][0][0] == "git"
                    and c[0][0][1] == "restore"
                ]
                assert len(rst_calls) == 1
                assert rst_calls[0][1]["timeout"] == 10, (
                    "git_restore_timeout must be 10 (from resource_config)"
                )


class TestNoAttributeErrorRegression:
    """Regression: all four timeout fields in ServerResourceConfig are wired in _create_snapshot.

    Previously Story #683 removed git_update_index_timeout and git_restore_timeout from
    ServerResourceConfig, but those fields ARE wired in refresh_scheduler._create_snapshot().
    This caused AttributeError on every global repo refresh AND silently discarded operator
    timeout tuning (e.g. 1800s for an 11 GB cidx-meta-global repo). The correct fix restores
    them as real configurable dataclass fields.
    """

    @pytest.fixture
    def scheduler_with_all_timeouts(self, tmp_path):
        """Run _create_snapshot with all four timeouts=10 and a .git dir.

        Returns (exception_or_none, call_args_list). Any exception from _create_snapshot
        is caught and returned (not re-raised) so individual tests can assert on it.
        """
        resource_config = ServerResourceConfig(
            cow_clone_timeout=10,
            cidx_fix_config_timeout=10,
            git_update_index_timeout=10,
            git_restore_timeout=10,
        )
        scheduler = _make_scheduler(tmp_path, resource_config)
        source_path = str(tmp_path / "source_repo")
        Path(source_path).mkdir()
        expected_dest = str(
            tmp_path
            / ".code-indexer"
            / "golden_repos"
            / ".versioned"
            / "test-repo"
            / "v_1234567890"
        )
        caught_exc = None
        call_args = []
        with patch("time.time", return_value=1234567890):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = create_successful_subprocess_mock_with_git(
                    expected_dest
                )
                try:
                    scheduler._create_snapshot(
                        alias_name="test-repo-global", source_path=source_path
                    )
                except Exception as exc:  # noqa: BLE001
                    caught_exc = exc
                call_args = mock_run.call_args_list
        return caught_exc, call_args

    def test_no_attribute_error_on_resource_config_attrs(
        self, scheduler_with_all_timeouts
    ):
        """Regression: _create_snapshot must not raise AttributeError for any timeout attr."""
        caught_exc, _ = scheduler_with_all_timeouts
        if isinstance(caught_exc, AttributeError):
            pytest.fail(
                f"AttributeError raised — timeout attr missing from resource_config: {caught_exc}"
            )

    def test_git_update_index_timeout_flows_through(self, scheduler_with_all_timeouts):
        """git_update_index_timeout=10 from resource_config must reach subprocess.run."""
        _, call_args = scheduler_with_all_timeouts
        git_update_calls = [
            c
            for c in call_args
            if c[0]
            and c[0][0]
            and len(c[0][0]) >= 2
            and c[0][0][0] == "git"
            and c[0][0][1] == "update-index"
        ]
        assert len(git_update_calls) == 1, (
            "git update-index must be called once (triggered by .git dir)"
        )
        assert git_update_calls[0][1]["timeout"] == 10, (
            "git_update_index_timeout=10 from resource_config must flow through to subprocess"
        )

    def test_git_restore_timeout_flows_through(self, scheduler_with_all_timeouts):
        """git_restore_timeout=10 from resource_config must reach subprocess.run."""
        _, call_args = scheduler_with_all_timeouts
        git_restore_calls = [
            c
            for c in call_args
            if c[0]
            and c[0][0]
            and len(c[0][0]) >= 2
            and c[0][0][0] == "git"
            and c[0][0][1] == "restore"
        ]
        assert len(git_restore_calls) == 1, (
            "git restore must be called once (triggered by .git dir)"
        )
        assert git_restore_calls[0][1]["timeout"] == 10, (
            "git_restore_timeout=10 from resource_config must flow through to subprocess"
        )
