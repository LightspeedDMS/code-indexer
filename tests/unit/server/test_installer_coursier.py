"""
Unit tests for Coursier installation in ServerInstaller.

Tests the automatic Coursier (cs) installation feature that ensures
Coursier is available for Java/Kotlin SCIP indexing.
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.installer import ServerInstaller


class TestIsCoursierInstalled:
    """Tests for _is_coursier_installed method."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        with patch.object(ServerInstaller, "__init__", lambda self, **kwargs: None):
            inst = ServerInstaller.__new__(ServerInstaller)
            inst.server_dir = tmp_path / ".cidx-server"
            inst.home_dir = tmp_path
            return inst

    def test_returns_true_when_cs_installed(self, installer):
        """Test returns True when cs --version succeeds."""
        mock_result = Mock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = installer._is_coursier_installed()

        assert result is True
        mock_run.assert_called_once_with(
            ["cs", "--version"], capture_output=True, text=True, timeout=10
        )

    def test_returns_false_when_cs_not_found(self, installer):
        """Test returns False when cs command not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = installer._is_coursier_installed()

        assert result is False

    def test_returns_false_when_cs_returns_nonzero(self, installer):
        """Test returns False when cs returns non-zero exit code."""
        mock_result = Mock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = installer._is_coursier_installed()

        assert result is False

    def test_returns_false_on_timeout(self, installer):
        """Test returns False when command times out."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cs", 10)):
            result = installer._is_coursier_installed()

        assert result is False


class TestInstallCoursier:
    """Tests for install_coursier method."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        with patch.object(ServerInstaller, "__init__", lambda self, **kwargs: None):
            inst = ServerInstaller.__new__(ServerInstaller)
            inst.server_dir = tmp_path / ".cidx-server"
            inst.home_dir = tmp_path
            return inst

    def test_skips_installation_when_already_installed(self, installer):
        """Test skips installation when Coursier already present (idempotent)."""
        with patch.object(
            installer, "_is_coursier_installed", return_value=True
        ) as mock_check:
            result = installer.install_coursier()

        assert result is True
        mock_check.assert_called_once()

    @patch("platform.machine", return_value="aarch64")
    def test_skips_installation_on_non_x86_64(self, mock_machine, installer):
        """Test skips installation on non-x86_64 architecture."""
        with patch.object(installer, "_is_coursier_installed", return_value=False):
            result = installer.install_coursier()

        assert result is False

    @patch("platform.machine", return_value="x86_64")
    def test_installs_coursier_successfully(self, mock_machine, installer, tmp_path):
        """Test successfully downloads and installs Coursier binary."""
        from unittest.mock import MagicMock

        # Create ~/.local/bin directory
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        install_path = local_bin / "cs"

        # Mock successful download with proper context manager
        mock_response = MagicMock()
        mock_response.read.return_value = b"gzipped binary content"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        # First check returns False (not installed), after install returns True
        install_check_results = [False, True]

        with patch.object(
            installer, "_is_coursier_installed", side_effect=install_check_results
        ):
            with patch("urllib.request.urlopen", return_value=mock_response):
                with patch("gzip.open") as mock_gzip:
                    mock_gzip.return_value.__enter__.return_value.read.return_value = (
                        b"decompressed binary"
                    )

                    result = installer.install_coursier()

        assert result is True

    @patch("platform.machine", return_value="x86_64")
    def test_returns_false_when_download_fails(self, mock_machine, installer):
        """Test returns False when download fails."""
        with patch.object(installer, "_is_coursier_installed", return_value=False):
            with patch(
                "urllib.request.urlopen", side_effect=Exception("Download failed")
            ):
                result = installer.install_coursier()

        assert result is False

    @patch("platform.machine", return_value="x86_64")
    def test_returns_false_when_verification_fails(
        self, mock_machine, installer, tmp_path
    ):
        """Test returns False when post-install verification fails."""
        from unittest.mock import MagicMock

        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)

        mock_response = MagicMock()
        mock_response.read.return_value = b"gzipped binary content"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        # Both checks return False (verification fails)
        with patch.object(installer, "_is_coursier_installed", return_value=False):
            with patch("urllib.request.urlopen", return_value=mock_response):
                with patch("gzip.open") as mock_gzip:
                    mock_gzip.return_value.__enter__.return_value.read.return_value = (
                        b"decompressed binary"
                    )

                    result = installer.install_coursier()

        assert result is False

    @patch("platform.machine", return_value="x86_64")
    def test_handles_generic_exception(self, mock_machine, installer):
        """Test returns False when installation raises unexpected exception."""
        with patch.object(installer, "_is_coursier_installed", return_value=False):
            with patch(
                "urllib.request.urlopen", side_effect=RuntimeError("unexpected error")
            ):
                result = installer.install_coursier()

        assert result is False


class TestInstallMethodIntegration:
    """Tests for install() method integration with Coursier installation."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with mocked dependencies."""
        with patch.object(ServerInstaller, "__init__", lambda self, **kwargs: None):
            inst = ServerInstaller.__new__(ServerInstaller)
            inst.server_dir = tmp_path / ".cidx-server"
            inst.home_dir = tmp_path
            inst.base_port = 8090
            inst.config_manager = Mock()
            inst.jwt_manager = Mock()
            return inst

    def test_install_calls_install_coursier(self, installer):
        """Test that install() method calls install_coursier()."""
        # Setup mocks for all install() dependencies
        installer.config_manager.create_server_directories = Mock()
        installer.config_manager.create_default_config = Mock(
            return_value=Mock(port=8090)
        )
        installer.config_manager.apply_env_overrides = Mock(
            return_value=Mock(port=8090)
        )
        installer.config_manager.validate_config = Mock()
        installer.config_manager.save_config = Mock()
        installer.config_manager.config_file_path = installer.server_dir / "config.json"
        installer.jwt_manager.get_or_create_secret = Mock()

        with patch.object(installer, "find_available_port", return_value=8090):
            with patch.object(installer, "create_server_directory_structure"):
                with patch.object(
                    installer,
                    "create_startup_script",
                    return_value=installer.server_dir / "start.sh",
                ):
                    with patch.object(installer, "seed_initial_admin_user"):
                        with patch.object(installer, "install_claude_cli"):
                            with patch.object(installer, "install_scip_indexers"):
                                with patch.object(installer, "install_ripgrep"):
                                    with patch.object(
                                        installer, "install_coursier"
                                    ) as mock_install_cs:
                                        installer.install()

        mock_install_cs.assert_called_once()
