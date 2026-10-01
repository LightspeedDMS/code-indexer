"""Bug #1979 (P2, round 5 review): the daemon-mode single-provider `--clear`
guard (cli.py, next to the multi-provider guard in
test_cli_daemon_multi_provider_clear_guard_1979.py) only rejected
`len(embedding_providers) > 1`. Two real gaps slipped through it:

1. An EMPTY `embedding_providers` list (`embedding_providers: []` in
   config.json -- `Optional[List[str]]` in config.py, so an explicit empty
   list, distinct from the `None` default, is a value pydantic accepts)
   passes `len([]) > 1 is False`, and
   `provider_rebuild_check.find_providers_not_rebuilt_since` then iterates
   zero providers and returns `[]` (nothing stale) -- reporting success with
   ZERO verification performed.

2. A single-entry `embedding_providers` list can legitimately name a
   DIFFERENT provider than the top-level `embedding_provider` field --
   there is no cross-field validator tying them together (see
   `Config.get_embedding_providers()`, config.py). The daemon
   (`daemon/service.py`, `EmbeddingProviderFactory.create(config=config)`)
   always constructs its embedding provider from `config.embedding_provider`
   alone, never from `embedding_providers`. So a single mismatched entry
   would make the CLI's post-delegation rebuild check verify a completely
   different provider's metadata/rows than the one the daemon actually
   indexed -- stale rows from an unrelated earlier run could then falsely
   pass.

Both cases must be rejected BEFORE delegation, with the same
"daemon mode" / "--no-daemon" messaging convention as the existing
multi-provider guard, and `_index_via_daemon` must never be called.
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
    embedding_provider = "voyage-ai"
    embedding_providers: Optional[List[str]] = None

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(dir=Path.cwd() / ".tmp")
        self.project_dir = Path(self.temp_dir) / "test_project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        config_dir = self.project_dir / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_data = {
            "codebase_dir": str(self.project_dir),
            "embedding_provider": self.embedding_provider,
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


class TestEmptyProviderListDaemonClearRejected(_DaemonClearGuardTestBase):
    embedding_provider = "voyage-ai"
    embedding_providers = []

    def test_empty_provider_list_daemon_clear_rejected_before_delegation(self):
        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon"
        ) as mock_index_via_daemon:
            result = self._invoke_clear()

        mock_index_via_daemon.assert_not_called()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("daemon mode", result.output)
        self.assertIn("--no-daemon", result.output)


class TestMismatchedSingleProviderDaemonClearRejected(_DaemonClearGuardTestBase):
    # embedding_providers names a DIFFERENT provider than embedding_provider.
    embedding_provider = "voyage-ai"
    embedding_providers = ["cohere"]

    def test_mismatched_single_provider_daemon_clear_rejected_before_delegation(self):
        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon"
        ) as mock_index_via_daemon:
            result = self._invoke_clear()

        mock_index_via_daemon.assert_not_called()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("daemon mode", result.output)
        self.assertIn("--no-daemon", result.output)


class TestMatchedSingleProviderDaemonClearStillDelegates(_DaemonClearGuardTestBase):
    # Regression: embedding_providers agreeing with embedding_provider must
    # keep delegating exactly as before.
    embedding_provider = "voyage-ai"
    embedding_providers = ["voyage-ai"]

    def test_matched_single_provider_daemon_clear_still_delegates(self):
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
