from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from code_indexer.config import ConfigManager
from code_indexer.services.embedding_factory import EmbeddingProviderFactory
from code_indexer.services.smart_indexer import SmartIndexer
from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _FAKE_KEY,
    _KEPT,
    _git,
    _indexed_git_repo,
)
from tests.unit.services.test_git_aware_watch_handler_retry import (
    _FlakyStore,
    _hidden,
    _hide_on,
    _index_dir,
    _reader,
)
from tests.utils.cidx_fake_provider_repo import (
    COLLECTION,
    FakeProviderRepo,
    unique_python_text,
)
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_FEATURE = "feature"


class _RecordingStore(_FlakyStore):
    """A real store that records which collections were published or aborted."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ended: List[str] = []
        self.aborted: List[str] = []

    def end_indexing(self, collection_name: str, *args: Any, **kwargs: Any) -> Any:
        self.ended.append(collection_name)
        return super().end_indexing(collection_name, *args, **kwargs)

    def abort_indexing(self, collection_name: str, *args: Any, **kwargs: Any) -> Any:
        self.aborted.append(collection_name)
        return super().abort_indexing(collection_name, *args, **kwargs)


def _branch_change_indexer(
    tmp_path: Path, server: Any, monkeypatch: pytest.MonkeyPatch, failures: int
) -> Tuple[SmartIndexer, _RecordingStore, FakeProviderRepo]:
    repo, _ = _indexed_git_repo(tmp_path, server, False)
    _hide_on(_reader(repo), f"src/{_KEPT}.py", _FEATURE)
    _git(repo.repo, "checkout", "-q", "-b", _FEATURE)
    metadata = json.loads(repo.metadata_path.read_text())
    metadata["current_branch"] = "master"
    repo.metadata_path.write_text(json.dumps(metadata))
    monkeypatch.setenv("VOYAGE_API_KEY", _FAKE_KEY)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    config = ConfigManager.load_verified_config(repo.repo)
    store = _RecordingStore(base_path=_index_dir(repo), project_root=repo.repo)
    store.failures = failures
    indexer = SmartIndexer(
        config,
        EmbeddingProviderFactory.create(config=config),
        store,
        repo.metadata_path,
    )
    return indexer, store, repo


def test_failed_branch_change_aborts_its_session_instead_of_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        indexer, store, _ = _branch_change_indexer(tmp_path, server, monkeypatch, 1)

        with pytest.raises(RuntimeError, match="graph-optimized"):
            indexer.smart_index()

        assert store.failures == 0
        assert COLLECTION in store.aborted
        assert COLLECTION not in store.ended


def test_clean_branch_change_publishes_and_unhides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        indexer, store, repo = _branch_change_indexer(tmp_path, server, monkeypatch, 0)

        indexer.smart_index()

        assert COLLECTION in store.ended
        assert COLLECTION not in store.aborted
        assert _FEATURE not in _hidden(_reader(repo), f"src/{_KEPT}.py")


def _pin_stored_branch(repo: FakeProviderRepo, branch: str) -> None:
    metadata = json.loads(repo.metadata_path.read_text())
    metadata["current_branch"] = branch
    repo.metadata_path.write_text(json.dumps(metadata))


def test_branch_change_records_failed_files_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        indexer, _, repo = _branch_change_indexer(tmp_path, server, monkeypatch, 0)
        broken = repo.repo / "src" / "kept_001.py"
        broken.write_text(unique_python_text("kept_001_on_feature", 40))
        _git(repo.repo, "commit", "-qam", "change on feature")
        _pin_stored_branch(repo, "master")
        broken.chmod(0)
        try:
            stats = indexer.smart_index()
        finally:
            broken.chmod(0o644)

        assert stats.failed_paths == {"src/kept_001.py"}
        assert stats.failed_files == 1
        recorded = json.loads(repo.metadata_path.read_text())["failed_file_paths"]
        assert [Path(p).name for p in recorded] == ["kept_001.py"]
