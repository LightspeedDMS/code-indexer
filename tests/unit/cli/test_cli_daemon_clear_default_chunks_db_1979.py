"""Bug #1979 round 6: the maintainer's own acceptance criteria for this
issue (`gh issue view 1979 --comments`) require every `--clear` run --
standalone CLI AND daemon mode -- to rebuild every collection as CHUNKS_DB
by default, without the caller having to also pass
`--new-collection-layout=chunks_db`.

These tests exercise the daemon-mode CLI seam (`cli.py`'s
`daemon_enabled` branch resolving `use_chunks_db_for_new_collections` and
passing it into `_index_via_daemon`), the same seam already covered for the
explicit-flag case by `tests/unit/cli/test_daemon_new_collection_layout_1488.py`
and for the rebuild-check wiring by
`tests/unit/cli/test_cli_daemon_clear_rebuild_check_1979.py`. `_index_via_daemon`
is mocked (no real daemon subprocess) so these tests focus purely on what
the CLI resolves and forwards.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from code_indexer.cli import cli


class _DaemonIndexTestBase(unittest.TestCase):
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
        (config_dir / "config.json").write_text(json.dumps(config_data))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _invoke(self, args):
        runner = CliRunner()
        old_cwd = os.getcwd()
        os.chdir(str(self.project_dir))
        try:
            return runner.invoke(cli, args)
        finally:
            os.chdir(old_cwd)


class TestDaemonClearDefaultsToChunksDbWithNoFlag(_DaemonIndexTestBase):
    def test_daemon_clear_with_no_layout_flag_passes_chunks_db_true(self):
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
            result = self._invoke(["index", "--clear"])

        self.assertEqual(result.exit_code, 0, result.output)
        mock_index_via_daemon.assert_called_once()
        self.assertIs(
            mock_index_via_daemon.call_args.kwargs.get(
                "use_chunks_db_for_new_collections"
            ),
            True,
            "A daemon-mode --clear with no --new-collection-layout flag must "
            "default to CHUNKS_DB (True), per the maintainer's own "
            "acceptance criteria for Bug #1979.",
        )


class TestDaemonClearExplicitSharedJsonRejected(_DaemonIndexTestBase):
    """Bug #1979 round 7 (Codex round-6 P2): the maintainer's acceptance
    criterion is unconditional -- every successful clear=true run rebuilds
    every collection as CHUNKS_DB. An explicit
    --new-collection-layout=sharded_json request cannot be honored together
    with --clear (it would leave legacy vector_*.json collections in place),
    so the combination must be rejected BEFORE any daemon delegation, not
    silently honored."""

    def test_daemon_clear_with_explicit_sharded_json_flag_is_rejected(self):
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
            result = self._invoke(
                ["index", "--clear", "--new-collection-layout", "sharded_json"]
            )

        self.assertEqual(
            result.exit_code,
            1,
            "--clear --new-collection-layout=sharded_json must be rejected "
            f"before delegation, got exit_code={result.exit_code}: {result.output}",
        )
        mock_index_via_daemon.assert_not_called()


class TestDaemonNonClearIndexUnaffected(_DaemonIndexTestBase):
    def test_daemon_non_clear_index_passes_none(self):
        """Regression: this fix only changes the `--clear` default; a plain
        daemon-mode incremental `cidx index` (no --clear) must still resolve
        to None (ambient daemon-side env/default applies), exactly as
        before."""
        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon",
            return_value=0,
        ) as mock_index_via_daemon:
            result = self._invoke(["index"])

        self.assertEqual(result.exit_code, 0, result.output)
        mock_index_via_daemon.assert_called_once()
        self.assertIsNone(
            mock_index_via_daemon.call_args.kwargs.get(
                "use_chunks_db_for_new_collections"
            ),
            "A non-clear daemon-mode index run must be unaffected by this "
            "fix -- still None (ambient default).",
        )


if __name__ == "__main__":
    unittest.main()
