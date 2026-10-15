"""Pod-local golden repos stay on this pod's disk under a delegated backend.

``local://`` repositories other than cidx-meta (Langfuse trace folders) are
created and written by cidx on THIS pod; workspace-mcp never holds them. So the
golden file tools (list_files, browse_directory, get_file_content,
directory_tree) read them locally even when the content backend is delegated,
while cidx-meta and git-backed golden repos keep reading through the seam.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from code_indexer.global_repos.directory_explorer import (
    DirectoryExplorerService,
    ManifestDirectoryExplorerService,
)
from code_indexer.server.models.api_models import FileListQueryParams
from code_indexer.server.repositories import golden_repo_manager as grm_module
from code_indexer.server.services import file_service as fs
from code_indexer.server.startup import content_backend_wiring as backend_wiring


class _UnreachableBackend:
    """A delegated backend that must not be asked about pod-local repos."""

    def __init__(self, fail: Optional[Exception] = None) -> None:
        self.calls = 0
        self.fail = fail

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.fail or AssertionError(f"manifest({repo_path}) for a pod repo")

    def fetch(self, repo_path: Any, paths: Any) -> Any:
        self.calls += 1
        raise self.fail or AssertionError(f"fetch({repo_path}) for a pod repo")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Dict[str, str]:
    urls: Dict[str, str] = {
        "langfuse_proj": "local://langfuse_proj",
        "cidx-meta": "local://cidx-meta",
        "agent-router": "https://gitlab.example.com/agent-router.git",
    }

    class _Manager:
        def get_golden_repo(self, alias: str) -> Any:
            url = urls.get(alias)
            return (
                None
                if url is None
                else SimpleNamespace(
                    repo_url=url, clone_path=str(tmp_path / "golden-repos" / alias)
                )
            )

    monkeypatch.setattr(grm_module, "get_golden_repo_manager", lambda: _Manager())
    return urls


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _UnreachableBackend:
    parked = _UnreachableBackend()
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: parked)
    return parked


@pytest.fixture
def langfuse_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "golden-repos" / "langfuse_proj"
    (repo / "traces").mkdir(parents=True)
    (repo / "traces" / "t1.json").write_text('{"trace": 1}\n')
    return repo


def _params() -> FileListQueryParams:
    return FileListQueryParams(page=1, limit=100, path_pattern=None, language=None)


def test_delegated_pod_local_repo_is_listed_and_read_from_this_pod(
    registry: Dict[str, str], backend: _UnreachableBackend, langfuse_repo: Path
) -> None:
    service = fs.FileListingService()

    listing = service.list_files_by_path(str(langfuse_repo), _params())
    level = service.list_directory_level(str(langfuse_repo), "traces", _params())
    content = service.get_file_content_by_path(str(langfuse_repo), "traces/t1.json")
    tree = fs.directory_explorer_for(str(langfuse_repo))

    assert [f.path for f in listing.files] == ["traces/t1.json"]
    assert [f.path for f in level.files] == ["traces/t1.json"]
    assert content["content"].startswith('{"trace": 1}')
    assert type(tree) is DirectoryExplorerService
    assert backend.calls == 0


def test_delegated_snapshot_of_a_pod_local_repo_is_local_too(
    registry: Dict[str, str], backend: _UnreachableBackend, tmp_path: Path
) -> None:
    snapshot = tmp_path / "golden-repos" / ".versioned" / "langfuse_proj" / "v_1"

    assert fs.content_seam_source(str(snapshot)) is None


def test_delegated_cidx_meta_and_git_repos_keep_the_seam(
    registry: Dict[str, str], backend: _UnreachableBackend, tmp_path: Path
) -> None:
    golden = tmp_path / "golden-repos"
    for alias in ("cidx-meta", "agent-router", "unregistered"):
        source = fs.content_seam_source(str(golden / alias))
        assert source is not None and source.backend is backend, alias
        assert source.root == str(golden / alias)

    # A git-backed golden repo's tree comes from the backend's manifest.
    with pytest.raises(AssertionError, match="manifest"):
        fs.directory_explorer_for(str(golden / "agent-router"))


def test_delegated_seam_failure_for_a_git_repo_is_an_error(
    registry: Dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failing = _UnreachableBackend(fail=ConnectionError("workspace-mcp down"))
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: failing)
    monkeypatch.setattr(fs, "_collect_cache", type(fs._collect_cache)())

    with pytest.raises(ConnectionError):
        fs.FileListingService().list_files_by_path(
            str(tmp_path / "golden-repos" / "agent-router"), _params()
        )


def test_local_backend_is_unchanged_for_every_repo(
    registry: Dict[str, str], monkeypatch: pytest.MonkeyPatch, langfuse_repo: Path
) -> None:
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: None)

    for path in (langfuse_repo, langfuse_repo.parent / "agent-router"):
        assert fs.content_seam_source(str(path)) is None
    tree = fs.directory_explorer_for(str(langfuse_repo))
    assert not isinstance(tree, ManifestDirectoryExplorerService)
