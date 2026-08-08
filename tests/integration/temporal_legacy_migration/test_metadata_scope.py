from pathlib import Path

from code_indexer.storage.temporal_metadata_sqlite_backend import (
    TemporalMetadataSqliteBackend,
)


def test_sqlite_scope_copy_and_delete_use_real_database(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    backend = TemporalMetadataSqliteBackend(source)
    backend.save_metadata("point-1", {"commit_hash": "abc", "path": "f.py"})

    backend.copy_collection_scope(target)
    copied = TemporalMetadataSqliteBackend(target)
    assert copied.count_entries() == 1

    copied.delete_collection_scope()
    assert not (target / "temporal_metadata.db").exists()
