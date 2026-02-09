"""Tests for DeploymentExecutor hnswlib fallback clone approach.

Bug #160: Tests fallback mechanism that clones hnswlib to standalone location
when submodule initialization fails.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import (
    DeploymentExecutor,
    HNSWLIB_FALLBACK_PATH,
    HNSWLIB_REPO_URL,
)


@pytest.fixture
def executor(tmp_path: Path) -> DeploymentExecutor:
    """Create DeploymentExecutor instance with temp repo path."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
    )


class TestHnswlibConstants:
    """Test that required constants are defined."""

    def test_hnswlib_constants_defined(self):
        """Verify HNSWLIB_FALLBACK_PATH and HNSWLIB_REPO_URL constants exist."""
        assert HNSWLIB_FALLBACK_PATH == Path("/var/tmp/cidx-hnswlib")
        assert HNSWLIB_REPO_URL == "https://github.com/LightspeedDMS/hnswlib.git"


class TestCloneHnswlibStandalone:
    """Test _clone_hnswlib_standalone() method."""

    def test_clone_hnswlib_standalone_creates_directory(
        self, executor: DeploymentExecutor
    ):
        """Test that clone creates the fallback directory."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists") as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            # Directory doesn't exist initially
            mock_exists.return_value = False

            # Mock successful git operations
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            result = executor._clone_hnswlib_standalone()

            assert result is True
            # Should add to safe.directory and clone
            assert mock_run.call_count == 2

    def test_clone_hnswlib_standalone_removes_existing_directory(
        self, executor: DeploymentExecutor
    ):
        """Test that existing directory is removed before clone."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists") as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            # Directory exists
            mock_exists.return_value = True

            # Mock successful operations
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            result = executor._clone_hnswlib_standalone()

            assert result is True
            # Should remove directory
            mock_rmtree.assert_called_once_with(HNSWLIB_FALLBACK_PATH)

    def test_clone_hnswlib_standalone_adds_safe_directory(
        self, executor: DeploymentExecutor
    ):
        """Test that fallback path is added to git safe.directory."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists") as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            mock_exists.return_value = False
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            result = executor._clone_hnswlib_standalone()

            assert result is True
            # First call should be safe.directory add
            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == [
                "git",
                "config",
                "--global",
                "--add",
                "safe.directory",
                str(HNSWLIB_FALLBACK_PATH),
            ]

    def test_clone_hnswlib_standalone_clones_correct_repo(
        self, executor: DeploymentExecutor
    ):
        """Test that the correct repo URL is cloned."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists") as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            mock_exists.return_value = False
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            result = executor._clone_hnswlib_standalone()

            assert result is True
            # Second call should be git clone with 60s timeout
            second_call = mock_run.call_args_list[1]
            assert second_call[0][0] == [
                "git",
                "clone",
                HNSWLIB_REPO_URL,
                str(HNSWLIB_FALLBACK_PATH),
            ]
            assert second_call[1]["timeout"] == 60

    def test_clone_hnswlib_standalone_returns_false_on_rmtree_error(
        self, executor: DeploymentExecutor
    ):
        """Test that rmtree failure returns False."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists") as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            mock_exists.return_value = True
            mock_rmtree.side_effect = OSError("Permission denied")

            result = executor._clone_hnswlib_standalone()

            assert result is False

    def test_clone_hnswlib_standalone_returns_false_on_clone_failure(
        self, executor: DeploymentExecutor
    ):
        """Test that git clone failure returns False."""
        with (
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.exists") as mock_exists,
            patch("shutil.rmtree") as mock_rmtree,
        ):
            mock_exists.return_value = False
            # Safe directory succeeds, clone fails
            mock_run.side_effect = [
                Mock(returncode=0, stderr="", stdout=""),  # safe.directory
                Mock(returncode=1, stderr="Clone failed", stdout=""),  # clone
            ]

            result = executor._clone_hnswlib_standalone()

            assert result is False


class TestBuildCustomHnswlibWithPath:
    """Test build_custom_hnswlib() accepts custom path parameter."""

    def test_build_custom_hnswlib_accepts_custom_path(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that build_custom_hnswlib accepts hnswlib_path parameter."""
        custom_path = tmp_path / "custom-hnswlib"
        custom_path.mkdir()
        (custom_path / "setup.py").write_text("# setup")

        with (
            patch("subprocess.run") as mock_run,
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
        ):
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            result = executor.build_custom_hnswlib(hnswlib_path=custom_path)

            assert result is True
            # Verify pip install ran in custom_path
            pip_install_call = mock_run.call_args_list[1]  # Second call after pybind11
            assert pip_install_call[1]["cwd"] == custom_path

    def test_build_custom_hnswlib_uses_default_path_when_none(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that None parameter uses default submodule path."""
        default_path = tmp_path / "third_party" / "hnswlib"
        default_path.mkdir(parents=True)
        (default_path / "setup.py").write_text("# setup")

        with (
            patch("subprocess.run") as mock_run,
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
        ):
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            result = executor.build_custom_hnswlib(hnswlib_path=None)

            assert result is True
            # Verify pip install ran in default_path
            pip_install_call = mock_run.call_args_list[1]
            assert pip_install_call[1]["cwd"] == default_path


class TestBuildHnswlibWithFallback:
    """Test _build_hnswlib_with_fallback() unified method."""

    def test_build_hnswlib_with_fallback_tries_submodule_first(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that submodule path is tried first when setup.py exists."""
        submodule_path = tmp_path / "third_party" / "hnswlib"
        submodule_path.mkdir(parents=True)
        (submodule_path / "setup.py").write_text("# setup")

        with (
            patch.object(
                executor, "build_custom_hnswlib", return_value=True
            ) as mock_build,
            patch.object(
                executor, "_clone_hnswlib_standalone", return_value=True
            ) as mock_clone,
        ):
            result = executor._build_hnswlib_with_fallback()

            assert result is True
            # Should call build with default path (None)
            mock_build.assert_called_once_with(hnswlib_path=None)
            # Should NOT call fallback clone since submodule has setup.py
            mock_clone.assert_not_called()

    def test_build_hnswlib_with_fallback_uses_fallback_when_submodule_missing(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that fallback is used when submodule has no setup.py."""
        # Submodule directory exists but no setup.py - triggers fallback
        submodule_path = tmp_path / "third_party" / "hnswlib"
        submodule_path.mkdir(parents=True)

        with (
            patch.object(
                executor, "build_custom_hnswlib", return_value=True
            ) as mock_build,
            patch.object(
                executor, "_clone_hnswlib_standalone", return_value=True
            ) as mock_clone,
        ):
            result = executor._build_hnswlib_with_fallback()

            assert result is True
            # Should clone fallback and build from it
            mock_clone.assert_called_once()
            mock_build.assert_called_once_with(hnswlib_path=HNSWLIB_FALLBACK_PATH)

    def test_build_hnswlib_with_fallback_returns_false_when_clone_fails(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that False is returned when fallback clone fails."""
        # No setup.py - triggers fallback
        submodule_path = tmp_path / "third_party" / "hnswlib"
        submodule_path.mkdir(parents=True)

        with (
            patch.object(
                executor, "build_custom_hnswlib", return_value=True
            ) as mock_build,
            patch.object(
                executor, "_clone_hnswlib_standalone", return_value=False
            ) as mock_clone,
        ):
            result = executor._build_hnswlib_with_fallback()

            assert result is False
            # Should try to clone but fail
            mock_clone.assert_called_once()
            # Should NOT attempt build if clone fails
            mock_build.assert_not_called()

    def test_build_hnswlib_with_fallback_returns_false_when_fallback_build_fails(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that False is returned when fallback build fails."""
        # No setup.py - triggers fallback
        submodule_path = tmp_path / "third_party" / "hnswlib"
        submodule_path.mkdir(parents=True)

        with (
            patch.object(
                executor, "build_custom_hnswlib", return_value=False
            ) as mock_build,
            patch.object(
                executor, "_clone_hnswlib_standalone", return_value=True
            ) as mock_clone,
        ):
            result = executor._build_hnswlib_with_fallback()

            assert result is False
            # Should clone successfully then build fails
            mock_clone.assert_called_once()
            mock_build.assert_called_once_with(hnswlib_path=HNSWLIB_FALLBACK_PATH)

    def test_build_hnswlib_with_fallback_returns_true_when_submodule_succeeds(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that True is returned when submodule build succeeds."""
        submodule_path = tmp_path / "third_party" / "hnswlib"
        submodule_path.mkdir(parents=True)
        (submodule_path / "setup.py").write_text("# setup")

        with (
            patch.object(
                executor, "build_custom_hnswlib", return_value=True
            ) as mock_build,
            patch.object(
                executor, "_clone_hnswlib_standalone", return_value=True
            ) as mock_clone,
        ):
            result = executor._build_hnswlib_with_fallback()

            assert result is True
            # Should use submodule path
            mock_build.assert_called_once_with(hnswlib_path=None)
            mock_clone.assert_not_called()

    def test_build_hnswlib_with_fallback_returns_false_when_submodule_build_fails(
        self, executor: DeploymentExecutor, tmp_path: Path
    ):
        """Test that False is returned when submodule build fails."""
        submodule_path = tmp_path / "third_party" / "hnswlib"
        submodule_path.mkdir(parents=True)
        (submodule_path / "setup.py").write_text("# setup")

        with (
            patch.object(
                executor, "build_custom_hnswlib", return_value=False
            ) as mock_build,
            patch.object(
                executor, "_clone_hnswlib_standalone", return_value=True
            ) as mock_clone,
        ):
            result = executor._build_hnswlib_with_fallback()

            assert result is False
            # Should try submodule and fail, NOT try fallback
            mock_build.assert_called_once_with(hnswlib_path=None)
            mock_clone.assert_not_called()
