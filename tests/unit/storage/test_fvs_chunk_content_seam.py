"""A query on a pod without the repository reads blob-hash chunks through the seam.

A chunk indexed from a clean git file stores only its blob hash; the query
path reads the lines back from the file, falling back to ``git cat-file``. Both
are LOCAL: on a server whose content backend is delegated the file is not on
this pod and there is no object store, so every such result came back as
``[File deleted, cannot retrieve: ...]`` flagged deleted -- for files that are
right there in the repository. Those chunks exist wherever the index was built
with the checkout present (before the mount came off, or by a pod or CLI that
had it) and survive every incremental refresh that leaves the file unchanged.

Pinned against that shape: an empty directory here, a delegated backend serving
the repository held elsewhere, and the server's content backend set to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Sequence

import pytest

from code_indexer.server.startup import content_backend_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    LocalContentBackend,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.utils.blob_hash import git_blob_hash_bytes

SOURCE = b"line one\nline two\r\nline three\nline four\n"
REL = "src/a.py"


class _WorkspaceMcp(LocalContentBackend):
    """The repository's files, served from elsewhere (workspace-mcp's view)."""

    def __init__(self, here: Path, there: Path) -> None:
        self.here, self.there = str(here), str(there)
        self.fetched: list = []

    @property
    def is_local(self) -> bool:
        return False

    def _map(self, repo_path: Any) -> Path:
        assert str(repo_path) == self.here, "fetched from the wrong root"
        return Path(self.there)

    def manifest(self, repo_path: Any, **kw: Any) -> ContentPage:
        return super().manifest(self._map(repo_path), **kw)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        self.fetched.extend(paths)
        return super().fetch(self._map(repo_path), paths)


@pytest.fixture
def pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    held = tmp_path / "workspace-mcp" / "agent-router"
    (held / "src").mkdir(parents=True)
    (held / REL).write_bytes(SOURCE)
    here = tmp_path / "golden-repos" / "agent-router"
    (here / ".code-indexer" / "index").mkdir(parents=True)
    backend = _WorkspaceMcp(here, held)
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )
    store = FilesystemVectorStore(here / ".code-indexer" / "index", project_root=here)
    return {"held": held, "here": here, "store": store, "backend": backend}


def _chunk(blob_hash: str) -> Dict[str, Any]:
    return {
        "git_blob_hash": blob_hash,
        "payload": {"path": REL, "line_start": 2, "line_end": 3},
    }


def test_an_unchanged_file_reads_back_fresh(pod: Dict[str, Any]) -> None:
    content, staleness = pod["store"]._get_chunk_content_with_staleness(
        _chunk(git_blob_hash_bytes(SOURCE))
    )
    assert content == "line two\nline three\n"
    assert staleness["is_stale"] is False
    assert pod["backend"].fetched == [REL]


def test_a_changed_file_is_stale_not_deleted(pod: Dict[str, Any]) -> None:
    content, staleness = pod["store"]._get_chunk_content_with_staleness(
        _chunk("0" * 40)
    )
    assert staleness["staleness_reason"] == "file_modified_after_indexing"
    assert "unavailable" in content


def test_a_file_gone_from_the_repository_is_deleted(pod: Dict[str, Any]) -> None:
    (pod["held"] / REL).unlink()
    _, staleness = pod["store"]._get_chunk_content_with_staleness(
        _chunk(git_blob_hash_bytes(SOURCE))
    )
    assert staleness["staleness_reason"] == "file_deleted"


def test_a_local_backend_reads_the_disk_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: LocalContentBackend(),
    )
    (tmp_path / "src").mkdir()
    (tmp_path / REL).write_bytes(SOURCE)
    store = FilesystemVectorStore(tmp_path / "index", project_root=tmp_path)
    content, staleness = store._get_chunk_content_with_staleness(
        _chunk(git_blob_hash_bytes(SOURCE))
    )
    assert content == "line two\nline three\n"
    assert staleness["is_stale"] is False
