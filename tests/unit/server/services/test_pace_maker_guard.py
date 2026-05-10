"""Story #997 - Unit tests for pace_maker_guard module.

Tests that enforce_pace_maker_config() correctly enforces pace-maker
configuration before Claude CLI invocations, is non-fatal, and uses
idempotent CLI enforcement.
"""

from unittest.mock import MagicMock, patch


from code_indexer.server.services.pace_maker_guard import (
    _check_pacing_only_status,
    _PACING_ONLY_COMMANDS,
    _PACING_ONLY_EXPECTED,
    enforce_pace_maker_config,
)


def _make_pacing_only_status() -> str:
    """Build a status string that satisfies all _PACING_ONLY_EXPECTED checks."""
    return "\n".join(_PACING_ONLY_EXPECTED) + "\n"


class TestCheckPacingOnlyStatus:
    """Tests for the _check_pacing_only_status helper."""

    def test_returns_true_when_all_expected_lines_present(self) -> None:
        """Status with all expected lines returns True."""
        status = _make_pacing_only_status()
        assert _check_pacing_only_status(status) is True

    def test_returns_false_when_any_line_missing(self) -> None:
        """Status missing even one expected line returns False."""
        lines = list(_PACING_ONLY_EXPECTED)
        # Remove the first expected line
        partial = "\n".join(lines[1:]) + "\n"
        assert _check_pacing_only_status(partial) is False

    def test_returns_false_for_empty_string(self) -> None:
        """Empty status string returns False."""
        assert _check_pacing_only_status("") is False

    def test_strips_whitespace_from_lines(self) -> None:
        """Lines with leading/trailing whitespace are matched after strip."""
        padded = "\n".join("  " + line + "  " for line in _PACING_ONLY_EXPECTED)
        assert _check_pacing_only_status(padded) is True


class TestEnforcePaceMakerConfigNoClonePath:
    """Tests for early-exit when clone path is absent or invalid."""

    def test_no_clone_path_returns_early_no_subprocess(self) -> None:
        """When clone path is None, no subprocess calls are made."""
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=None,
            ),
            patch("subprocess.run") as mock_run,
        ):
            enforce_pace_maker_config()
            mock_run.assert_not_called()

    def test_clone_path_not_exists_returns_early(self, tmp_path) -> None:
        """When clone path does not exist on disk, no subprocess calls are made."""
        nonexistent = str(tmp_path / "nonexistent")
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=nonexistent,
            ),
            patch("subprocess.run") as mock_run,
        ):
            enforce_pace_maker_config()
            mock_run.assert_not_called()

    def test_cli_not_found_returns_early(self, tmp_path) -> None:
        """When pace-maker binary not in PATH, no subprocess calls are made."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=str(clone_dir),
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._get_enforce_toggle",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which",
                return_value=None,
            ),
            patch("subprocess.run") as mock_run,
        ):
            enforce_pace_maker_config()
            mock_run.assert_not_called()


class TestEnforcePaceMakerConfigEnforceTrue:
    """Tests for enforce=True (pacing-only mode) enforcement."""

    def _make_patches(self, clone_dir, status_output: str):
        """Return a context manager bundle for enforce=True tests."""
        return (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=str(clone_dir),
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._get_enforce_toggle",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which",
                return_value="/usr/local/bin/pace-maker",
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._run_pace_maker_status",
                return_value=status_output,
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._run_pace_maker_command",
            ),
        )

    def test_enforce_true_already_correct_no_corrective_commands(
        self, tmp_path
    ) -> None:
        """When status already matches pacing-only config, no corrective commands run."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        correct_status = _make_pacing_only_status()
        p1, p2, p3, p4, p5 = self._make_patches(clone_dir, correct_status)
        with p1, p2, p3, p4, p5 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()

    def test_enforce_true_drift_detected_runs_all_corrective_commands(
        self, tmp_path
    ) -> None:
        """When status differs from pacing-only config, all corrective commands run."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        drifted_status = "Pace Maker: ACTIVE\nSome Other Line: enabled\n"
        p1, p2, p3, p4, p5 = self._make_patches(clone_dir, drifted_status)
        with p1, p2, p3, p4, p5 as mock_cmd:
            enforce_pace_maker_config()
            assert mock_cmd.call_count == len(_PACING_ONLY_COMMANDS)
            for cmd in _PACING_ONLY_COMMANDS:
                mock_cmd.assert_any_call(cmd)

    def test_enforce_true_status_none_does_not_run_corrective_commands(
        self, tmp_path
    ) -> None:
        """When status call returns None (CLI error), no corrective commands run."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        p1, p2, p3, p4, p5 = self._make_patches(clone_dir, None)  # type: ignore
        with p1, p2, p3, p4, p5 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()


