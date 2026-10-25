from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

_BULK = 12_000
_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]


class _PagingStore:
    """In-memory content points served page by page through numeric offsets."""

    def __init__(self, points: List[Dict[str, Any]], update_result: Any = True):
        self.points = points
        self.update_result = update_result
        self.updates: List[List[Dict[str, Any]]] = []
        self.cycle = False

    def scroll_points(
        self,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        offset: Optional[str] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        self_heal: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        start = int(offset or 0)
        if self.cycle:
            return self.points[:1], str(1 - start)
        page = self.points[start : start + limit]
        end = start + limit
        return page, (str(end) if end < len(self.points) else None)

    def _batch_update_payload_only(
        self, updates: List[Dict[str, Any]], collection_name: str
    ) -> Any:
        self.updates.append(updates)
        if isinstance(self.update_result, Exception):
            raise self.update_result
        return self.update_result


def _point(point_id: str, path: str, commit: str) -> Dict[str, Any]:
    return {
        "id": point_id,
        "payload": {
            "type": "content",
            "path": path,
            "git_commit_hash": commit,
            "hidden_branches": [],
        },
    }


def _points_with_late_duplicate() -> List[Dict[str, Any]]:
    bulk = [_point(f"p{i}", f"src/f{i}.py", "c" * 40) for i in range(_BULK)]
    return bulk + [
        _point("dup-committed", "src/dup.py", "d" * 40),
        _point("dup-working", "src/dup.py", "working_dir_example"),
    ]


def _indexer(tmp_path: Path, store: _PagingStore) -> SmartIndexer:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([*_GIT, "init", "-q", "-b", "master"], cwd=repo, check=True)
    return SmartIndexer(
        config=Config(codebase_dir=repo),
        embedding_provider=MagicMock(),
        vector_store_client=store,
        metadata_path=tmp_path / "metadata.json",
    )


def test_duplicate_beyond_ten_thousand_points_is_hidden(tmp_path: Path) -> None:
    store = _PagingStore(_points_with_late_duplicate())

    _indexer(tmp_path, store)._cleanup_multiple_visible_content_points("coll", "master")

    assert store.updates == [
        [{"id": "dup-working", "payload": {"hidden_branches": ["master"]}}]
    ]


@pytest.mark.parametrize(
    "result", [False, OSError("payload write failed")], ids=["false", "raises"]
)
def test_failed_visibility_update_fails_the_cleanup(
    tmp_path: Path, result: Any
) -> None:
    store = _PagingStore(_points_with_late_duplicate(), update_result=result)

    with pytest.raises((RuntimeError, OSError)):
        _indexer(tmp_path, store)._cleanup_multiple_visible_content_points(
            "coll", "master"
        )


class _SessionStore(_PagingStore):
    """The paging store plus the session calls branch processing makes."""

    def __init__(self, points: List[Dict[str, Any]], update_result: Any = True):
        super().__init__(points, update_result)
        self.session_calls: List[str] = []

    def begin_indexing(self, collection_name: str) -> None:
        self.session_calls.append("begin")

    def end_indexing(self, collection_name: str, *args: Any, **kwargs: Any) -> Any:
        self.session_calls.append("end")
        return {}

    def abort_indexing(self, collection_name: str) -> None:
        self.session_calls.append("abort")

    def collection_exists(self, collection_name: str) -> bool:
        return False

    def set_hnsw_branch_context(self, *args: Any, **kwargs: Any) -> None:
        self.session_calls.append("context")


def _points_hidden_on_master() -> List[Dict[str, Any]]:
    point = _point("p0", "src/f0.py", "c" * 40)
    point["payload"]["hidden_branches"] = ["master"]
    return [point, _point("p1", "src/f1.py", "c" * 40)]


def test_batch_unhide_failure_raises(tmp_path: Path) -> None:
    store = _SessionStore(_points_hidden_on_master(), update_result=False)

    with pytest.raises(RuntimeError):
        _indexer(tmp_path, store)._batch_ensure_files_visible_in_branch(
            ["src/f0.py"], "master", "coll", store.points
        )


def test_batch_hide_failure_raises(tmp_path: Path) -> None:
    store = _SessionStore(_points_hidden_on_master(), update_result=False)

    with pytest.raises(RuntimeError):
        _indexer(tmp_path, store)._batch_hide_files_in_branch(
            ["src/f1.py"], "master", "coll", store.points
        )


def test_branch_change_unhide_failure_propagates(tmp_path: Path) -> None:
    store = _SessionStore(_points_hidden_on_master(), update_result=False)

    with pytest.raises(RuntimeError):
        _indexer(tmp_path, store).process_branch_changes_high_throughput(
            old_branch="feature",
            new_branch="master",
            changed_files=[],
            unchanged_files=["src/f0.py"],
            collection_name="coll",
            skip_branch_isolation=True,
            defer_finalization=True,
        )


class _UnlistableStore(_SessionStore):
    error: Exception = ValueError("path listing failed")

    def distinct_content_paths(self, collection_name: str) -> Any:
        raise self.error


@pytest.mark.parametrize(
    "error, raised",
    [
        (ValueError("path listing failed"), RuntimeError),
        (OSError("path listing failed"), ChunkStoreUnavailableError),
    ],
    ids=["non-fatal", "fatal-typed"],
)
def test_branch_isolation_discovery_failure_raises(
    tmp_path: Path, error: Exception, raised: type
) -> None:
    store = _UnlistableStore(_points_hidden_on_master())
    store.error = error

    with pytest.raises(raised):
        _indexer(tmp_path, store).hide_files_not_in_branch_thread_safe(
            "master", ["src/f1.py"], "coll"
        )


@pytest.mark.timeout(20)
def test_paging_without_new_points_stops(tmp_path: Path) -> None:
    store = _PagingStore(_points_with_late_duplicate())
    store.cycle = True

    with pytest.raises(RuntimeError, match="no new points"):
        _indexer(tmp_path, store)._scroll_all_content_points("coll")
