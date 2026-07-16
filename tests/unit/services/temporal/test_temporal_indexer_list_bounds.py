"""Test for list index out of range bug in temporal indexing.

Bug reproduction: At 365/366 commits, temporal indexing fails with
'list index out of range' error during metadata save operation.

Root cause analysis:
- Progressive metadata filtering removes already-completed commits
- If ALL commits are filtered out, commits list becomes empty
- commits[-1].hash access at line 202 fails with IndexError
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.services.temporal.models import CommitInfo


@pytest.fixture
def mock_config_manager():
    """Create mock config manager."""
    config_manager = Mock()
    config = Mock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai = Mock()
    config.voyage_ai.parallel_requests = 4
    config.voyage_ai.temporal_parallel_requests = None
    config.voyage_ai.max_concurrent_batches_per_commit = 10
    config_manager.get_config.return_value = config
    return config_manager


@pytest.fixture
def mock_vector_store():
    """Create mock vector store."""
    vector_store = Mock()
    temp_dir = Path(tempfile.mkdtemp())
    vector_store.project_root = temp_dir
    vector_store.base_path = temp_dir / ".code-indexer" / "index"
    vector_store.collection_exists.return_value = True
    vector_store.load_id_index.return_value = set()
    return vector_store


@pytest.fixture
def temporal_indexer(mock_config_manager, mock_vector_store):
    """Create temporal indexer instance."""
    with patch("code_indexer.services.embedding_factory.EmbeddingProviderFactory"):
        indexer = TemporalIndexer(mock_config_manager, mock_vector_store)
        return indexer


def test_empty_commits_after_filtering_should_return_early(temporal_indexer):
    """Test that filtering ALL commits returns early gracefully without error.

    Bug scenario that should be fixed:
    1. Get 366 commits from git history
    2. Every configured embedder's reconcile-based discovery finds nothing missing
    3. Every embedder is skipped (zero work scheduled)
    4. Code should return early with zero results
    5. Currently crashes with IndexError at line 202: commits[-1].hash

    Story #1291: missing-commit discovery is now ALWAYS done via
    reconcile_temporal_index() (disk-scan-based, per embedder) rather than
    the old single-collection progressive_metadata filter, so "all commits
    already indexed" is simulated by patching reconcile_temporal_index to
    report nothing missing, with a real registered fake embedder standing in
    for the (unconfigured Mock) active_embedder.
    """
    from code_indexer.services.temporal.embedders.base import TemporalEmbedder
    from code_indexer.services.temporal.embedders.registry import (
        register_embedder,
        unregister_embedder_for_tests,
    )

    fake_embedder_name = "fake-list-bounds-1291"

    class _FakeEmbedder(TemporalEmbedder):
        name = fake_embedder_name
        model_slug = "fake_list_bounds_1291"
        dimensions = 4
        overlap_percentage = 0.0

        def __init__(self, config=None):
            pass

        def embed_commit_chunks(self, chunks):
            return [[0.0] * self.dimensions for _ in chunks]

        def embed_query(self, text):
            return [0.0] * self.dimensions

    register_embedder(fake_embedder_name, lambda config: _FakeEmbedder(config))
    temporal_indexer.config.temporal = Mock()
    temporal_indexer.config.temporal.active_embedder = fake_embedder_name
    temporal_indexer.config.temporal.embedders = [fake_embedder_name]

    # Setup: Create 3 commits
    commits = [
        CommitInfo(
            hash=f"commit{i}",
            timestamp=1234567890 + i,
            author_name="Test Author",
            author_email="test@example.com",
            message=f"Commit {i}",
            parent_hashes="",
        )
        for i in range(3)
    ]

    from code_indexer.services.temporal.temporal_incremental_gate import (
        EmbedderIndexingPlan,
    )

    try:
        # Mock git operations, embedding provider, and the gate's
        # missing-commit discovery (all commits already indexed -> empty
        # plan for the fake embedder). Bug #1407: the automatic path now
        # calls compute_embedder_indexing_plan() instead of the disk-scan
        # reconcile_temporal_index().
        with patch.object(
            temporal_indexer, "_get_commit_history", return_value=commits
        ):
            with patch.object(
                temporal_indexer, "_get_current_branch", return_value="main"
            ):
                with patch(
                    "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
                ):
                    with patch(
                        "code_indexer.services.temporal.temporal_incremental_gate.compute_embedder_indexing_plan",
                        return_value=EmbedderIndexingPlan(shard_commits={}),
                    ):
                        # Expected behavior: Should return early with zero results
                        # Current bug: Crashes with IndexError at line 202
                        result = temporal_indexer.index_commits(all_branches=False)

                        # Verify correct early return behavior with new field names
                        assert result.total_commits == 0
                        assert result.files_processed == 0
                        assert result.approximate_vectors_created == 0
                        assert result.skip_ratio == 1.0  # All commits skipped
                        assert result.branches_indexed == []
                        assert result.commits_per_branch == {}
    finally:
        unregister_embedder_for_tests(fake_embedder_name)