class TestEnforcePaceMakerConfigEnforceFalse:
    """Tests for enforce=False (dormant) enforcement."""

    def _make_patches(self, clone_dir, status_output: str):
        return (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=str(clone_dir),
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._get_enforce_toggle",
                return_value=False,
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which",
                return_value="/usr/local/bin/pace-maker",
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._run_pace_maker_status",
                return_value=status_output,
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._run_pace_maker_command",
            ),
        )

    def test_enforce_false_already_inactive_no_off_command(self, tmp_path) -> None:
        """When status shows INACTIVE (no 'Pace Maker: ACTIVE'), no 'off' command runs."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        inactive_status = "Pace Maker: INACTIVE\n5-Hour Limit: DISABLED\n"
        p1, p2, p3, p4, p5 = self._make_patches(clone_dir, inactive_status)
        with p1, p2, p3, p4, p5 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()

    def test_enforce_false_active_runs_off_command(self, tmp_path) -> None:
        """When status shows 'Pace Maker: ACTIVE', runs 'pace-maker off'."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        active_status = "Pace Maker: ACTIVE\n5-Hour Limit: ENABLED\n"
        p1, p2, p3, p4, p5 = self._make_patches(clone_dir, active_status)
        with p1, p2, p3, p4, p5 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_called_once_with(["pace-maker", "off"])

    def test_enforce_false_status_none_no_command(self, tmp_path) -> None:
        """When status call returns None, no off command runs."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        p1, p2, p3, p4, p5 = self._make_patches(clone_dir, None)  # type: ignore
        with p1, p2, p3, p4, p5 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()


class TestRunPaceMakerCommand:
    """Tests for _run_pace_maker_command returncode checking."""

    def test_nonzero_returncode_returns_false(self) -> None:
        """_run_pace_maker_command returns False when subprocess exits non-zero."""
        from code_indexer.server.services.pace_maker_guard import (
            _run_pace_maker_command,
        )

        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            result = _run_pace_maker_command(["pace-maker", "on"])
        assert result is False

    def test_zero_returncode_returns_true(self) -> None:
        """_run_pace_maker_command returns True when subprocess exits zero."""
        from code_indexer.server.services.pace_maker_guard import (
            _run_pace_maker_command,
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = _run_pace_maker_command(["pace-maker", "on"])
        assert result is True

    def test_nonzero_returncode_logs_warning(self) -> None:
        """_run_pace_maker_command logs WARNING on non-zero returncode."""
        from code_indexer.server.services.pace_maker_guard import (
            _run_pace_maker_command,
        )

        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stderr = "some error"
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(
                "code_indexer.server.services.pace_maker_guard.logger"
            ) as mock_logger,
        ):
            _run_pace_maker_command(["pace-maker", "on"])
        mock_logger.warning.assert_called_once()

    def test_exception_returns_false_via_simplified_except(self) -> None:
        """_run_pace_maker_command catches Exception (including OSError) and returns False."""
        from code_indexer.server.services.pace_maker_guard import (
            _run_pace_maker_command,
        )

        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = _run_pace_maker_command(["pace-maker", "on"])
        assert result is False


class TestRunPaceMakerStatus:
    """Tests for _run_pace_maker_status returncode checking."""

    def test_nonzero_returncode_returns_none(self) -> None:
        """_run_pace_maker_status returns None when subprocess exits non-zero."""
        from code_indexer.server.services.pace_maker_guard import _run_pace_maker_status

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "some output"
        with patch("subprocess.run", return_value=mock_result):
            result = _run_pace_maker_status()
        assert result is None

    def test_zero_returncode_returns_stdout(self) -> None:
        """_run_pace_maker_status returns stdout string when subprocess exits zero."""
        from code_indexer.server.services.pace_maker_guard import _run_pace_maker_status

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Pace Maker: ACTIVE\n"
        with patch("subprocess.run", return_value=mock_result):
            result = _run_pace_maker_status()
        assert result == "Pace Maker: ACTIVE\n"

    def test_nonzero_returncode_logs_debug(self) -> None:
        """_run_pace_maker_status logs DEBUG on non-zero returncode."""
        from code_indexer.server.services.pace_maker_guard import _run_pace_maker_status

        mock_result = MagicMock()
        mock_result.returncode = 127
        mock_result.stdout = ""
        mock_result.stderr = "command not found"
        with (
            patch("subprocess.run", return_value=mock_result),
            patch(
                "code_indexer.server.services.pace_maker_guard.logger"
            ) as mock_logger,
        ):
            _run_pace_maker_status()
        mock_logger.debug.assert_called_once()

    def test_exception_returns_none_via_simplified_except(self) -> None:
        """_run_pace_maker_status catches Exception (including OSError) and returns None."""
        from code_indexer.server.services.pace_maker_guard import _run_pace_maker_status

        with patch("subprocess.run", side_effect=OSError("permission denied")):
            result = _run_pace_maker_status()
        assert result is None


class TestEnforcePaceMakerConfigNonFatal:
    """Tests that enforce_pace_maker_config() is non-fatal under all failure modes."""

    def test_exception_in_get_clone_path_is_nonfatal(self) -> None:
        """Exception raised by _get_clone_path_from_bootstrap does not propagate."""
        with patch(
            "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
            side_effect=RuntimeError("disk error"),
        ):
            # Must not raise
            enforce_pace_maker_config()

    def test_exception_in_get_enforce_toggle_is_nonfatal(self, tmp_path) -> None:
        """Exception raised by _get_enforce_toggle does not propagate."""
        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=str(clone_dir),
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._get_enforce_toggle",
                side_effect=RuntimeError("db error"),
            ),
        ):
            enforce_pace_maker_config()

    def test_timeout_from_status_is_nonfatal(self, tmp_path) -> None:
        """subprocess.TimeoutExpired from _run_pace_maker_status does not propagate."""
        import subprocess

        clone_dir = tmp_path / "pace-maker"
        clone_dir.mkdir()
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_clone_path_from_bootstrap",
                return_value=str(clone_dir),
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._get_enforce_toggle",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which",
                return_value="/usr/local/bin/pace-maker",
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard._run_pace_maker_status",
                side_effect=subprocess.TimeoutExpired(cmd="pace-maker", timeout=5),
            ),
        ):
            enforce_pace_maker_config()
