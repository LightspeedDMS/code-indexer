"""Unit tests for Bug #1440: `cidx server install-auto-update` must also
substitute {HOME} so the rendered unit's PATH= line points at the correct
user's ~/.local/bin.

The auto-update systemd unit template now parameterizes {HOME} (in addition
to the pre-existing {USER}, {REPO_PATH}, {BRANCH}) for its new
Environment="PATH={HOME}/.local/bin:..." line. The CLI command previously
substituted only {USER}/{REPO_PATH}/{BRANCH}, which would silently leave a
literal "{HOME}" in the deployed unit's PATH -- breaking the very PATH fix
this bug introduces.

Mirrors the established test_server_install_auto_update_branch.py pattern
exactly: mocks subprocess.run/check_output so no real sudo/systemctl call is
made, and captures the actual temp-file content written by the command
before it is copied+deleted.
"""

import os
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


class TestServerInstallAutoUpdateHome:
    """Test suite for the {HOME} substitution on `cidx server install-auto-update`."""

    def test_home_placeholder_substituted_with_actual_home_dir(self):
        """Rendered unit must contain the real home dir's .local/bin in
        PATH, with no unrendered {HOME} literal left behind."""
        captured: dict = {}
        runner = CliRunner()

        current_user = os.getenv("USER") or os.getenv("LOGNAME")
        assert current_user, (
            "Test environment must expose USER or LOGNAME so the expected "
            "home directory can be derived the same way the CLI does"
        )
        expected_home = os.path.expanduser(f"~{current_user}")

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
        assert "content" in captured, (
            "sudo cp of the rendered .service was never observed"
        )
        assert f"{expected_home}/.local/bin" in captured["content"], (
            f"Expected PATH to include {expected_home}/.local/bin; "
            f"got:\n{captured['content']}"
        )
        assert "{HOME}" not in captured["content"]
