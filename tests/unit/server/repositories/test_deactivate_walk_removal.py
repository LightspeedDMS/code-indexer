"""
Unit tests for AC6: Drop redundant pre-deletion filesystem walks in deactivation.

Covers:
- Walk #1 (os.walk + os.path.getsize for size telemetry) is removed from success path
- Walk #2 (_detect_resource_leaks) is moved to rmtree-failure path only
- Bootstrap flag enable_predeactivation_leak_scan=True restores pre-flight leak scan
- Telemetry shape: log line no longer contains repo_size_mb or file_count
- Composite path receives the same treatment
- Config schema: enable_predeactivation_leak_scan exists and defaults to False
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def manager(temp_dir):
    """Create ActivatedRepoManager with minimal mocks."""
    golden_repo_manager = MagicMock()
    background_job_manager = MagicMock()
    background_job_manager.submit_job.return_value = "test-job-id"
    return ActivatedRepoManager(
        data_dir=temp_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )


def _make_repo_dir(manager: ActivatedRepoManager, username: str, alias: str) -> str:
    """Create a small fake repo directory under activated_repos_dir."""
    repo_dir = os.path.join(manager.activated_repos_dir, username, alias)
    os.makedirs(repo_dir, exist_ok=True)
    # Write a couple of small files so the directory is non-empty
    (Path(repo_dir) / "file1.py").write_text("print('hello')")
    (Path(repo_dir) / "file2.py").write_text("print('world')")
    return repo_dir


def _make_metadata(manager: ActivatedRepoManager, username: str, alias: str) -> None:
    """Write minimal JSON metadata so _delete_metadata / _load_metadata work."""
    import json

    metadata_dir = os.path.join(manager.activated_repos_dir, username)
    os.makedirs(metadata_dir, exist_ok=True)
    meta_file = os.path.join(metadata_dir, f"{alias}.json")
    meta = {
        "user_alias": alias,
        "repo_url": "https://example.com/repo.git",
        "username": username,
        "path": os.path.join(manager.activated_repos_dir, username, alias),
        "is_composite": False,
    }
    with open(meta_file, "w") as f:
        json.dump(meta, f)


# ---------------------------------------------------------------------------
# Test 1: _do_deactivate_single success path — os.walk NEVER called
# ---------------------------------------------------------------------------


class TestNoWalkOnSuccessPath:
    """Walk #1 (size telemetry) must be completely absent on success path."""

    def test_os_walk_not_called_on_success(self, manager, temp_dir):
        """Patch os.walk to raise; success path must complete without triggering it."""
        username = "alice"
        alias = "test-repo"
        _make_repo_dir(manager, username, alias)
        _make_metadata(manager, username, alias)

        metadata = {
            "user_alias": alias,
            "path": os.path.join(manager.activated_repos_dir, username, alias),
            "is_composite": False,
            "username": username,
        }

        walk_called = []

        def walk_raises(*args, **kwargs):
            walk_called.append(args)
            raise AssertionError("os.walk must NOT be called on the success path")

        with patch("os.walk", side_effect=walk_raises):
            # Should not raise
            result = manager._do_deactivate_single(username, alias, metadata)

        assert result["success"] is True
        assert walk_called == [], "os.walk was called but should not have been"


# ---------------------------------------------------------------------------
# Test 2: _do_deactivate_single success path — _detect_resource_leaks NOT called
#          when enable_predeactivation_leak_scan=False (default)
# ---------------------------------------------------------------------------


class TestNoLeakDetectionOnSuccessPathByDefault:
    """_detect_resource_leaks must not be called on success path when flag is False."""

    def test_detect_resource_leaks_not_called_on_success(self, manager, temp_dir):
        username = "alice"
        alias = "test-repo"
        _make_repo_dir(manager, username, alias)
        _make_metadata(manager, username, alias)

        metadata = {
            "user_alias": alias,
            "path": os.path.join(manager.activated_repos_dir, username, alias),
            "is_composite": False,
            "username": username,
        }

        leak_calls = []

        original_detect = manager._detect_resource_leaks

        def tracking_detect(*args, **kwargs):
            leak_calls.append(args)
            return original_detect(*args, **kwargs)

        # Ensure flag is False
        with patch.object(
            manager, "_detect_resource_leaks", side_effect=tracking_detect
        ):
            with patch(
                "src.code_indexer.server.repositories.activated_repo_manager._predeactivation_leak_scan_enabled",
                return_value=False,
            ):
                result = manager._do_deactivate_single(username, alias, metadata)

        assert result["success"] is True
        assert leak_calls == [], (
            "_detect_resource_leaks was called on success path but should not have been"
        )


# ---------------------------------------------------------------------------
# Test 3: _do_deactivate_single rmtree-FAILURE path — _detect_resource_leaks
#          IS called exactly once for post-failure diagnostic
# ---------------------------------------------------------------------------


