from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer

# ^ see tests/unit/services/test_indexing_session_and_watermark_integrity.py; the real-CLI proof of the abort rule
# ^ why process_files_high_throughput is patched; the error must arrive after begin_indexing, as a lease-file race does


def _create_git_repo(path: Path) -> str:
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
    config = Config(codebase_dir=repo)
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
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.count_points.return_value = 0
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    store.delete_by_filter.return_value = True
    store.get_collection_info.return_value = {"points_count": 0}
    store.clear_collection.return_value = None
    return store


@pytest.fixture
def git_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_git_repo(repo)
    return repo


GIT_STATUS = {"git_available": True, "current_branch": "master", "current_commit": None}


class TestFileNotFoundErrorDuringGitIncrementalAbortsKeepingThePublishOwed:
    def _seed_for_incremental(self, indexer: SmartIndexer) -> None:
        meta = indexer.progressive_metadata.metadata
        meta["status"] = "completed"
        meta["last_index_timestamp"] = 1.0  # epoch -- far in the past
        meta["embedding_provider"] = "voyage-ai"
        meta["embedding_model"] = "voyage-code-3"
        meta["files_to_index"] = []
        meta["current_file_index"] = 0
        indexer.progressive_metadata._save_metadata()

    # ^ why abort, not end_indexing; committed rows stay in chunks.db and the owed publish => next run republishes them filtered
    def test_ordinary_file_not_found_error_aborts_not_finalizes(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        self._seed_for_incremental(indexer)

        lease_race_error = FileNotFoundError(
            2,
            "No such file or directory",
            str(
                git_repo
                / ".."
                / "cidx-meta"
                / ".snapshot-reader-leases"
                / "deadbeef-hostname-123.json"
            ),
        )

        with patch.object(
            indexer,
            "process_files_high_throughput",
            side_effect=lease_race_error,
        ):
            with pytest.raises(
                RuntimeError, match="Git-aware incremental indexing failed"
            ) as exc_info:
                indexer._do_incremental_index(
                    batch_size=50,
                    progress_callback=None,
                    git_status=GIT_STATUS,
                    provider_name="voyage-ai",
                    model_name="voyage-code-3",
                    safety_buffer_seconds=0,
                )

        assert exc_info.value.__cause__ is lease_race_error
        mock_vector_store.abort_indexing.assert_any_call("test_collection")
        mock_vector_store.end_indexing.assert_not_called()
