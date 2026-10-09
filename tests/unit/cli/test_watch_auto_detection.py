"""Unit tests for watch mode auto-detection functionality.

Tests detect_existing_indexes() against the index layout `cidx index` really
writes (Opus P3-1 / #2057): the semantic collection is named after the
configured embedding model, the FTS index is the Tantivy index at
fts_lifecycle.fts_index_dir_for_repo(). Real directories and a real Tantivy
index; no mocks.

Story: 02_Feat_WatchModeAutoDetection/01_Story_WatchModeAutoUpdatesAllIndexes.md
"""

from pathlib import Path

from code_indexer.cli_watch_helpers import (
    detect_existing_indexes,
    semantic_collection_name,
)
from code_indexer.config import Config
from code_indexer.services.fts_lifecycle import fts_index_dir_for_repo
from code_indexer.services.tantivy_index_manager import TantivyIndexManager


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "test_project"
    (project_root / ".code-indexer" / "index").mkdir(parents=True)
    return project_root


def _semantic_index(project_root: Path, config: Config) -> None:
    (
        project_root / ".code-indexer" / "index" / semantic_collection_name(config)
    ).mkdir()


def _fts_index(project_root: Path) -> None:
    fts = TantivyIndexManager(fts_index_dir_for_repo(project_root))
    fts.initialize_index(create_new=True)
    fts.close()


def _temporal_index(project_root: Path) -> None:
    (project_root / ".code-indexer" / "index" / "code-indexer-temporal").mkdir()


class TestDetectExistingIndexes:
    """Test suite for detect_existing_indexes() function."""

    def test_detect_all_three_indexes(self, tmp_path: Path) -> None:
        project_root = _project(tmp_path)
        config = Config(codebase_dir=project_root)
        _semantic_index(project_root, config)
        _fts_index(project_root)
        _temporal_index(project_root)

        assert detect_existing_indexes(project_root, config) == {
            "semantic": True,
            "fts": True,
            "temporal": True,
        }

    def test_detect_semantic_only(self, tmp_path: Path) -> None:
        project_root = _project(tmp_path)
        config = Config(codebase_dir=project_root)
        _semantic_index(project_root, config)

        assert detect_existing_indexes(project_root, config) == {
            "semantic": True,
            "fts": False,
            "temporal": False,
        }

    def test_detect_no_indexes(self, tmp_path: Path) -> None:
        project_root = _project(tmp_path)

        assert detect_existing_indexes(
            project_root, Config(codebase_dir=project_root)
        ) == {"semantic": False, "fts": False, "temporal": False}

    def test_detect_fts_and_temporal_only(self, tmp_path: Path) -> None:
        project_root = _project(tmp_path)
        _fts_index(project_root)
        _temporal_index(project_root)

        assert detect_existing_indexes(
            project_root, Config(codebase_dir=project_root)
        ) == {"semantic": False, "fts": True, "temporal": True}

    def test_detect_with_nonexistent_project_root(self, tmp_path: Path) -> None:
        project_root = tmp_path / "nonexistent"

        assert detect_existing_indexes(
            project_root, Config(codebase_dir=project_root)
        ) == {"semantic": False, "fts": False, "temporal": False}

    def test_paths_no_indexing_run_writes_are_not_indexes(self, tmp_path: Path) -> None:
        """The paths the detection used to look for (#2057) are not where
        `cidx index` puts its semantic or FTS index."""
        project_root = _project(tmp_path)
        index_base = project_root / ".code-indexer" / "index"
        (index_base / "code-indexer-HEAD").mkdir()
        (index_base / "tantivy-fts").mkdir()
        fts_index_dir_for_repo(project_root).mkdir()  # no Tantivy index in it

        assert detect_existing_indexes(
            project_root, Config(codebase_dir=project_root)
        ) == {"semantic": False, "fts": False, "temporal": False}

    def test_semantic_collection_follows_the_configured_provider(
        self, tmp_path: Path
    ) -> None:
        project_root = _project(tmp_path)
        config = Config(codebase_dir=project_root, embedding_provider="cohere")
        (project_root / ".code-indexer" / "index" / "voyage-code-3").mkdir()

        assert not detect_existing_indexes(project_root, config)["semantic"]
        _semantic_index(project_root, config)
        assert detect_existing_indexes(project_root, config)["semantic"]

    def test_collection_name_is_made_filesystem_safe(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        config.voyage_ai.model = "org/model:v2"

        assert semantic_collection_name(config) == "org_model_v2"
