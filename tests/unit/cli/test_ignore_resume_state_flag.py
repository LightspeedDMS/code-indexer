"""Indexer resume-state trust and containment.

Mechanism: `cidx index` gains a new internal
`--ignore-resume-state` flag that threads `trust_resume_state=False` into
`SmartIndexer.smart_index()`. This is the CLI-level half of the fix --
server call sites (golden_repo_manager.py, activated_repo_index_manager.py)
pass this flag on every server-spawned `cidx index` invocation so they
never trust a committer/tenant-authored `.code-indexer/metadata-<provider>.json`
resume state.

This test drives the REAL `cidx index` command (via click's CliRunner)
through its real flag-parsing and argument-passing wiring, with SmartIndexer
itself replaced by a recording spy (mirroring the accepted pattern in
tests/unit/cli/test_cli_multi_provider_index.py) -- only the collaborator is
stood in, the SUT (cli.py's flag threading) runs for real.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch


def _make_config(codebase_dir: str) -> MagicMock:
    cfg = MagicMock()
    cfg.codebase_dir = codebase_dir
    cfg.embedding_provider = "voyage-ai"
    cfg.embedding_providers = None

    cfg.voyage_ai = MagicMock()
    cfg.voyage_ai.parallel_requests = 8

    cfg.vector_store = None
    cfg.daemon = None  # standalone (local) index path

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
def _index_test_env(tmp_path, cfg, mock_indexer):
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
    ):
        mock_cm.create_with_backtrack.return_value.load.return_value = cfg
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )

        from click.testing import CliRunner

        yield CliRunner()


class TestIgnoreResumeStateFlag:
    """--ignore-resume-state threads
    trust_resume_state=False into SmartIndexer.smart_index()."""

    def test_ignore_resume_state_flag_threads_trust_resume_state_false(
        self, tmp_path
    ) -> None:
        cfg = _make_config(str(tmp_path))
        mock_indexer = _make_indexer()

        with _index_test_env(tmp_path, cfg, mock_indexer) as runner:
            from code_indexer.cli import cli

            result = runner.invoke(cli, ["index", "--ignore-resume-state"])

        assert mock_indexer.smart_index.call_count == 1, result.output
        _, kwargs = mock_indexer.smart_index.call_args
        assert kwargs.get("trust_resume_state") is False, (
            "Expected --ignore-resume-state to thread "
            f"trust_resume_state=False into smart_index(); got kwargs={kwargs}"
        )

    def test_default_index_trusts_resume_state(self, tmp_path) -> None:
        cfg = _make_config(str(tmp_path))
        mock_indexer = _make_indexer()

        with _index_test_env(tmp_path, cfg, mock_indexer) as runner:
            from code_indexer.cli import cli

            result = runner.invoke(cli, ["index"])

        assert mock_indexer.smart_index.call_count == 1, result.output
        _, kwargs = mock_indexer.smart_index.call_args
        assert kwargs.get("trust_resume_state") is True, (
            "Expected plain `cidx index` (no flag) to keep "
            f"trust_resume_state=True; got kwargs={kwargs}"
        )
