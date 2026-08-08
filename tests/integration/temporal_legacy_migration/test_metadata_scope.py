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


def test_content_digest_matches_between_identical_scopes_and_differs_on_divergence(
    tmp_path: Path,
):
    """Issue #1548 round-4 exploit 2 fix: ``content_digest()`` is the
    genuine, content-bound proof ``mover.py`` compares between a legacy
    and a fixed-root metadata scope before authorizing deletion --
    replacing a count-only check that unrelated non-empty data could
    satisfy. Two scopes holding field-for-field identical rows (via a
    real ``copy_collection_scope`` re-key) must digest equal; unrelated
    data must digest different, even with the same row COUNT.
    """
    source = tmp_path / "source"
    copied = tmp_path / "copied"
    unrelated = tmp_path / "unrelated"
    backend = TemporalMetadataSqliteBackend(source)
    backend.save_metadata("point-1", {"commit_hash": "abc", "path": "f.py"})
    backend.save_metadata("point-2", {"commit_hash": "def", "path": "g.py"})

    backend.copy_collection_scope(copied)
    copied_backend = TemporalMetadataSqliteBackend(copied)
    assert copied_backend.content_digest() == backend.content_digest()

    unrelated_backend = TemporalMetadataSqliteBackend(unrelated)
    unrelated_backend.save_metadata(
        "unrelated-1", {"commit_hash": "xyz", "path": "u.py"}
    )
    unrelated_backend.save_metadata(
        "unrelated-2", {"commit_hash": "uvw", "path": "v.py"}
    )
    assert unrelated_backend.count_entries() == backend.count_entries()
    assert unrelated_backend.content_digest() != backend.content_digest()
