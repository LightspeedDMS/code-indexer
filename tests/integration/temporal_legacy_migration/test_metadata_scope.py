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


def test_copy_collection_scope_is_a_row_level_reentrant_re_key(tmp_path: Path):
    """Issue #1548 blocker 3: copy_collection_scope must be a genuine
    row-level copy -- the source is never mutated, a repeated call against
    unchanged source data is a no-op, and re-running it against source data
    that has since grown (as happens on a resumed migration pass) picks up
    the new rows without depending on the destination's prior state.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    backend = TemporalMetadataSqliteBackend(source)
    backend.save_metadata("point-1", {"commit_hash": "abc", "path": "f.py"})

    backend.copy_collection_scope(target)
    backend.copy_collection_scope(target)  # repeat with unchanged source data
    copied = TemporalMetadataSqliteBackend(target)
    assert copied.count_entries() == 1, "repeating an unchanged copy must be a no-op"

    backend.save_metadata("point-2", {"commit_hash": "def", "path": "g.py"})
    backend.copy_collection_scope(target)

    assert backend.count_entries() == 2, "source must never be mutated by a copy"
    assert copied.count_entries() == 2

    # Deleting the source scope must never touch the already-copied target.
    backend.delete_collection_scope()
    assert not (source / "temporal_metadata.db").exists()
    assert copied.count_entries() == 2


def test_copy_collection_scope_of_empty_source_is_a_safe_no_op(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    backend = TemporalMetadataSqliteBackend(source)

    backend.copy_collection_scope(target)

    assert not (target / "temporal_metadata.db").exists()
