"""
Unit tests for RipgrepInstaller utility class.

Tests the shared ripgrep installation utility used by both ServerInstaller
and DeploymentExecutor, including critical security features like safe tar
extraction and path traversal prevention.
"""

import subprocess
import tarfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, mock_open

import pytest

from code_indexer.server.utils.ripgrep_installer import RipgrepInstaller


class TestRipgrepInstallerInit:
    """Tests for RipgrepInstaller initialization."""

    def test_init_with_default_home_dir(self):
        """Test initialization uses Path.home() by default."""
        installer = RipgrepInstaller()
        assert installer.home_dir == Path.home()

    def test_init_with_custom_home_dir(self, tmp_path):
        """Test initialization accepts custom home directory."""
        installer = RipgrepInstaller(home_dir=tmp_path)
        assert installer.home_dir == tmp_path


class TestIsInstalled:
    """Tests for is_installed method."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        return RipgrepInstaller(home_dir=tmp_path)

    def test_returns_true_when_rg_installed(self, installer):
        """Test returns True when rg --version succeeds."""
        mock_result = Mock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = installer.is_installed()

        assert result is True
        mock_run.assert_called_once_with(
            ["rg", "--version"], capture_output=True, text=True, timeout=10
        )

    def test_returns_false_when_rg_not_found(self, installer):
        """Test returns False when rg command not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = installer.is_installed()

        assert result is False

    def test_returns_false_when_rg_returns_nonzero(self, installer):
        """Test returns False when rg returns non-zero exit code."""
        mock_result = Mock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = installer.is_installed()

        assert result is False

    def test_returns_false_on_timeout(self, installer):
        """Test returns False when command times out."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("rg", 10)):
            result = installer.is_installed()

        assert result is False


class TestGetInstallPath:
    """Tests for get_install_path method."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        return RipgrepInstaller(home_dir=tmp_path)

    def test_returns_local_bin_path(self, installer, tmp_path):
        """Test returns ~/.local/bin/rg path."""
        result = installer.get_install_path()

        assert result == tmp_path / ".local" / "bin" / "rg"
        assert isinstance(result, Path)


class TestSafeExtractTar:
    """Tests for _safe_extract_tar method (CRITICAL SECURITY)."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        return RipgrepInstaller(home_dir=tmp_path)

    def test_extracts_normal_tar_successfully(self, installer, tmp_path):
        """Test successfully extracts tar with normal paths."""
        # Create a mock tarfile with safe members
        mock_tar = Mock(spec=tarfile.TarFile)
        member1 = Mock()
        member1.name = "ripgrep-14.1.1/rg"
        member2 = Mock()
        member2.name = "ripgrep-14.1.1/README.md"
        mock_tar.getmembers.return_value = [member1, member2]

        dest_path = tmp_path / "extract"
        dest_path.mkdir()

        # Should not raise exception
        installer._safe_extract_tar(mock_tar, str(dest_path))
        mock_tar.extractall.assert_called_once_with(str(dest_path))

    def test_blocks_absolute_path_traversal(self, installer, tmp_path):
        """Test blocks path traversal using absolute paths."""
        mock_tar = Mock(spec=tarfile.TarFile)
        member = Mock()
        member.name = "/etc/passwd"  # Absolute path - malicious
        mock_tar.getmembers.return_value = [member]

        dest_path = tmp_path / "extract"
        dest_path.mkdir()

        with pytest.raises(ValueError, match="Path traversal detected"):
            installer._safe_extract_tar(mock_tar, str(dest_path))

        mock_tar.extractall.assert_not_called()

    def test_blocks_parent_directory_traversal(self, installer, tmp_path):
        """Test blocks path traversal using ../ sequences."""
        mock_tar = Mock(spec=tarfile.TarFile)
        member = Mock()
        member.name = "../../../etc/passwd"  # Parent directory traversal
        mock_tar.getmembers.return_value = [member]

        dest_path = tmp_path / "extract"
        dest_path.mkdir()

        with pytest.raises(ValueError, match="Path traversal detected"):
            installer._safe_extract_tar(mock_tar, str(dest_path))

        mock_tar.extractall.assert_not_called()

    def test_blocks_mixed_traversal_attack(self, installer, tmp_path):
        """Test blocks mixed normal + malicious paths."""
        mock_tar = Mock(spec=tarfile.TarFile)
        member1 = Mock()
        member1.name = "ripgrep-14.1.1/rg"  # Safe
        member2 = Mock()
        member2.name = "ripgrep-14.1.1/../../etc/shadow"  # Malicious
        mock_tar.getmembers.return_value = [member1, member2]

        dest_path = tmp_path / "extract"
        dest_path.mkdir()

        with pytest.raises(ValueError, match="Path traversal detected"):
            installer._safe_extract_tar(mock_tar, str(dest_path))

        mock_tar.extractall.assert_not_called()


class TestDownloadFile:
    """Tests for _download_file method."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        return RipgrepInstaller(home_dir=tmp_path)

    def test_downloads_file_successfully(self, installer, tmp_path):
        """Test successfully downloads file to destination with timeout."""
        url = "https://example.com/file.tar.gz"
        dest_path = tmp_path / "downloaded.tar.gz"

        mock_response = MagicMock()
        mock_response.read.return_value = b"file contents"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        with patch(
            "urllib.request.urlopen", return_value=mock_response
        ) as mock_urlopen:
            with patch("builtins.open", mock_open()) as mock_file:
                installer._download_file(url, dest_path)

        # Verify urlopen called with URL and timeout
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        assert call_args[0][0] == url
        assert call_args.kwargs.get("timeout") == 60

        mock_file.assert_called_once_with(dest_path, "wb")
        mock_file().write.assert_called_once_with(b"file contents")

    def test_handles_download_failure(self, installer, tmp_path):
        """Test handles download failures gracefully."""
        url = "https://example.com/nonexistent.tar.gz"
        dest_path = tmp_path / "downloaded.tar.gz"

        with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            with pytest.raises(Exception, match="Network error"):
                installer._download_file(url, dest_path)


