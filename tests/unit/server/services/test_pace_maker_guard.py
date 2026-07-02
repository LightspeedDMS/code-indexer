"""Story #997 - Unit tests for pace_maker_guard module.

Tests that enforce_pace_maker_config() correctly enforces pace-maker
configuration before Claude CLI invocations, is non-fatal, and uses
idempotent CLI enforcement.

Three-way mode:
  "disabled" -> no-op (never touch pace-maker)
  "on"       -> enforce pacing-only (5h + weekly limits)
  "off"      -> actively disable pace-maker
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


class TestEnforcePaceMakerConfigDisabledMode:
    """Tests for mode='disabled' (pure no-op): no subprocess calls ever made."""

    def test_disabled_mode_returns_early_no_subprocess(self) -> None:
        """When mode is 'disabled', no subprocess calls are made."""
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="disabled",
            ),
            patch("subprocess.run") as mock_run,
        ):
            enforce_pace_maker_config()
            mock_run.assert_not_called()

    def test_disabled_mode_does_not_check_cli_availability(self) -> None:
        """When mode is 'disabled', shutil.which is never consulted."""
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="disabled",
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which"
            ) as mock_which,
        ):
            enforce_pace_maker_config()
            mock_which.assert_not_called()


class TestEnforcePaceMakerConfigCliNotFound:
    """Tests for early-exit when pace-maker binary is absent."""

    def test_cli_not_found_returns_early_mode_on(self) -> None:
        """When pace-maker binary not in PATH and mode='on', no subprocess calls."""
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="on",
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which",
                return_value=None,
            ),
            patch("subprocess.run") as mock_run,
        ):
            enforce_pace_maker_config()
            mock_run.assert_not_called()

    def test_cli_not_found_returns_early_mode_off(self) -> None:
        """When pace-maker binary not in PATH and mode='off', no subprocess calls."""
        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="off",
            ),
            patch(
                "code_indexer.server.services.pace_maker_guard.shutil.which",
                return_value=None,
            ),
            patch("subprocess.run") as mock_run,
        ):
            enforce_pace_maker_config()
            mock_run.assert_not_called()


class TestEnforcePaceMakerConfigModeOn:
    """Tests for mode='on' (pacing-only) enforcement."""

    def _make_patches(self, status_output: str):
        """Return a context manager bundle for mode='on' tests."""
        return (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="on",
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

    def test_mode_on_already_correct_no_corrective_commands(self) -> None:
        """When status already matches pacing-only config, no corrective commands run."""
        correct_status = _make_pacing_only_status()
        p1, p2, p3, p4 = self._make_patches(correct_status)
        with p1, p2, p3, p4 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()

    def test_mode_on_drift_detected_runs_all_corrective_commands(self) -> None:
        """When status differs from pacing-only config, all corrective commands run."""
        drifted_status = "Pace Maker: ACTIVE\nSome Other Line: enabled\n"
        p1, p2, p3, p4 = self._make_patches(drifted_status)
        with p1, p2, p3, p4 as mock_cmd:
            enforce_pace_maker_config()
            assert mock_cmd.call_count == len(_PACING_ONLY_COMMANDS)
            for cmd in _PACING_ONLY_COMMANDS:
                mock_cmd.assert_any_call(cmd)

    def test_mode_on_status_none_does_not_run_corrective_commands(self) -> None:
        """When status call returns None (CLI error), no corrective commands run."""
        p1, p2, p3, p4 = self._make_patches(None)  # type: ignore
        with p1, p2, p3, p4 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()


class TestEnforcePaceMakerConfigModeOff:
    """Tests for mode='off' (actively disable) enforcement."""

    def _make_patches(self, status_output: str):
        return (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="off",
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

    def test_mode_off_already_inactive_no_off_command(self) -> None:
        """When status shows INACTIVE (no 'Pace Maker: ACTIVE'), no 'off' command runs."""
        inactive_status = "Pace Maker: INACTIVE\n5-Hour Limit: DISABLED\n"
        p1, p2, p3, p4 = self._make_patches(inactive_status)
        with p1, p2, p3, p4 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_not_called()

    def test_mode_off_active_runs_off_command(self) -> None:
        """When status shows 'Pace Maker: ACTIVE', runs 'pace-maker off'."""
        active_status = "Pace Maker: ACTIVE\n5-Hour Limit: ENABLED\n"
        p1, p2, p3, p4 = self._make_patches(active_status)
        with p1, p2, p3, p4 as mock_cmd:
            enforce_pace_maker_config()
            mock_cmd.assert_called_once_with(["pace-maker", "off"])

    def test_mode_off_status_none_no_command(self) -> None:
        """When status call returns None, no off command runs."""
        p1, p2, p3, p4 = self._make_patches(None)  # type: ignore
        with p1, p2, p3, p4 as mock_cmd:
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

    def test_exception_in_get_pace_maker_mode_is_nonfatal(self) -> None:
        """Exception raised by _get_pace_maker_mode does not propagate."""
        with patch(
            "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
            side_effect=RuntimeError("db error"),
        ):
            # Must not raise
            enforce_pace_maker_config()

    def test_timeout_from_status_is_nonfatal_mode_on(self) -> None:
        """subprocess.TimeoutExpired from _run_pace_maker_status does not propagate (mode=on)."""
        import subprocess

        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="on",
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

    def test_timeout_from_status_is_nonfatal_mode_off(self) -> None:
        """subprocess.TimeoutExpired from _run_pace_maker_status does not propagate (mode=off)."""
        import subprocess

        with (
            patch(
                "code_indexer.server.services.pace_maker_guard._get_pace_maker_mode",
                return_value="off",
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
