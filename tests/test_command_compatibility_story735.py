"""Tests for COMMAND_COMPATIBILITY matrix entries - Story #735.

Tests the new command group entries required for subsequent stories.
"""

from code_indexer.disabled_commands import COMMAND_COMPATIBILITY


class TestNewCommandCompatibilityEntries:
    """Tests for new command groups in COMMAND_COMPATIBILITY matrix."""

    def test_scip_command_supports_local_and_remote(self):
        """Test scip command is available in local and remote modes."""
        assert "scip" in COMMAND_COMPATIBILITY
        assert COMMAND_COMPATIBILITY["scip"]["local"] is True
        assert COMMAND_COMPATIBILITY["scip"]["remote"] is True
        assert COMMAND_COMPATIBILITY["scip"]["proxy"] is False
        assert COMMAND_COMPATIBILITY["scip"]["uninitialized"] is False

    def test_git_command_remote_only(self):
        """Test git command is remote-only."""
        assert "git" in COMMAND_COMPATIBILITY
        assert COMMAND_COMPATIBILITY["git"]["local"] is False
        assert COMMAND_COMPATIBILITY["git"]["remote"] is True
        assert COMMAND_COMPATIBILITY["git"]["proxy"] is False
        assert COMMAND_COMPATIBILITY["git"]["uninitialized"] is False

    def test_files_command_remote_only(self):
        """Test files command is remote-only."""
        assert "files" in COMMAND_COMPATIBILITY
        assert COMMAND_COMPATIBILITY["files"]["local"] is False
        assert COMMAND_COMPATIBILITY["files"]["remote"] is True
        assert COMMAND_COMPATIBILITY["files"]["proxy"] is False
        assert COMMAND_COMPATIBILITY["files"]["uninitialized"] is False

    def test_cicd_command_remote_only(self):
        """Test cicd command is remote-only."""
        assert "cicd" in COMMAND_COMPATIBILITY
        assert COMMAND_COMPATIBILITY["cicd"]["local"] is False
        assert COMMAND_COMPATIBILITY["cicd"]["remote"] is True
        assert COMMAND_COMPATIBILITY["cicd"]["proxy"] is False
        assert COMMAND_COMPATIBILITY["cicd"]["uninitialized"] is False

    def test_groups_command_remote_only(self):
        """Test groups command is remote-only."""
        assert "groups" in COMMAND_COMPATIBILITY
        assert COMMAND_COMPATIBILITY["groups"]["local"] is False
        assert COMMAND_COMPATIBILITY["groups"]["remote"] is True
        assert COMMAND_COMPATIBILITY["groups"]["proxy"] is False
        assert COMMAND_COMPATIBILITY["groups"]["uninitialized"] is False

    def test_credentials_command_remote_only(self):
        """Test credentials command is remote-only."""
        assert "credentials" in COMMAND_COMPATIBILITY
        assert COMMAND_COMPATIBILITY["credentials"]["local"] is False
        assert COMMAND_COMPATIBILITY["credentials"]["remote"] is True
        assert COMMAND_COMPATIBILITY["credentials"]["proxy"] is False
        assert COMMAND_COMPATIBILITY["credentials"]["uninitialized"] is False

    def test_all_new_commands_have_all_modes(self):
        """Test all new command groups have all required mode definitions."""
        new_commands = ["scip", "git", "files", "cicd", "groups", "credentials"]
        required_modes = ["local", "remote", "proxy", "uninitialized"]

        for cmd in new_commands:
            assert cmd in COMMAND_COMPATIBILITY, f"Command {cmd} not in matrix"
            for mode in required_modes:
                assert (
                    mode in COMMAND_COMPATIBILITY[cmd]
                ), f"Mode {mode} missing for {cmd}"
                assert isinstance(
                    COMMAND_COMPATIBILITY[cmd][mode], bool
                ), f"Mode {mode} for {cmd} is not a boolean"