class TestInstall:
    """Tests for install method."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        return RipgrepInstaller(home_dir=tmp_path)

    def test_skips_installation_when_already_installed(self, installer):
        """Test skips installation when ripgrep already present (idempotent)."""
        with patch.object(installer, "is_installed", return_value=True) as mock_check:
            with patch.object(installer, "_download_file") as mock_download:
                result = installer.install()

        assert result is True
        mock_check.assert_called_once()
        mock_download.assert_not_called()

    @patch("platform.machine", return_value="aarch64")
    def test_skips_installation_on_non_x86_64(self, mock_machine, installer):
        """Test skips installation on non-x86_64 architecture."""
        with patch.object(installer, "is_installed", return_value=False):
            result = installer.install()

        assert result is False

    @patch("shutil.copy2")
    @patch("shutil.rmtree")
    @patch("tempfile.mkdtemp")
    @patch("tarfile.open")
    @patch("platform.machine", return_value="x86_64")
    def test_installs_successfully(
        self,
        mock_machine,
        mock_tarfile,
        mock_mkdtemp,
        mock_rmtree,
        mock_copy,
        installer,
        tmp_path,
    ):
        """Test successful installation flow."""
        temp_dir = str(tmp_path / "temp")
        mock_mkdtemp.return_value = temp_dir

        mock_tar = Mock()
        mock_tar.getmembers.return_value = []
        mock_tarfile.return_value.__enter__.return_value = mock_tar

        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        install_path = local_bin / "rg"

        # Create the file so chmod doesn't fail
        install_path.touch()

        install_check_results = [False, True]

        with patch.object(installer, "is_installed", side_effect=install_check_results):
            with patch.object(installer, "_download_file") as mock_download:
                with patch.object(
                    installer, "get_install_path", return_value=install_path
                ):
                    result = installer.install()

        assert result is True
        assert mock_download.called
        download_args = mock_download.call_args[0]
        assert "ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz" in download_args[0]

    @patch("platform.machine", return_value="x86_64")
    def test_returns_false_when_download_fails(self, mock_machine, installer):
        """Test returns False when download fails."""
        with patch.object(installer, "is_installed", return_value=False):
            with patch.object(
                installer, "_download_file", side_effect=Exception("Download failed")
            ):
                result = installer.install()

        assert result is False

    @patch("shutil.copy2")
    @patch("shutil.rmtree")
    @patch("tempfile.mkdtemp")
    @patch("tarfile.open")
    @patch("platform.machine", return_value="x86_64")
    def test_returns_false_when_verification_fails(
        self,
        mock_machine,
        mock_tarfile,
        mock_mkdtemp,
        mock_rmtree,
        mock_copy,
        installer,
        tmp_path,
    ):
        """Test returns False when post-install verification fails."""
        temp_dir = str(tmp_path / "temp")
        mock_mkdtemp.return_value = temp_dir

        mock_tar = Mock()
        mock_tar.getmembers.return_value = []
        mock_tarfile.return_value.__enter__.return_value = mock_tar

        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        install_path = local_bin / "rg"

        with patch.object(installer, "is_installed", return_value=False):
            with patch.object(installer, "_download_file"):
                with patch.object(
                    installer, "get_install_path", return_value=install_path
                ):
                    result = installer.install()

        assert result is False

    @patch("platform.machine", return_value="x86_64")
    def test_handles_generic_exception(self, mock_machine, installer):
        """Test returns False when installation raises unexpected exception."""
        with patch.object(installer, "is_installed", return_value=False):
            with patch.object(
                installer,
                "_download_file",
                side_effect=RuntimeError("unexpected error"),
            ):
                result = installer.install()

        assert result is False

    def test_creates_local_bin_directory(self, installer, tmp_path):
        """Test install creates ~/.local/bin directory if it doesn't exist."""
        local_bin = tmp_path / ".local" / "bin"
        assert not local_bin.exists()

        install_check_results = [False, True]

        with patch.object(installer, "is_installed", side_effect=install_check_results):
            with patch.object(installer, "_download_file"):
                with patch("tempfile.mkdtemp", return_value=str(tmp_path / "temp")):
                    with patch("tarfile.open"):
                        with patch("shutil.copy2"):
                            with patch("shutil.rmtree"):
                                with patch("platform.machine", return_value="x86_64"):
                                    installer.install()

        assert local_bin.exists()