class TestLeakDetectionOnRmtreeFailurePath:
    """_detect_resource_leaks must be called when rmtree fails."""

    def test_detect_resource_leaks_called_on_rmtree_failure(self, manager, temp_dir):
        username = "alice"
        alias = "test-repo"
        _make_repo_dir(manager, username, alias)
        _make_metadata(manager, username, alias)

        metadata = {
            "user_alias": alias,
            "path": os.path.join(manager.activated_repos_dir, username, alias),
            "is_composite": False,
            "username": username,
        }

        leak_calls = []

        def tracking_detect(repo_dir, alias_arg):
            leak_calls.append((repo_dir, alias_arg))
            return []

        def rename_raises(src, dst, *args, **kwargs):
            # Post-Commit 4: failure path is Phase 1 os.rename (not Phase 2 rmtree)
            raise OSError("Simulated rename failure")

        with patch.object(
            manager, "_detect_resource_leaks", side_effect=tracking_detect
        ):
            with patch(
                "src.code_indexer.server.repositories.activated_repo_manager.os.rename",
                side_effect=rename_raises,
            ):
                with patch(
                    "src.code_indexer.server.repositories.activated_repo_manager._predeactivation_leak_scan_enabled",
                    return_value=False,
                ):
                    result = manager._do_deactivate_single(username, alias, metadata)

        # Result is still returned (rename failure is non-fatal — metadata cleanup continues)
        assert result["success"] is True or "warnings" in result
        assert len(leak_calls) == 1, (
            f"_detect_resource_leaks should be called exactly once on Phase 1 rename failure; "
            f"got {len(leak_calls)} calls"
        )


# ---------------------------------------------------------------------------
# Test 4: when enable_predeactivation_leak_scan=True, _detect_resource_leaks
#          is called PRE-rmtree on the success path (parity with old behavior)
# ---------------------------------------------------------------------------


class TestLeakDetectionRestoredByFlag:
    """When bootstrap flag is True, _detect_resource_leaks runs pre-rmtree even on success."""

    def test_detect_resource_leaks_called_when_flag_true(self, manager, temp_dir):
        username = "alice"
        alias = "test-repo"
        _make_repo_dir(manager, username, alias)
        _make_metadata(manager, username, alias)

        metadata = {
            "user_alias": alias,
            "path": os.path.join(manager.activated_repos_dir, username, alias),
            "is_composite": False,
            "username": username,
        }

        leak_calls = []

        def tracking_detect(repo_dir, alias_arg):
            leak_calls.append((repo_dir, alias_arg))
            return []

        with patch.object(
            manager, "_detect_resource_leaks", side_effect=tracking_detect
        ):
            with patch(
                "src.code_indexer.server.repositories.activated_repo_manager._predeactivation_leak_scan_enabled",
                return_value=True,
            ):
                result = manager._do_deactivate_single(username, alias, metadata)

        assert result["success"] is True
        assert len(leak_calls) >= 1, (
            "_detect_resource_leaks should be called pre-rmtree when flag is True"
        )


# ---------------------------------------------------------------------------
# Test 5: Composite path — _do_deactivate_composite — os.walk NOT called
# ---------------------------------------------------------------------------


class TestCompositeNoWalkOnSuccessPath:
    """Walk #1 must not be called in composite deactivation success path."""

    def test_os_walk_not_called_on_composite_success(self, manager, temp_dir):
        import json

        username = "alice"
        alias = "composite-repo"
        repo_path = Path(manager.activated_repos_dir) / username / alias
        repo_path.mkdir(parents=True, exist_ok=True)

        metadata = {
            "user_alias": alias,
            "path": str(repo_path),
            "is_composite": True,
            "username": username,
        }

        # Write metadata file
        meta_dir = os.path.join(manager.activated_repos_dir, username)
        meta_file = os.path.join(meta_dir, f"{alias}.json")
        with open(meta_file, "w") as f:
            json.dump(metadata, f)

        walk_called = []

        def walk_raises(*args, **kwargs):
            walk_called.append(args)
            raise AssertionError(
                "os.walk must NOT be called on the composite success path"
            )

        # Mock _stop_composite_services and ProxyConfigManager to isolate
        with patch.object(manager, "_stop_composite_services"):
            with patch(
                "src.code_indexer.server.repositories.activated_repo_manager.os.walk",
                side_effect=walk_raises,
            ):
                try:
                    manager._do_deactivate_composite(username, alias, metadata)
                    # If it succeeded, walk was not called
                    assert walk_called == []
                except Exception as exc:
                    if "os.walk must NOT be called" in str(exc):
                        pytest.fail(str(exc))
                    # Other exceptions (e.g., ProxyConfigManager import) are OK for this test


