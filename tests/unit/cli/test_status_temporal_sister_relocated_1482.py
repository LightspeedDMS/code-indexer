"""GitHub Issue #1482 (extension, site 4 -- LOW priority/future-proofing):
`cidx status`'s temporal-index display (`_status_impl`, cli.py) only ever
scans the local `.code-indexer/index/` directory for temporal
collections. When the codebase dir structurally IS a golden repo's own
clone (an operator running `cidx status` directly inside
`~/.cidx-server/data/golden-repos/{alias}`, bypassing the server -- see
temporal_sister_root_detection.py) and temporal data has relocated to the
golden-owned sister location (Story #1457 AC1), the status command
reports "Not configured" even though temporal data genuinely exists.

Mocking pattern mirrors test_status_temporal_index_display.py exactly
(Table/EmbeddingProviderFactory/FilesystemVectorStore patched,
_status_impl called directly with a real ConfigManager/Config). The
`.code-indexer/index/` directory itself is created empty as required
scaffolding (mirroring that file's own `filesystem_config_no_temporal`
fixture) -- the actual gap under test is that NO `code-indexer-temporal*`
collection SUBdirectory exists inside it, while a real sister-location
alias pointer + versioned snapshot dir (built via a real AliasManager)
genuinely does.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager

REPO_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"
FIXTURE_VERSION_TIMESTAMP = "1785164318"


@pytest.fixture
def sister_only_golden_clone_config(tmp_path: Path):
    """Golden-repo-shaped project dir (flat layout) with NO local temporal
    collection SUBdirectory, but real sister-location temporal data."""
    golden_repos_dir = tmp_path / "golden-repos"
    project = golden_repos_dir / REPO_ALIAS
    config_dir = project / ".code-indexer"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    config_data = {
        "codebase_dir": str(project),
        "embedding_provider": "voyage-ai",
        "embedding": {"model": "voyage-code-3", "dimensions": 1024},
        "vector_store": {"provider": "filesystem"},
    }
    config_path.write_text(json.dumps(config_data))

    # Required scaffolding (mirrors filesystem_config_no_temporal): the
    # index/ dir itself exists, empty -- no temporal collection
    # subdirectory inside it. That absence is the exact gap under test.
    index_path = config_dir / "index"
    index_path.mkdir(parents=True, exist_ok=True)

    version_dir = (
        golden_repos_dir
        / ".versioned"
        / POINTER_NAMESPACE
        / f"v_{FIXTURE_VERSION_TIMESTAMP}"
    )
    version_dir.mkdir(parents=True)
    (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
    alias_manager.create_alias(POINTER_NAMESPACE, str(version_dir))

    config_manager = ConfigManager(config_path)
    config = config_manager.load()
    return config_manager, config


@patch("code_indexer.cli.Table")
@patch("code_indexer.cli.EmbeddingProviderFactory")
def test_status_recognizes_sister_relocated_temporal_data(
    mock_embedding_factory,
    mock_table_class,
    sister_only_golden_clone_config,
):
    config_manager, config = sister_only_golden_clone_config

    mock_table = MagicMock()
    mock_table_class.return_value = mock_table

    mock_embedding = MagicMock()
    mock_embedding.get_provider_name.return_value = "voyage-ai"
    mock_embedding.get_current_model.return_value = "voyage-code-3"
    mock_embedding.health_check.return_value = True
    mock_embedding.get_model_info.return_value = {"dimensions": 1024}
    mock_embedding_factory.create.return_value = mock_embedding

    with patch(
        "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore"
    ) as mock_fs:
        mock_fs_instance = MagicMock()
        mock_fs_instance.health_check.return_value = True
        mock_fs_instance.collection_exists.side_effect = lambda name: False
        mock_fs_instance.resolve_collection_name.return_value = (
            "code-indexer-voyage-code-3-d1024"
        )
        mock_fs.return_value = mock_fs_instance

        from code_indexer.cli import _status_impl, cli

        ctx = click.Context(cli)
        ctx.obj = {"config_manager": config_manager}

        _status_impl(ctx)

    add_row_calls = list(mock_table.add_row.call_args_list)
    temporal_rows = [c for c in add_row_calls if c[0][0] == "Temporal Index"]

    assert temporal_rows, (
        f"No 'Temporal Index' row rendered at all; got rows: "
        f"{[c[0][0] for c in add_row_calls]}"
    )
    status_text = temporal_rows[0][0][1]
    assert "not configured" not in status_text.lower(), (
        "cidx status must recognize temporal data relocated to the "
        "golden-owned sister location (Bug #1482 extension) instead of "
        f"reporting 'Not configured'; got status: {status_text!r}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
