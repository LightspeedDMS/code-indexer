"""Integration tests for CLI status temporal display and fusion dispatch wiring (Story #636).

Covers:
- get_temporal_collections() enumerates per-provider temporal dirs and returns stats
- TemporalProgressiveMetadata.load_progress() returns commit count and state
- get_temporal_collections() returns empty list when no temporal dirs exist
- execute_temporal_query_with_fusion importability

Issue #1398: TEMPORAL_QUERY_TIMEOUT_SECONDS (confirmed dead code -- Story
#1291 removed its only consumer) was deleted entirely from
temporal_fusion_dispatch.py; its "constant value" test was removed here
accordingly (see test_temporal_query_timeout_seconds_removed_1398.py for
the proof that it no longer exists in source).
"""

from code_indexer.services.temporal.temporal_collection_naming import (
    get_temporal_collections,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)


# ---------------------------------------------------------------------------
# test_get_temporal_collections_returns_stats_per_provider
# ---------------------------------------------------------------------------


def test_get_temporal_collections_returns_stats_per_provider(tmp_path):
    """Two provider dirs in index_path → get_temporal_collections returns both."""
    index_path = tmp_path / "index"
    index_path.mkdir()

    voyage_dir = index_path / "code-indexer-temporal-voyage_code_3"
    voyage_dir.mkdir()
    cohere_dir = index_path / "code-indexer-temporal-embed_v4_0"
    cohere_dir.mkdir()

    # Write progress files for each provider.
    # flush_pending() required: mark_commit_indexed stages in _pending (in-memory);
    # load_progress() reads from disk via _load().  A real completed run always
    # calls the final flush — mirror that here.
    voyage_meta = TemporalProgressiveMetadata(voyage_dir)
    voyage_meta.mark_commit_indexed("aaa111")
    voyage_meta.mark_commit_indexed("bbb222")
    voyage_meta.flush_pending()

    cohere_meta = TemporalProgressiveMetadata(cohere_dir)
    cohere_meta.mark_commit_indexed("ccc333")
    cohere_meta.flush_pending()

    config = object()  # config is not used by get_temporal_collections
    collections = get_temporal_collections(config, index_path)

    assert len(collections) == 2
    names = {c[0] for c in collections}
    assert "code-indexer-temporal-voyage_code_3" in names
    assert "code-indexer-temporal-embed_v4_0" in names

    # Verify progress data readable for each returned path
    for coll_name, coll_path in collections:
        progress = TemporalProgressiveMetadata(coll_path)
        data = progress.load_progress()
        assert "completed_commits" in data
        assert "state" in data
        assert len(data["completed_commits"]) >= 1


# ---------------------------------------------------------------------------
# test_status_temporal_reads_progress_metadata
# ---------------------------------------------------------------------------


def test_status_temporal_reads_progress_metadata(tmp_path):
    """TemporalProgressiveMetadata.load_progress() returns commit count and state fields."""
    coll_dir = tmp_path / "code-indexer-temporal-voyage_code_3"
    coll_dir.mkdir()

    meta = TemporalProgressiveMetadata(coll_dir)
    meta.mark_commit_indexed("abc123")
    meta.mark_commit_indexed("def456")
    meta.mark_commit_indexed("ghi789")
    # flush_pending() required: mark_commit_indexed stages in _pending (in-memory);
    # load_progress() reads from disk.  Mirror a real completed run's final flush.
    meta.flush_pending()
    meta.set_state("idle")

    data = meta.load_progress()

    commit_count = len(data.get("completed_commits", []))
    state = data.get("state", "idle")

    assert commit_count == 3
    assert state == "idle"


# ---------------------------------------------------------------------------
# test_status_temporal_no_collections_message
# ---------------------------------------------------------------------------


def test_status_temporal_no_collections_message(tmp_path):
    """index_path with no temporal dirs → get_temporal_collections returns empty list."""
    index_path = tmp_path / "index"
    index_path.mkdir()

    # Create a non-temporal dir that should not be included
    other_dir = index_path / "code-indexer-main"
    other_dir.mkdir()

    config = object()
    collections = get_temporal_collections(config, index_path)

    assert collections == []


# ---------------------------------------------------------------------------
# test_fusion_dispatch_import_available
# ---------------------------------------------------------------------------


def test_fusion_dispatch_import_available():
    """execute_temporal_query_with_fusion must be importable from temporal_fusion_dispatch."""
    assert callable(execute_temporal_query_with_fusion)
