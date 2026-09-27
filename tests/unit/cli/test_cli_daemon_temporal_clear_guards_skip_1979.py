"""Bug #1979 round 9: a regression introduced by rounds 5-7's semantic-only
`--clear` guards and rebuild check.

`cidx index --index-commits --clear` in DAEMON mode now incorrectly runs
into semantic-only machinery:

- the daemon single-provider guard (cli.py, ~3466, rounds 4/5) can reject a
  temporal clear in a multi-provider repo, even though the daemon's temporal
  branch (`daemon/service.py`'s `exposed_index_blocking`, `if
  kwargs.get("index_commits", False):`) never touches
  `embedding_providers`/multi-provider concerns at all -- it constructs a
  SINGLE `FilesystemVectorStore` directly and returns before ever reaching
  the semantic `BackendFactory.create` branch these guards exist to protect;
- the post-delegation semantic rebuild check
  (`provider_rebuild_check.find_providers_not_rebuilt_since`, round 3) runs
  whenever `clear` is true, but the daemon's temporal branch never writes
  the TEXT collection metadata/rows that check inspects -- it would
  incorrectly report "did not genuinely rebuild" for a temporal-only run
  that never touched semantic collections at all.

Fix: both checks now also require `not index_commits`. This test module
proves the fix and pins the regression's absence:

1. A daemon-mode temporal clear whose delegation succeeds exits 0, and the
   semantic rebuild check is never called (single-provider config).
2. The SAME holds in a two-provider config -- not rejected by the
   single-provider guard, which is a purely semantic concern.
3. A genuine SEMANTIC daemon clear (index_commits=False) is UNCHANGED --
   still rejected by the multi-provider guard, and still runs the rebuild
   check on a single-provider success (regression check).

A companion audit target -- `--index-commits --clear
--new-collection-layout=sharded_json` must still be rejected by temporal's
OWN Bug #1529 guard (`reject_sharded_json_for_temporal`), not
double-rejected/re-messaged by the round-7 semantic `--clear` +
`sharded_json` conflict guard -- is also covered here.
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


class _DaemonIndexCommitsTestBase(unittest.TestCase):
    embedding_providers = None  # type: ignore[assignment]

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

    def _invoke(self, args):
        runner = CliRunner()
        old_cwd = os.getcwd()
        os.chdir(str(self.project_dir))
        try:
            return runner.invoke(cli, args)
        finally:
            os.chdir(old_cwd)


class TestDaemonTemporalClearSkipsSemanticRebuildCheck(_DaemonIndexCommitsTestBase):
    """Single-provider config (the default)."""

    embedding_providers = None

    def test_temporal_clear_success_exits_zero_and_skips_rebuild_check(self):
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=0,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
            ) as mock_check,
        ):
            result = self._invoke(["index", "--index-commits", "--clear"])

        self.assertEqual(
            result.exit_code,
            0,
            f"a successful daemon-mode temporal clear must exit 0: {result.output}",
        )
        mock_index_via_daemon.assert_called_once()
        mock_check.assert_not_called()


class TestDaemonTemporalClearNotRejectedByMultiProviderGuard(
    _DaemonIndexCommitsTestBase
):
    """Two-provider config -- the semantic-only single-provider guard must
    NOT reject a temporal clear."""

    embedding_providers = ["voyage-ai", "cohere"]

    def test_temporal_clear_not_rejected_in_multiprovider_repo(self):
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=0,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
            ) as mock_check,
        ):
            result = self._invoke(["index", "--index-commits", "--clear"])

        self.assertEqual(
            result.exit_code,
            0,
            "a two-provider repo's temporal clear must not be rejected by "
            f"the semantic-only single-provider guard: {result.output}",
        )
        mock_index_via_daemon.assert_called_once()
        mock_check.assert_not_called()


class TestDaemonSemanticClearStillGuardedAndChecked(_DaemonIndexCommitsTestBase):
    """Regression: a genuine SEMANTIC daemon clear (index_commits=False)
    must be completely unaffected by this fix."""

    embedding_providers = ["voyage-ai", "cohere"]

    def test_semantic_clear_still_rejected_by_multiprovider_guard(self):
        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon",
            return_value=0,
        ) as mock_index_via_daemon:
            result = self._invoke(["index", "--clear"])

        self.assertEqual(result.exit_code, 1, result.output)
        mock_index_via_daemon.assert_not_called()


class TestDaemonSemanticClearStillRunsRebuildCheck(_DaemonIndexCommitsTestBase):
    """Regression: a genuine SEMANTIC daemon clear (index_commits=False, one
    provider) must still run the rebuild check exactly as before."""

    embedding_providers = None

    def test_semantic_clear_still_calls_rebuild_check(self):
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=0,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
                return_value=[],
            ) as mock_check,
        ):
            result = self._invoke(["index", "--clear"])

        self.assertEqual(result.exit_code, 0, result.output)
        mock_index_via_daemon.assert_called_once()
        mock_check.assert_called_once()


class TestTemporalShardedJsonNotDoubleRejected(_DaemonIndexCommitsTestBase):
    """`--index-commits --clear --new-collection-layout=sharded_json` must
    be rejected by temporal's OWN Bug #1529 guard
    (`reject_sharded_json_for_temporal`), not the round-7 semantic `--clear`
    + `sharded_json` conflict guard -- same rejection point and message as
    before #1979 touched this code at all."""

    embedding_providers = None

    def test_temporal_sharded_json_rejected_with_temporal_message(self):
        with patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon",
            return_value=0,
        ) as mock_index_via_daemon:
            result = self._invoke(
                [
                    "index",
                    "--index-commits",
                    "--clear",
                    "--new-collection-layout",
                    "sharded_json",
                ]
            )

        self.assertEqual(result.exit_code, 1, result.output)
        mock_index_via_daemon.assert_not_called()
        lowered = result.output.lower()
        assert "temporal" in lowered, (
            "must be rejected by temporal's own Bug #1529 guard "
            f"(reject_sharded_json_for_temporal), not re-messaged by the "
            f"round-7 semantic --clear conflict guard: {result.output}"
        )
        assert "index-commits" in lowered, result.output


if __name__ == "__main__":
    unittest.main()
