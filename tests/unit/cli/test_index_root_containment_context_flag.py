"""`cidx index` marks server-spawned runs as server context.

Server-spawned `cidx index` runs carry the internal server flags stamped by
`append_server_layout_args` (`--ignore-resume-state` and
`--server-managed-provider-settings`). Either flag marks the loaded Config as
confined to the codebase root, so out-of-root symlinks are never indexed on
the server. A plain local `cidx index` leaves the Config unmarked and follows
symlinks.

Drives the REAL `cidx index` command through click's CliRunner with real
`Config` objects; only the embedding provider, backend and SmartIndexer
collaborators are stood in (the pattern used by
test_server_managed_provider_settings_flag.py), and the Config each
SmartIndexer construction receives is captured.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator, List
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from code_indexer.config import Config


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
def _index_env(tmp_path: Path, indexer_configs: List[Config]) -> Iterator[CliRunner]:
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "metadata.json").write_text("{}")
    mock_indexer = _make_indexer()

    def _smart_indexer_factory(config: Config, *args: object, **kwargs: object):
        indexer_configs.append(config)
        return mock_indexer

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
            side_effect=_smart_indexer_factory,
        ),
        patch("code_indexer.cli.ConfigManager") as mock_cm,
        patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
        patch(
            "code_indexer.progress.multi_threaded_display.MultiThreadedProgressManager"
        ),
    ):
        mock_cm.create_with_backtrack.return_value.load.side_effect = lambda: Config(
            codebase_dir=tmp_path
        )
        mock_cm.create_with_backtrack.return_value.config_path = (
            config_dir / "config.json"
        )
        yield CliRunner()


@pytest.mark.parametrize(
    "flags",
    [
        ["--ignore-resume-state"],
        ["--server-managed-provider-settings"],
        ["--ignore-resume-state", "--server-managed-provider-settings"],
    ],
)
def test_server_flag_confines_indexing_to_codebase_root(
    tmp_path: Path, flags: List[str]
) -> None:
    indexer_configs: List[Config] = []
    with _index_env(tmp_path, indexer_configs) as runner:
        from code_indexer.cli import cli

        result = runner.invoke(cli, ["index", *flags])

    assert result.exit_code == 0, result.output
    assert indexer_configs, result.output
    assert all(c.confined_to_codebase_root for c in indexer_configs), (
        f"A server-spawned run ({flags}) must confine indexing to the codebase root."
    )


def test_local_index_follows_symlinks_outside_codebase_root(tmp_path: Path) -> None:
    indexer_configs: List[Config] = []
    with _index_env(tmp_path, indexer_configs) as runner:
        from code_indexer.cli import cli

        result = runner.invoke(cli, ["index"])

    assert result.exit_code == 0, result.output
    assert indexer_configs, result.output
    assert not any(c.confined_to_codebase_root for c in indexer_configs)
