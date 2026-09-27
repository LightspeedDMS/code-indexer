"""Bug #1979 (P1, round 3): daemon-mode --clear never runs the shared
rebuild check.

The CLI's `daemon_enabled` branch delegates to `_index_via_daemon(...)`
then `sys.exit(exit_code)` -- entirely before the shared
`find_providers_not_rebuilt_since` check, which lives only in the
foreground `else:` branch. This means a daemon-enabled, two-provider repo
can report success (exit 0) even when a configured provider was never
genuinely rebuilt, and an image-only repo can report success with an
empty TEXT collection.

These tests drive the CLI front door in daemon mode (CliRunner, real
on-disk config, `_index_via_daemon` mocked -- no real daemon subprocess)
and mock the shared check itself (already covered at the unit level by
`tests/unit/services/test_provider_rebuild_physical_rows_1979.py` and the
daemon-mode branch by
`tests/unit/services/test_provider_rebuild_check_daemon_mode_1979.py`) so
these tests focus purely on the CLI WIRING: is the check called, with what
arguments, only under what conditions, and is its result acted on
correctly.

Bug #1979 (round 4): the daemon's real write path was reverted back to the
bare legacy `metadata.json` (see
`tests/unit/daemon/test_daemon_clear_metadata_path_1979.py`), so
`TestDaemonClearRealRebuildCheck` below -- which runs the REAL
`find_providers_not_rebuilt_since` end to end -- simulates the daemon
writing that SAME bare file, and the CLI call site passes
`daemon_mode=True` so the real check reads it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli


class _DaemonClearRebuildCheckTestBase(unittest.TestCase):
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

    def _invoke_clear(self, extra_args=()):
        runner = CliRunner()
        old_cwd = os.getcwd()
        os.chdir(str(self.project_dir))
        try:
            return runner.invoke(cli, ["index", "--clear", *extra_args])
        finally:
            os.chdir(old_cwd)


class TestDaemonClearRebuildCheckRunsOnSuccess(_DaemonClearRebuildCheckTestBase):
    """Two configured providers.

    Bug #1979 (P2, round 4) coordinator ruling: a multi-provider daemon
    `--clear` is now rejected BEFORE delegation instead of relying on this
    post-delegation rebuild check to catch the provider the daemon never
    rebuilt (the daemon only ever constructs one provider/SmartIndexer, so
    that scenario -- one provider skipped after "successful" delegation --
    can no longer occur here). The dedicated guard tests live in
    tests/unit/cli/test_cli_daemon_multi_provider_clear_guard_1979.py; this
    test now proves the guard fires first and neither the delegate nor the
    rebuild check ever runs for this two-provider setup.
    """

    embedding_providers = ["voyage-ai", "cohere"]

    def test_skipped_provider_fails_after_successful_daemon_delegation(self):
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=0,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
                return_value=["cohere"],
            ) as mock_check,
        ):
            result = self._invoke_clear()

        mock_index_via_daemon.assert_not_called()
        mock_check.assert_not_called()
        self.assertEqual(
            result.exit_code,
            1,
            f"expected the pre-delegation multi-provider guard to reject this run, got: {result.output}",
        )
        self.assertIn("daemon mode", result.output)


class TestDaemonClearRebuildCheckPassesOnGenuineSuccess(
    _DaemonClearRebuildCheckTestBase
):
    """Single configured provider, genuinely rebuilt -- must NOT false-fail."""

    embedding_providers = None  # defaults to ["voyage-ai"]

    def test_single_provider_genuine_success_exits_zero(self):
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
            result = self._invoke_clear()

        mock_index_via_daemon.assert_called_once()
        mock_check.assert_called_once()
        self.assertEqual(
            result.exit_code,
            0,
            f"a genuine single-provider success must not be false-failed: {result.output}",
        )


class TestDaemonClearRebuildCheckSkippedOnDaemonFailure(
    _DaemonClearRebuildCheckTestBase
):
    """A daemon failure must not be double-reported as a rebuild-check failure."""

    embedding_providers = None

    def test_daemon_failure_short_circuits_before_rebuild_check(self):
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=1,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
            ) as mock_check,
        ):
            result = self._invoke_clear()

        mock_index_via_daemon.assert_called_once()
        mock_check.assert_not_called()
        self.assertEqual(result.exit_code, 1)


class TestDaemonNonClearRunNeverCallsRebuildCheck(_DaemonClearRebuildCheckTestBase):
    """A plain (non-`--clear`) daemon run must never invoke the rebuild check."""

    embedding_providers = None

    def test_incremental_daemon_run_skips_rebuild_check(self):
        runner = CliRunner()
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                return_value=0,
            ) as mock_index_via_daemon,
            patch(
                "code_indexer.services.provider_rebuild_check.find_providers_not_rebuilt_since",
            ) as mock_check,
        ):
            old_cwd = os.getcwd()
            os.chdir(str(self.project_dir))
            try:
                result = runner.invoke(cli, ["index"])
            finally:
                os.chdir(old_cwd)

        mock_index_via_daemon.assert_called_once()
        mock_check.assert_not_called()
        self.assertEqual(result.exit_code, 0)


class TestDaemonClearRealRebuildCheck(_DaemonClearRebuildCheckTestBase):
    """The CLI and the physical-data checker work together after delegation."""

    def _invoke_with_selected_provider_rows(self, *, has_text_rows, providers):
        self.embedding_providers = providers
        config_dir = self.project_dir / ".code-indexer"
        config_data = json.loads((config_dir / "config.json").read_text())
        config_data["embedding_providers"] = providers
        (config_dir / "config.json").write_text(json.dumps(config_data))

        collection = config_dir / "index" / "example-text"
        collection.mkdir(parents=True)

        def daemon_completed(**_kwargs):
            # Bug #1979 (round 4): the daemon writes the bare legacy
            # filename (reverted from round 3's per-provider write).
            (config_dir / "metadata.json").write_text(
                json.dumps({"status": "completed", "last_index_timestamp": time.time()})
            )
            if has_text_rows:
                (collection / "vector_example.json").write_text(
                    '{"id": "example-chunk"}'
                )
            return 0

        vector_store = MagicMock()
        vector_store.resolve_collection_name.return_value = "example-text"
        vector_store._get_collection_path.return_value = collection
        backend = MagicMock()
        backend.get_vector_store_client.return_value = vector_store
        with (
            patch(
                "code_indexer.cli_daemon_delegation._index_via_daemon",
                side_effect=daemon_completed,
            ),
            patch("code_indexer.cli.BackendFactory.create", return_value=backend),
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
                return_value=MagicMock(),
            ),
        ):
            return self._invoke_clear()

    def test_one_provider_with_committed_text_rows_succeeds(self):
        result = self._invoke_with_selected_provider_rows(
            has_text_rows=True, providers=["voyage-ai"]
        )
        self.assertEqual(result.exit_code, 0, result.output)

    def test_multi_provider_daemon_clear_rejected_before_real_check(self):
        """Bug #1979 (P2, round 4): two configured providers now never reach
        the real rebuild check in daemon mode -- the pre-delegation guard
        (see test_cli_daemon_multi_provider_clear_guard_1979.py) rejects the
        run first, so `_index_via_daemon`'s side effect never fires and no
        provider name is available to mention."""
        result = self._invoke_with_selected_provider_rows(
            has_text_rows=True, providers=["voyage-ai", "cohere"]
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("daemon mode", result.output)

    def test_image_only_run_with_no_text_rows_fails(self):
        result = self._invoke_with_selected_provider_rows(
            has_text_rows=False, providers=["voyage-ai"]
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("voyage-ai", result.output)


if __name__ == "__main__":
    unittest.main()
