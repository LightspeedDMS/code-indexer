"""Tests for Bug #642 Step 3: _save_temporal_metadata persists max_commits/since_date.

TDD: Tests written BEFORE implementation to drive the design.

Covers:
- test_save_temporal_metadata_persists_max_commits
- test_save_temporal_metadata_persists_since_date
- test_save_temporal_metadata_persists_both
- test_save_temporal_metadata_no_max_commits_not_in_json
- test_save_temporal_metadata_no_since_date_not_in_json
"""

import json
from pathlib import Path
from unittest.mock import MagicMock


from code_indexer.services.temporal.temporal_indexer import TemporalIndexer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config_manager(tmp_path: Path) -> MagicMock:
    """Build a minimal config manager mock for TemporalIndexer construction."""
    from code_indexer.config import TemporalConfig

    config = MagicMock()
    config.codebase_dir = tmp_path
    config.embedding_provider = "voyage-ai"
    config.file_extensions = None
    temporal_cfg = TemporalConfig()
    config.temporal = temporal_cfg
    config.override_config = None

    # voyage_ai sub-config
    voyage_ai = MagicMock()
    voyage_ai.model = "voyage-code-3"
    voyage_ai.parallel_requests = 4
    config.voyage_ai = voyage_ai

    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = tmp_path / ".code-indexer" / "config.json"
    return config_manager


def _make_vector_store(tmp_path: Path) -> MagicMock:
    """Build a minimal vector store mock."""
    index_dir = tmp_path / ".code-indexer" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = MagicMock()
    vector_store.project_root = tmp_path
    vector_store.base_path = index_dir
    return vector_store


def _make_indexer(tmp_path: Path) -> TemporalIndexer:
    """Build a TemporalIndexer with mocked dependencies."""
    config_manager = _make_config_manager(tmp_path)
    vector_store = _make_vector_store(tmp_path)
    indexer = TemporalIndexer(
        config_manager,
        vector_store,
        collection_name="code-indexer-temporal-voyage_code_3",
    )
    return indexer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveTemporalMetadataPersistenceMaxCommits:
    """Bug #642 Step 3: max_commits must be persisted in temporal_meta.json."""

    def test_save_temporal_metadata_persists_max_commits(self, tmp_path):
        """max_commits passed to _save_temporal_metadata appears in persisted JSON."""
        indexer = _make_indexer(tmp_path)

        indexer._save_temporal_metadata(
            last_commit="abc123",
            total_commits=10,
            files_processed=5,
            approximate_vectors_created=50,
            branch_stats={"branches": ["main"], "per_branch_counts": {}},
            indexing_mode="single-branch",
            max_commits=5,
        )

        meta_path = indexer.temporal_dir / "temporal_meta.json"
        assert meta_path.exists(), "temporal_meta.json must be written"
        meta = json.loads(meta_path.read_text())
        assert "max_commits" in meta, (
            "max_commits must be persisted in temporal_meta.json"
        )
        assert meta["max_commits"] == 5

    def test_save_temporal_metadata_persists_since_date(self, tmp_path):
        """since_date passed to _save_temporal_metadata appears in persisted JSON."""
        indexer = _make_indexer(tmp_path)

        indexer._save_temporal_metadata(
            last_commit="def456",
            total_commits=20,
            files_processed=8,
            approximate_vectors_created=80,
            branch_stats={"branches": ["main"], "per_branch_counts": {}},
            indexing_mode="single-branch",
            since_date="2024-01-01",
        )

        meta_path = indexer.temporal_dir / "temporal_meta.json"
        meta = json.loads(meta_path.read_text())
        assert "since_date" in meta, (
            "since_date must be persisted in temporal_meta.json"
        )
        assert meta["since_date"] == "2024-01-01"

    def test_save_temporal_metadata_persists_both(self, tmp_path):
        """Both max_commits and since_date are persisted when both are provided."""
        indexer = _make_indexer(tmp_path)

        indexer._save_temporal_metadata(
            last_commit="ghi789",
            total_commits=15,
            files_processed=6,
            approximate_vectors_created=60,
            branch_stats={"branches": ["main"], "per_branch_counts": {}},
            indexing_mode="single-branch",
            max_commits=100,
            since_date="2023-06-01",
        )

        meta_path = indexer.temporal_dir / "temporal_meta.json"
        meta = json.loads(meta_path.read_text())
        assert meta["max_commits"] == 100
        assert meta["since_date"] == "2023-06-01"

    def test_save_temporal_metadata_no_max_commits_not_in_json(self, tmp_path):
        """When max_commits is None (default), it is not written to the JSON."""
        indexer = _make_indexer(tmp_path)

        indexer._save_temporal_metadata(
            last_commit="jkl012",
            total_commits=3,
            files_processed=1,
            approximate_vectors_created=10,
            branch_stats={"branches": ["main"], "per_branch_counts": {}},
            indexing_mode="single-branch",
        )

        meta_path = indexer.temporal_dir / "temporal_meta.json"
        meta = json.loads(meta_path.read_text())
        assert "max_commits" not in meta, (
            "max_commits must NOT appear in JSON when not provided"
        )

    def test_save_temporal_metadata_no_since_date_not_in_json(self, tmp_path):
        """When since_date is None (default), it is not written to the JSON."""
        indexer = _make_indexer(tmp_path)

        indexer._save_temporal_metadata(
            last_commit="mno345",
            total_commits=3,
            files_processed=1,
            approximate_vectors_created=10,
            branch_stats={"branches": ["main"], "per_branch_counts": {}},
            indexing_mode="single-branch",
        )

        meta_path = indexer.temporal_dir / "temporal_meta.json"
        meta = json.loads(meta_path.read_text())
        assert "since_date" not in meta, (
            "since_date must NOT appear in JSON when not provided"
        )
