"""Bug #1979 (P2, round 4): multi-provider `--clear` in daemon mode.

Coordinator ruling: do NOT implement a provider loop in the daemon (a
pre-existing daemon limitation, filed as a separate follow-up). Instead,
reject a multi-provider daemon `--clear` BEFORE delegation, with a message
pointing at `cidx config --no-daemon` -- the same pattern already used for
the unsupported rebuild flags a few lines above this guard in cli.py.

A single-provider daemon `--clear` must keep delegating and running exactly
as before.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

from click.testing import CliRunner

from code_indexer.cli import cli


class _DaemonClearGuardTestBase(unittest.TestCase):
    embedding_providers: Optional[List[str]] = None

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(dir=Path.cwd() / ".tmp")
        self.project_dir = Path(self.temp_dir) / "test_project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        config_dir = self.project_dir / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_data = {
            "codebase_dir": str(self.project_dir),
            "embedding_provider": "voyage-ai",
            "daemon": {"enabled": True},
        }
        if self.embedding_providers is not None:
            config_data["embedding_providers"] = self.embedding_providers
        (config_dir / "config.json").write_text(json.dumps(config_data))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _invoke_clear(self):
        runner = CliRunner()
        old_cwd = os.getcwd()
        os.chdir(str(self.project_dir))
        try:
            return runner.invoke(cli, ["index", "--clear"])
        finally:
            os.chdir(old_cwd)


class TestMultiProviderDaemonClearRejected(_DaemonClearGuardTestBase):
    embedding_providers = ["voyage-ai", "cohere"]

    def test_two_provider_daemon_clear_rejected_before_delegation(self):
        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon"
        ) as mock_index_via_daemon:
            result = self._invoke_clear()

        mock_index_via_daemon.assert_not_called()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("daemon mode", result.output)
        self.assertIn("--no-daemon", result.output)


class TestSingleProviderDaemonClearStillDelegates(_DaemonClearGuardTestBase):
    embedding_providers = None  # defaults to ["voyage-ai"]

    def test_single_provider_daemon_clear_still_delegates(self):
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=0,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
                return_value=[],
            ),
        ):
            result = self._invoke_clear()

        mock_index_via_daemon.assert_called_once()
        self.assertEqual(result.exit_code, 0, result.output)


if __name__ == "__main__":
    unittest.main()
