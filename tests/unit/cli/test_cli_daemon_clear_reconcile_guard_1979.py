"""Bug #1979 (P2, round 3): --clear + --reconcile must be rejected before
daemon delegation, not just in the foreground branch.

Regression test for the second Codex round-2 finding: cli.py forwarded both
flags to `_index_via_daemon` and exited before ever reaching the existing
conflict guard, which lived downstream of the daemon `sys.exit()` inside the
foreground-only branch. This test drives the CLI front door in daemon mode
and asserts the combination is rejected before any daemon delegation call.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from code_indexer.cli import cli


class TestCliDaemonClearReconcileGuard(unittest.TestCase):
    """--clear --reconcile must be rejected identically in daemon mode."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(dir=Path.cwd() / ".tmp")
        self.project_dir = Path(self.temp_dir) / "test_project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        test_file = self.project_dir / "test.py"
        test_file.write_text("def test():\n    pass\n")

        # Real on-disk config with daemon mode enabled -- CommandModeDetector
        # gates the "index" command off a REAL config.json, and cli.py's
        # `daemon_enabled = config.daemon and config.daemon.enabled` reads
        # the real ConfigManager-loaded Config, so daemon mode must be
        # genuinely enabled on disk, not just mocked.
        config_dir = self.project_dir / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "codebase_dir": str(self.project_dir),
                    "embedding_provider": "voyage-ai",
                    "daemon": {"enabled": True},
                }
            )
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_daemon_mode_clear_reconcile_rejected_before_delegation(self):
        runner = CliRunner()

        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon"
        ) as mock_index_via_daemon:
            # If the bug were present, the (unfixed) code would reach this
            # mock and exit 0 -- an explicit success return makes the RED
            # discriminating rather than an accidental non-zero exit code
            # from sys.exit(MagicMock()).
            mock_index_via_daemon.return_value = 0

            old_cwd = os.getcwd()
            os.chdir(str(self.project_dir))
            try:
                result = runner.invoke(cli, ["index", "--clear", "--reconcile"])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(
                result.exit_code,
                1,
                f"expected rejection before daemon delegation, got: {result.output}",
            )
            self.assertIn("--clear", result.output)
            self.assertIn("--reconcile", result.output)
            mock_index_via_daemon.assert_not_called()


if __name__ == "__main__":
    unittest.main()
