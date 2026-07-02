"""
Unit tests for ripgrep installation in ServerInstaller.

Tests that ServerInstaller.install_ripgrep() properly delegates to the shared
RipgrepInstaller utility class. The actual ripgrep installation logic is
comprehensively tested in tests/unit/server/utils/test_ripgrep_installer.py.
"""

from unittest.mock import Mock, patch

import pytest

from code_indexer.server.installer import ServerInstaller


class TestInstallRipgrepDelegation:
    """Tests for install_ripgrep delegation to RipgrepInstaller."""

    @pytest.fixture
    def installer(self, tmp_path):
        """Create installer with temporary directory."""
        with patch.object(ServerInstaller, "__init__", lambda self, **kwargs: None):
            inst = ServerInstaller.__new__(ServerInstaller)
            inst.server_dir = tmp_path / ".cidx-server"
            inst.home_dir = tmp_path
            return inst

    def test_delegates_to_ripgrep_installer(self, installer, tmp_path):
        """Test install_ripgrep creates RipgrepInstaller and calls install()."""
        with patch(
            "code_indexer.server.installer.RipgrepInstaller"
        ) as mock_ripgrep_class:
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = True
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            result = installer.install_ripgrep()

        # Verify RipgrepInstaller was instantiated with correct home_dir
        mock_ripgrep_class.assert_called_once_with(home_dir=tmp_path)

        # Verify install() was called on the instance
        mock_ripgrep_instance.install.assert_called_once()

        # Verify result is propagated
        assert result is True

    def test_returns_true_when_ripgrep_installer_succeeds(self, installer):
        """Test returns True when RipgrepInstaller.install() returns True."""
        with patch(
            "code_indexer.server.installer.RipgrepInstaller"
        ) as mock_ripgrep_class:
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = True
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            result = installer.install_ripgrep()

        assert result is True

    def test_returns_false_when_ripgrep_installer_fails(self, installer):
        """Test returns False when RipgrepInstaller.install() returns False."""
        with patch(
            "code_indexer.server.installer.RipgrepInstaller"
        ) as mock_ripgrep_class:
            mock_ripgrep_instance = Mock()
            mock_ripgrep_instance.install.return_value = False
            mock_ripgrep_class.return_value = mock_ripgrep_instance

            result = installer.install_ripgrep()

        assert result is False


class TestInstallMethodIntegration:
    """Tests for install() method integration with ripgrep installation."""

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

    def test_install_calls_install_ripgrep(self, installer):
        """Test that install() method calls install_ripgrep()."""
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
                                with patch.object(
                                    installer, "install_ripgrep"
                                ) as mock_install_rg:
                                    installer.install()

        mock_install_rg.assert_called_once()
