"""Tests for force re-index and incremental catch-up (Story #632).

TDD: These tests are written BEFORE the implementation to drive the design.

Covers:
- clear_all_temporal_collections: clears all provider temporal dirs on --force
- incremental catch-up: new provider has empty progress (all commits needed),
  existing provider has full progress (0 commits needed)
"""

from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vector_store_mock():
    """Return a MagicMock that records clear_collection calls."""
    mock = MagicMock()
    mock.clear_collection.return_value = True
    return mock


def _make_index_path_with_dirs(tmp_path: Path, dir_names: list) -> Path:
    """Create index_path with given subdirectory names and return index_path."""
    index_path = tmp_path / ".code-indexer" / "index"
    index_path.mkdir(parents=True)
    for name in dir_names:
        (index_path / name).mkdir()
    return index_path


# ---------------------------------------------------------------------------
# clear_all_temporal_collections
# ---------------------------------------------------------------------------


def test_clear_all_temporal_collections_clears_all(tmp_path):
    """All provider-aware and legacy temporal dirs are cleared."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        clear_all_temporal_collections,
    )

    index_path = _make_index_path_with_dirs(
        tmp_path,
        [
            "code-indexer-temporal",
            "code-indexer-temporal-voyage_code_3",
            "code-indexer-temporal-embed_v4_0",
            "voyage-code-3",  # NOT temporal — must NOT be cleared
        ],
    )
    vector_store = _make_vector_store_mock()

    count = clear_all_temporal_collections(index_path, vector_store)

    assert count == 3
    cleared_names = {
        c.kwargs["collection_name"]
        for c in vector_store.clear_collection.call_args_list
    }
    assert "code-indexer-temporal" in cleared_names
    assert "code-indexer-temporal-voyage_code_3" in cleared_names
    assert "code-indexer-temporal-embed_v4_0" in cleared_names
    assert "voyage-code-3" not in cleared_names


def test_clear_all_temporal_collections_clears_progress_and_meta(tmp_path):
    """temporal_progress.json and temporal_meta.json are deleted from each temporal dir."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        clear_all_temporal_collections,
    )

    index_path = _make_index_path_with_dirs(
        tmp_path,
        ["code-indexer-temporal-voyage_code_3"],
    )
    collection_dir = index_path / "code-indexer-temporal-voyage_code_3"
    progress_file = collection_dir / "temporal_progress.json"
    meta_file = collection_dir / "temporal_meta.json"
    progress_file.write_text('{"completed_commits": []}')
    meta_file.write_text("{}")

    vector_store = _make_vector_store_mock()
    clear_all_temporal_collections(index_path, vector_store)

    assert not progress_file.exists(), "temporal_progress.json must be deleted"
    assert not meta_file.exists(), "temporal_meta.json must be deleted"


def test_clear_all_temporal_collections_returns_count(tmp_path):
    """Returns the exact number of temporal collections cleared."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        clear_all_temporal_collections,
    )

    index_path = _make_index_path_with_dirs(
        tmp_path,
        [
            "code-indexer-temporal-voyage_code_3",
            "code-indexer-temporal-embed_v4_0",
        ],
    )
    vector_store = _make_vector_store_mock()

    count = clear_all_temporal_collections(index_path, vector_store)

    assert count == 2


def test_clear_all_temporal_collections_empty_dir_returns_zero(tmp_path):
    """Returns 0 when index_path exists but has no temporal collection dirs."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        clear_all_temporal_collections,
    )

    index_path = _make_index_path_with_dirs(
        tmp_path,
        ["voyage-code-3", "some-other-dir"],
    )
    vector_store = _make_vector_store_mock()

    count = clear_all_temporal_collections(index_path, vector_store)

    assert count == 0
    vector_store.clear_collection.assert_not_called()


def test_clear_all_temporal_collections_nonexistent_dir_returns_zero(tmp_path):
    """Returns 0 when index_path does not exist."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        clear_all_temporal_collections,
    )

    index_path = tmp_path / "nonexistent"
    vector_store = _make_vector_store_mock()

    count = clear_all_temporal_collections(index_path, vector_store)

    assert count == 0
    vector_store.clear_collection.assert_not_called()


def test_clear_all_temporal_collections_ignores_non_temporal(tmp_path):
    """Non-temporal dirs like 'voyage-code-3' are never cleared."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        clear_all_temporal_collections,
    )

    index_path = _make_index_path_with_dirs(
        tmp_path,
        [
            "voyage-code-3",
            "embed-v4-0",
            "some-random-collection",
            "code-indexer",  # has prefix but NOT a temporal collection
        ],
    )
    vector_store = _make_vector_store_mock()

    count = clear_all_temporal_collections(index_path, vector_store)

    assert count == 0
    vector_store.clear_collection.assert_not_called()


# ---------------------------------------------------------------------------
# Incremental catch-up: new provider has empty progress
# ---------------------------------------------------------------------------


def test_incremental_catchup_new_provider_empty_progress(tmp_path):
    """A new provider collection dir with no progress file returns empty completed set.

    An empty completed set means ALL commits must be indexed for this provider
    (full catch-up required).
    """
    from code_indexer.services.temporal.temporal_progressive_metadata import (
        TemporalProgressiveMetadata,
    )

    new_provider_dir = tmp_path / "code-indexer-temporal-new_provider_model"
    new_provider_dir.mkdir(parents=True)

    metadata = TemporalProgressiveMetadata(new_provider_dir)
    completed = metadata.load_completed()

    assert completed == set(), (
        "New provider with no progress file must have empty completed set, "
        "indicating all commits need indexing (catch-up required)"
    )


def test_incremental_catchup_existing_provider_skips(tmp_path):
    """An existing provider that indexed all commits leaves 0 commits remaining.

    The incremental indexer computes remaining = all_commits - completed.
    If remaining is empty, no catch-up is needed for this provider.
    """
    from code_indexer.services.temporal.temporal_progressive_metadata import (
        TemporalProgressiveMetadata,
    )

    all_commits = {"abc123", "def456", "ghi789"}

    existing_provider_dir = tmp_path / "code-indexer-temporal-voyage_code_3"
    existing_provider_dir.mkdir(parents=True)

    metadata = TemporalProgressiveMetadata(existing_provider_dir)
    for commit in sorted(all_commits):
        metadata.save_completed(commit)

    completed = metadata.load_completed()
    remaining = all_commits - completed

    assert remaining == set(), (
        "Existing provider with full progress must have 0 commits remaining, "
        "indicating no catch-up needed"
    )
