"""Tests for SmartIndexer pre-flight checks."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo(path: Path) -> str:
    """Create a minimal git repo with one initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "initial.py").write_text("# initial\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    """Create a SmartIndexer wired to real git repo with mocked external services."""
    config = Config(codebase_dir=str(repo))
    mock_embedding = MagicMock()
    metadata_path = tmp_path / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding,
        vector_store_client=store,
        metadata_path=metadata_path,
    )


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """External vector store dependency — all methods return safe defaults."""
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.count_points.return_value = 0
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    store.delete_by_filter.return_value = True
    return store


@pytest.fixture
def git_repo(tmp_path: Path):
    """A real, minimally seeded git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_git_repo(repo)
    return repo


class TestHnswlibPreFlightCheck:
    """Tests for the hnswlib availability pre-flight check in smart_index()."""

    def test_smart_index_raises_runtime_error_when_hnswlib_unavailable(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """smart_index() must raise RuntimeError before acquiring lock when hnswlib is missing."""
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        with patch("code_indexer.services.smart_indexer.HNSWLIB_AVAILABLE", False):
            with pytest.raises(RuntimeError, match="hnswlib"):
                indexer.smart_index()


class TestFtsBootstrap:
    """Tests for the FTS bootstrap path in _do_incremental_index()."""

    # Offset added to current time when seeding last_index_timestamp.
    # Must be large enough that (timestamp - safety_buffer_seconds) still
    # exceeds the mtime of any file created during test setup, ensuring
    # find_modified_files() returns an empty list.
    _INDEX_TIMESTAMP_FUTURE_OFFSET_SECONDS = 120

    def _seed_completed_metadata(self, indexer: SmartIndexer, git_repo: Path) -> None:
        """Seed progressive metadata to look like a prior completed index.

        project_id for a no-remote git repo falls back to the directory name.
        The fixture creates the repo at tmp_path/'repo', so project_id='repo'.
        The commit watermark is seeded to the repo's current HEAD so incremental
        indexing finds no new commits and hits the 'nothing to do' early exit.
        """
        import time

        head_commit = subprocess.run(
            ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        current_branch = subprocess.run(
            ["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        meta = indexer.progressive_metadata.metadata
        meta["status"] = "completed"
        # Set slightly in the future so timestamp - safety_buffer is still
        # ahead of current file mtimes, making find_modified_files return [].
        meta["last_index_timestamp"] = (
            time.time() + self._INDEX_TIMESTAMP_FUTURE_OFFSET_SECONDS
        )
        meta["files_processed"] = 5
        meta["embedding_provider"] = "voyage"
        meta["embedding_model"] = "voyage-code-3"
        meta["git_available"] = True
        # No remote in test repo -> project_id = directory name = "repo"
        meta["project_id"] = "repo"
        meta["current_branch"] = current_branch
        meta["files_to_index"] = []
        meta["current_file_index"] = 0
        # Watermark tells incremental path this commit is already indexed.
        meta["branch_commit_watermarks"] = {current_branch: head_commit}
        indexer.progressive_metadata._save_metadata()

    def test_populate_fts_called_when_create_new_fts_and_nothing_to_index(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """FTS bootstrap must populate documents when FTS index is new and nothing to embed."""
        mock_vector_store.count_points.return_value = 10
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        # Embedding provider returns real strings so should_force_full_index stays False.
        indexer.embedding_provider.get_provider_name.return_value = "voyage"
        indexer.embedding_provider.get_current_model.return_value = "voyage-code-3"
        self._seed_completed_metadata(indexer, git_repo)

        # No tantivy_index/meta.json -> create_new_fts=True
        fts_index_dir = git_repo / ".code-indexer" / "tantivy_index"
        assert not (fts_index_dir / "meta.json").exists()

        mock_fts = MagicMock()

        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            return_value=mock_fts,
        ):
            indexer.smart_index(enable_fts=True)

        # Observable outcome: add_document must have been called at least once
        # (bootstrapping from disk files), proving the FTS index was populated.
        assert mock_fts.add_document.call_count >= 1

    def test_populate_fts_not_called_when_fts_index_already_exists(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """FTS bootstrap must NOT run when meta.json exists (create_new_fts=False)."""
        mock_vector_store.count_points.return_value = 10
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        indexer.embedding_provider.get_provider_name.return_value = "voyage"
        indexer.embedding_provider.get_current_model.return_value = "voyage-code-3"
        self._seed_completed_metadata(indexer, git_repo)

        # Create meta.json so the FTS index appears to already exist.
        fts_index_dir = git_repo / ".code-indexer" / "tantivy_index"
        fts_index_dir.mkdir(parents=True, exist_ok=True)
        (fts_index_dir / "meta.json").write_text("{}")

        mock_fts = MagicMock()

        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            return_value=mock_fts,
        ):
            indexer.smart_index(enable_fts=True)

        # FTS index already existed -> create_new_fts=False -> no bootstrap.
        assert mock_fts.add_document.call_count == 0

    def test_populate_fts_from_all_files_reads_files_and_adds_documents(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        """_populate_fts_from_all_files must call add_document once per file with required fields."""
        repo = tmp_path / "repo2"
        repo.mkdir()
        _create_git_repo(repo)
        (repo / "alpha.py").write_text("x = 1\n")
        (repo / "beta.py").write_text("y = 2\n")

        indexer = _make_indexer(repo, tmp_path, mock_vector_store)
        # Derive expected count from the same file_finder the method uses.
        expected_count = len(list(indexer.file_finder.find_files()))

        mock_fts = MagicMock()
        indexer._populate_fts_from_all_files(mock_fts)

        # Exact one call per discovered file, no more no less.
        assert mock_fts.add_document.call_count == expected_count

        # Both known files must appear in the reported paths.
        reported_paths = {
            call[0][0]["path"] for call in mock_fts.add_document.call_args_list
        }
        assert any("alpha.py" in p for p in reported_paths), (
            f"alpha.py missing from reported paths: {reported_paths}"
        )
        assert any("beta.py" in p for p in reported_paths), (
            f"beta.py missing from reported paths: {reported_paths}"
        )

        # Every call must carry all required fields with correct types.
        required_fields = {
            "path",
            "content",
            "content_raw",
            "identifiers",
            "line_start",
            "line_end",
            "language",
        }
        for call in mock_fts.add_document.call_args_list:
            doc = call[0][0]
            missing = required_fields - set(doc.keys())
            assert not missing, f"add_document call missing fields: {missing}"
            assert isinstance(doc["line_start"], int)
            assert isinstance(doc["line_end"], int)
            assert doc["line_start"] >= 1
            assert doc["line_end"] >= doc["line_start"]
