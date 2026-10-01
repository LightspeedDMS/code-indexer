"""Indexer resume-state trust and containment.

Daemon-mode coverage: `cli.py`'s
`daemon_enabled` branch delegates to `_index_via_daemon(...)`
(cli_daemon_delegation.py) WITHOUT threading `ignore_resume_state`. The
daemon side (`daemon/service.py`) then calls `smart_index()` with the
default `trust_resume_state=True`. Because `config.daemon.enabled` is read
from `.code-indexer/config.json` -- a file inside the repository working
tree -- repository-local config must never be able to change
server-managed resume behaviour for any server-spawned run.

Fix, fail closed: `daemon_enabled` is gated on `not ignore_resume_state`
too, so passing --ignore-resume-state ALWAYS takes the local path
(regardless of the daemon config), which already correctly threads
`trust_resume_state=False` into `SmartIndexer.smart_index()`.

This test drives the REAL `cidx index` command with a config claiming
daemon mode is enabled, and asserts NO daemon delegation happens and
smart_index() is called in-process with trust_resume_state=False.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch


def _make_daemon_config(codebase_dir: str) -> MagicMock:
    cfg = MagicMock()
    cfg.codebase_dir = codebase_dir
    cfg.embedding_provider = "voyage-ai"
    cfg.embedding_providers = None

    cfg.voyage_ai = MagicMock()
    cfg.voyage_ai.parallel_requests = 8

    cfg.vector_store = None
    cfg.daemon = MagicMock()
    cfg.daemon.enabled = True
    cfg.daemon.model_dump.return_value = {"enabled": True}

    cfg.get_embedding_providers = lambda: ["voyage-ai"]
    return cfg


def _make_indexer() -> MagicMock:
    stats = MagicMock()
    stats.duration = 1.0
    stats.files_processed = 1
    stats.chunks_created = 1
    stats.failed_files = 0
    stats.cancelled = False

    indexer = MagicMock()
    indexer.smart_index.return_value = stats
    indexer.get_git_status.return_value = {
        "git_available": False,
        "project_id": "test-proj",
    }
    indexer.get_indexing_status.return_value = {
        "status": "completed",
        "can_resume": False,
        "files_processed": 0,
        "chunks_indexed": 0,
    }
    indexer.slot_tracker = None
    return indexer


@contextlib.contextmanager
def _index_test_env(tmp_path, cfg, mock_indexer, mock_index_via_daemon):
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "metadata.json").write_text("{}")

    with (
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.resolve_api_key",
            return_value="key-123",
        ),
        patch(
            "code_indexer.cli.EmbeddingProviderFactory.create",
            return_value=MagicMock(
                health_check=lambda test_api=False: True,
                get_provider_name=lambda: "voyage-ai",
                get_current_model=lambda: "voyage-3",
                get_model_info=lambda: {},
            ),
        ),
        patch(
            "code_indexer.cli.BackendFactory.create",
            return_value=MagicMock(
                health_check=lambda: True,
                get_vector_store_client=lambda: MagicMock(),
            ),
        ),
        patch(
            "code_indexer.services.smart_indexer.SmartIndexer",
            return_value=mock_indexer,
        ),
        patch("code_indexer.cli.ConfigManager") as mock_cm,
        patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
        patch(
            "code_indexer.progress.multi_threaded_display.MultiThreadedProgressManager"
        ),
        patch(
            "code_indexer.cli_daemon_delegation._index_via_daemon",
            mock_index_via_daemon,
        ),
    ):
        mock_cm.create_with_backtrack.return_value.load.return_value = cfg
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )

        from click.testing import CliRunner

        yield CliRunner()


class TestIgnoreResumeStateBypassesDaemon:
    """--ignore-resume-state must never delegate
    to a daemon whose enablement is read from tenant-writable config."""

    def test_ignore_resume_state_with_daemon_enabled_skips_daemon_delegation(
        self, tmp_path
    ) -> None:
        cfg = _make_daemon_config(str(tmp_path))
        mock_indexer = _make_indexer()
        mock_index_via_daemon = MagicMock(return_value=0)

        with _index_test_env(
            tmp_path, cfg, mock_indexer, mock_index_via_daemon
        ) as runner:
            from code_indexer.cli import cli

            result = runner.invoke(cli, ["index", "--ignore-resume-state"])

        mock_index_via_daemon.assert_not_called()
        assert mock_indexer.smart_index.call_count == 1, result.output
        _, kwargs = mock_indexer.smart_index.call_args
        assert kwargs.get("trust_resume_state") is False, (
            "SECURITY: --ignore-resume-state with daemon config enabled "
            "must still run the local path with trust_resume_state=False, "
            f"never delegate to the daemon. Got kwargs={kwargs}"
        )

    def test_daemon_enabled_without_flag_still_delegates_to_daemon(
        self, tmp_path
    ) -> None:
        """Positive control: daemon delegation is untouched when
        --ignore-resume-state is NOT passed (no behavior regression)."""
        cfg = _make_daemon_config(str(tmp_path))
        mock_indexer = _make_indexer()
        mock_index_via_daemon = MagicMock(return_value=0)

        with _index_test_env(
            tmp_path, cfg, mock_indexer, mock_index_via_daemon
        ) as runner:
            from code_indexer.cli import cli

            runner.invoke(cli, ["index"])

        mock_index_via_daemon.assert_called_once()
        mock_indexer.smart_index.assert_not_called()
