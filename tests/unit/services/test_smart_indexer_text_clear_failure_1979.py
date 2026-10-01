"""Bug #1979 P1 (round 5 review): a failed clear of the TEXT collection must
abort a full index, mirroring the existing discipline for a failed
multimodal-collection clear
(tests/unit/services/test_smart_indexer_multimodal_clear_failure_1979.py).

`_do_full_index()` calls `ensure_provider_aware_collection()` first, which
always creates the collection when it does not already exist (see
`FilesystemVectorStore.ensure_provider_aware_collection`) -- so by the time
`clear_collection()` runs for the text collection a few lines later, the
collection is guaranteed to exist, and a `False` return is a genuine clear
failure, never a "did not exist" signal.

Previously this Boolean result was ignored (smart_indexer.py `_do_full_index`,
around line 1212), so a failed clear left stale rows on disk while indexing
proceeded to completion and could still report success.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


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
    store.get_collection_info.return_value = {"points_count": 5}
    # No multimodal collection exists for this run -- isolates the failure
    # to the text-collection clear under test.
    store.collection_exists.return_value = False
    # Bug #1979 P1: simulate a genuine clear failure for the text collection.
    store.clear_collection.return_value = False
    return store


@pytest.fixture
def git_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_git_repo(repo)
    return repo


GIT_STATUS = {
    "git_available": True,
    "current_branch": "master",
    "current_commit": None,
}


class TestFullIndexAbortsOnFailedTextClear:
    """AC: given `clear_collection()` returns False for the text collection,
    when `_do_full_index()` runs, then it raises instead of proceeding to
    index (and reporting) as if the clear had succeeded."""

    def test_failed_text_collection_clear_aborts_full_index(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        with pytest.raises(RuntimeError, match="collection"):
            indexer._do_full_index(
                batch_size=50,
                progress_callback=None,
                git_status=GIT_STATUS,
                provider_name="voyage-ai",
                model_name="voyage-code-3",
            )

        mock_vector_store.clear_collection.assert_called_once_with("test_collection")