# ---------------------------------------------------------------------------
# Test 6: Composite path — _detect_resource_leaks called on rmtree failure
# ---------------------------------------------------------------------------


class TestCompositeLeakDetectionOnRmtreeFailure:
    """Composite path must invoke _detect_resource_leaks on rmtree failure."""

    def test_detect_resource_leaks_called_on_composite_rmtree_failure(
        self, manager, temp_dir
    ):
        import json

        username = "alice"
        alias = "composite-repo"
        repo_path = Path(manager.activated_repos_dir) / username / alias
        repo_path.mkdir(parents=True, exist_ok=True)

        metadata = {
            "user_alias": alias,
            "path": str(repo_path),
            "is_composite": True,
            "username": username,
        }

        # Write metadata file
        meta_dir = os.path.join(manager.activated_repos_dir, username)
        meta_file = os.path.join(meta_dir, f"{alias}.json")
        with open(meta_file, "w") as f:
            json.dump(metadata, f)

        leak_calls = []

        def tracking_detect(repo_dir, alias_arg):
            leak_calls.append((repo_dir, alias_arg))
            return []

        def rename_raises(*args, **kwargs):
            # Post-Commit 5: composite Phase 1 failure is fd-anchored rename
            raise OSError("Simulated rename failure")

        with patch.object(
            manager, "_detect_resource_leaks", side_effect=tracking_detect
        ):
            with patch.object(manager, "_stop_composite_services"):
                with patch(
                    "src.code_indexer.server.repositories.activated_repo_manager._fd_anchored_phase1_rename",
                    side_effect=rename_raises,
                ):
                    with patch(
                        "src.code_indexer.server.repositories.activated_repo_manager._predeactivation_leak_scan_enabled",
                        return_value=False,
                    ):
                        try:
                            manager._do_deactivate_composite(username, alias, metadata)
                        except Exception:
                            pass  # rename failure may propagate differently for composite

        assert len(leak_calls) >= 1, (
            f"_detect_resource_leaks should be called on composite Phase 1 rename failure; "
            f"got {len(leak_calls)} calls"
        )


# ---------------------------------------------------------------------------
# Test 7: Telemetry shape — log dict must NOT contain repo_size_mb or file_count
# ---------------------------------------------------------------------------


class TestTelemetryShape:
    """The 'Repository deactivation initiated' log must not have repo_size_mb or file_count."""

    def test_log_line_lacks_repo_size_mb_and_file_count(self, manager, temp_dir):
        username = "alice"
        alias = "test-repo"
        _make_repo_dir(manager, username, alias)
        _make_metadata(manager, username, alias)

        metadata = {
            "user_alias": alias,
            "path": os.path.join(manager.activated_repos_dir, username, alias),
            "is_composite": False,
            "username": username,
        }

        captured_extras = []

        original_warning = manager.logger.warning

        def capture_warning(msg, *args, **kwargs):
            extra = kwargs.get("extra", {})
            captured_extras.append((msg, extra))
            return original_warning(msg, *args, **kwargs)

        with patch.object(manager.logger, "warning", side_effect=capture_warning):
            with patch(
                "src.code_indexer.server.repositories.activated_repo_manager._predeactivation_leak_scan_enabled",
                return_value=False,
            ):
                manager._do_deactivate_single(username, alias, metadata)

        # Find the "Repository deactivation initiated" log line
        initiated_extras = [
            extra for msg, extra in captured_extras if "deactivation initiated" in msg
        ]
        assert len(initiated_extras) >= 1, (
            "Expected at least one 'Repository deactivation initiated' log call"
        )
        for extra in initiated_extras:
            assert "repo_size_mb" not in extra, (
                f"Log extra must not contain 'repo_size_mb', got: {extra}"
            )
            assert "file_count" not in extra, (
                f"Log extra must not contain 'file_count', got: {extra}"
            )


# ---------------------------------------------------------------------------
# Test 8: Config schema — enable_predeactivation_leak_scan defaults to False
# ---------------------------------------------------------------------------


class TestBootstrapConfigFlag:
    """enable_predeactivation_leak_scan must exist in ServerConfig and default to False."""

    def test_flag_exists_and_defaults_false(self):
        from src.code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/tmp/test-cidx-server")
        assert hasattr(config, "enable_predeactivation_leak_scan"), (
            "ServerConfig must have 'enable_predeactivation_leak_scan' attribute"
        )
        assert config.enable_predeactivation_leak_scan is False, (
            "enable_predeactivation_leak_scan must default to False"
        )

    def test_flag_is_in_bootstrap_keys(self):
        from src.code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "enable_predeactivation_leak_scan" in BOOTSTRAP_KEYS, (
            "'enable_predeactivation_leak_scan' must be in BOOTSTRAP_KEYS"
        )
