"""
Unit tests for `cidx server install-auto-update --branch`.

The auto-update systemd unit template parameterizes {USER}, {REPO_PATH}, and
{BRANCH} (CIDX_AUTO_UPDATE_BRANCH). The CLI command previously substituted
only {USER}/{REPO_PATH}, silently leaving {BRANCH} unrendered (a literal
"{BRANCH}" in the deployed unit) and giving the operator no way to choose
which branch the auto-updater tracks. `run_once.py` defaults
CIDX_AUTO_UPDATE_BRANCH to "master" when the env var is absent/empty, so an
unrendered "{BRANCH}" env value is also non-empty and would NOT fall back to
"master" -- it would be treated as a literal (bogus) branch name.

These tests mock subprocess.run/check_output so no real sudo/systemctl call
is made; they capture the actual temp-file content written by the command
before it is copied+deleted.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from code_indexer.cli import cli


def _fake_subprocess_run_capturing(captured: dict):
    """Return a subprocess.run stand-in that captures rendered unit content.

    Intercepts the `sudo cp <tmp_service_path> /etc/systemd/system/...` call
    and reads the temp file's content (still on disk at that point, since
    it is only unlinked in the caller's `finally` block after this returns).
    """

    def _fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, list)
            and len(cmd) >= 3
            and cmd[0] == "sudo"
            and cmd[1] == "cp"
            and str(cmd[2]).endswith(".service")
        ):
            captured["content"] = Path(cmd[2]).read_text()
        return MagicMock(returncode=0)

    return _fake_run


class TestServerInstallAutoUpdateBranch:
    """Test suite for the --branch option on `cidx server install-auto-update`."""

    def test_branch_option_substitutes_cidx_auto_update_branch(self):
        """--branch staging renders CIDX_AUTO_UPDATE_BRANCH=staging, no placeholder leak."""
        captured: dict = {}
        runner = CliRunner()

        with (
            patch(
                "code_indexer.cli.subprocess.run",
                side_effect=_fake_subprocess_run_capturing(captured),
            ),
            patch(
                "code_indexer.cli.subprocess.check_output",
                return_value="/fake/repo/path\n",
            ),
        ):
            result = runner.invoke(
                cli, ["server", "install-auto-update", "--branch", "staging"]
            )

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, output={result.output}, "
            f"exception={result.exception}"
        )
        assert "content" in captured, (
            "sudo cp of the rendered .service was never observed"
        )
        assert "CIDX_AUTO_UPDATE_BRANCH=staging" in captured["content"]
        assert "{BRANCH}" not in captured["content"]

    def test_default_branch_is_master_when_not_specified(self):
        """Omitting --branch preserves existing default behavior: master."""
        captured: dict = {}
        runner = CliRunner()

        with (
            patch(
                "code_indexer.cli.subprocess.run",
                side_effect=_fake_subprocess_run_capturing(captured),
            ),
            patch(
                "code_indexer.cli.subprocess.check_output",
                return_value="/fake/repo/path\n",
            ),
        ):
            result = runner.invoke(cli, ["server", "install-auto-update"])

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, output={result.output}, "
            f"exception={result.exception}"
        )
        assert "content" in captured
        assert "CIDX_AUTO_UPDATE_BRANCH=master" in captured["content"]
        assert "{BRANCH}" not in captured["content"]
