"""Tests for `cidx status` id_index.bin layout-awareness (Issue #1459 AC1/AC5).

For a CHUNKS_DB-layout collection, `id_index.bin` is PERMANENTLY, DELIBERATELY
never written (Story #1456's "id_index.bin retirement for CHUNKS_DB
collections"). The status display and its downstream recovery-guidance
renderer must NOT warn about / recommend "rebuilding" id_index.bin for such a
collection -- that would be bogus advice. A legacy SHARDED_JSON collection
missing id_index.bin is a real, unchanged warning condition.

Real on-disk fixtures are used throughout (no mocking of the layout resolver
or the filesystem) -- only FilesystemVectorStore's network-touching methods
(embedding health check, count_points, etc.) are stubbed, matching the
pre-existing pattern in test_status_temporal_index_display.py.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click

from code_indexer.config import ConfigManager


def _write_config(tmp_path: Path) -> ConfigManager:
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    config_data = {
        "codebase_dir": str(tmp_path),
        "embedding_provider": "voyage-ai",
        "embedding": {"model": "voyage-code-3", "dimensions": 1024},
        "vector_store": {"provider": "filesystem"},
    }
    config_path.write_text(json.dumps(config_data))

    index_path = tmp_path / ".code-indexer" / "index"
    index_path.mkdir(parents=True, exist_ok=True)

    return ConfigManager(config_path)


def _write_chunks_db_discriminator(collection_dir: Path) -> None:
    """Write collection_meta.json with a valid CHUNKS_DB discriminator."""
    collection_dir.mkdir(parents=True, exist_ok=True)
    meta_path = collection_dir / "collection_meta.json"
    meta_path.write_text(json.dumps({"chunks_db": {"version": 1}}))


def _run_status_impl(config_manager: ConfigManager, mock_fs_instance: MagicMock):
    from code_indexer.cli import cli, _status_impl

    mock_table = MagicMock()
    with (
        patch("code_indexer.cli.Table", return_value=mock_table),
        patch("code_indexer.cli.EmbeddingProviderFactory") as mock_embedding_factory,
        patch(
            "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore",
            return_value=mock_fs_instance,
        ),
    ):
        mock_embedding = MagicMock()
        mock_embedding.get_provider_name.return_value = "voyage-ai"
        mock_embedding.get_current_model.return_value = "voyage-code-3"
        mock_embedding.health_check.return_value = True
        mock_embedding.get_model_info.return_value = {"dimensions": 1024}
        mock_embedding_factory.create.return_value = mock_embedding

        ctx = click.Context(cli)
        ctx.obj = {"config_manager": config_manager}
        _status_impl(ctx)

    return mock_table


def _base_fs_mock(collection_name: str) -> MagicMock:
    mock_fs_instance = MagicMock()
    mock_fs_instance.health_check.return_value = True
    mock_fs_instance.collection_exists.side_effect = (
        lambda name: name == collection_name
    )
    mock_fs_instance.resolve_collection_name.return_value = collection_name
    mock_fs_instance.count_points.return_value = 10
    mock_fs_instance.get_indexed_file_count_fast.return_value = 3
    mock_fs_instance.validate_embedding_dimensions.return_value = True
    return mock_fs_instance


def _index_files_text(mock_table: MagicMock) -> str:
    for call_args in mock_table.add_row.call_args_list:
        if call_args[0][0] == "Index Files":
            return str(call_args[0][2])
    raise AssertionError("Index Files row not found in table.add_row calls")


class TestSemanticIdIndexLayoutAwareness:
    def test_sharded_json_missing_id_index_still_warns(self, tmp_path):
        """Legacy SHARDED_JSON layout: missing id_index.bin is a REAL warning
        (unchanged pre-#1459 behavior)."""
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        collection_dir.mkdir(parents=True, exist_ok=True)
        # No collection_meta.json chunks_db discriminator -> resolves SHARDED_JSON.
        # No id_index.bin written -> missing.

        mock_fs_instance = _base_fs_mock(collection_name)
        mock_table = _run_status_impl(config_manager, mock_fs_instance)

        index_files_text = _index_files_text(mock_table)
        assert "ID Index: ⚠️ Missing (rebuilds automatically)" in index_files_text

    def test_chunks_db_missing_id_index_does_not_warn(self, tmp_path):
        """CHUNKS_DB layout: missing id_index.bin is EXPECTED/PERMANENT -- must
        not be displayed as a warning."""
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        _write_chunks_db_discriminator(collection_dir)
        # No id_index.bin written -- deliberately retired for CHUNKS_DB.

        mock_fs_instance = _base_fs_mock(collection_name)
        mock_table = _run_status_impl(config_manager, mock_fs_instance)

        index_files_text = _index_files_text(mock_table)
        assert "ID Index: ⚠️ Missing (rebuilds automatically)" not in index_files_text
        assert "N/A" in index_files_text or "consolidated" in index_files_text.lower()

    def test_chunks_db_present_id_index_unaffected(self, tmp_path):
        """CHUNKS_DB layout with an id_index.bin present (shouldn't normally
        happen, but must not crash and should still report the real file if
        present) -- exercised for completeness/regression safety."""
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        _write_chunks_db_discriminator(collection_dir)
        (collection_dir / "id_index.bin").write_bytes(b"x" * 1024)

        mock_fs_instance = _base_fs_mock(collection_name)
        mock_table = _run_status_impl(config_manager, mock_fs_instance)

        index_files_text = _index_files_text(mock_table)
        assert "ID Index: ✅" in index_files_text


class TestRecoveryGuidanceLayoutAwareness:
    def test_sharded_json_missing_id_index_shows_recovery_instruction(
        self, tmp_path, capsys
    ):
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        collection_dir.mkdir(parents=True, exist_ok=True)

        mock_fs_instance = _base_fs_mock(collection_name)
        _run_status_impl(config_manager, mock_fs_instance)

        captured = capsys.readouterr()
        assert "ID Index (affects point lookups)" in captured.out

    def test_chunks_db_missing_id_index_no_bogus_recovery_instruction(
        self, tmp_path, capsys
    ):
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        _write_chunks_db_discriminator(collection_dir)

        mock_fs_instance = _base_fs_mock(collection_name)
        _run_status_impl(config_manager, mock_fs_instance)

        captured = capsys.readouterr()
        assert "ID Index (affects point lookups)" not in captured.out


class TestMultimodalIdIndexLayoutAwareness:
    def test_multimodal_chunks_db_missing_id_index_does_not_warn(self, tmp_path):
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        collection_dir.mkdir(parents=True, exist_ok=True)

        multimodal_dir = tmp_path / ".code-indexer" / "index" / "voyage-multimodal-3"
        _write_chunks_db_discriminator(multimodal_dir)

        mock_fs_instance = _base_fs_mock(collection_name)
        # Multimodal collection also "exists" for count/file-count stubs.
        mock_fs_instance.collection_exists.side_effect = lambda name: name in (
            collection_name,
            "voyage-multimodal-3",
        )

        mock_table = _run_status_impl(config_manager, mock_fs_instance)

        multimodal_text = None
        for call_args in mock_table.add_row.call_args_list:
            if call_args[0][0] == "Multimodal Index Files":
                multimodal_text = call_args[0][2]
        assert multimodal_text is not None, "Multimodal Index Files row not found"
        assert "ID Index: ⚠️ Missing" not in multimodal_text

    def test_multimodal_sharded_json_missing_id_index_still_warns(self, tmp_path):
        config_manager = _write_config(tmp_path)
        collection_name = "code-indexer-voyage-code-3-d1024"
        collection_dir = tmp_path / ".code-indexer" / "index" / collection_name
        collection_dir.mkdir(parents=True, exist_ok=True)

        multimodal_dir = tmp_path / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_dir.mkdir(parents=True, exist_ok=True)
        # No chunks_db discriminator -> SHARDED_JSON; no id_index.bin -> missing.

        mock_fs_instance = _base_fs_mock(collection_name)
        mock_fs_instance.collection_exists.side_effect = lambda name: name in (
            collection_name,
            "voyage-multimodal-3",
        )

        mock_table = _run_status_impl(config_manager, mock_fs_instance)

        multimodal_text = None
        for call_args in mock_table.add_row.call_args_list:
            if call_args[0][0] == "Multimodal Index Files":
                multimodal_text = call_args[0][2]
        assert multimodal_text is not None, "Multimodal Index Files row not found"
        assert "ID Index: ⚠️ Missing" in multimodal_text
