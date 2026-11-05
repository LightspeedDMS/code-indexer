"""``content_seam_source`` always returns a source; the local one included.

Callers hold the content Protocol: the local implementation is a source like
any other, and whether its files are a directory on this pod is the
implementation's answer (``pod_directory``), never a caller's ``if delegated``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.repositories import golden_repo_manager as grm_module
from code_indexer.server.services import file_service as fs
from code_indexer.server.startup import content_backend_wiring as backend_wiring


class _Backend:
    def __init__(self, is_local: bool) -> None:
        self._is_local = is_local

    @property
    def is_local(self) -> bool:
        return self._is_local

    def manifest(self, repo_path: Any, **kwargs: Any) -> Any:
        raise AssertionError("not asked")

    def fetch(self, repo_path: Any, paths: Any) -> Any:
        raise AssertionError("not asked")


def _park(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: backend)


def test_outside_a_server_the_local_implementation_is_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _park(monkeypatch, None)

    source = fs.content_seam_source(str(tmp_path))

    assert source.backend.is_local
    assert source.root == str(tmp_path)
    assert source.version is None
    assert fs.pod_directory(source) == tmp_path


def test_a_local_server_backend_is_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parked = _Backend(is_local=True)
    _park(monkeypatch, parked)

    source = fs.content_seam_source(str(tmp_path))

    assert source.backend is parked
    assert fs.pod_directory(source) == tmp_path


def test_a_delegated_backend_is_the_source_and_has_no_pod_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parked = _Backend(is_local=False)
    _park(monkeypatch, parked)
    monkeypatch.setattr(
        backend_wiring, "is_pod_local_golden_path", lambda repo_path: False
    )

    source = fs.content_seam_source(str(tmp_path / "golden-repos" / "repo"))

    assert source.backend is parked
    assert fs.pod_directory(source) is None


def test_a_pod_local_repo_is_read_by_the_local_implementation_when_delegated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _park(monkeypatch, _Backend(is_local=False))
    repo = tmp_path / "golden-repos" / "langfuse_proj"

    class _Manager:
        def get_golden_repo(self, alias: str) -> Any:
            return SimpleNamespace(repo_url=f"local://{alias}", clone_path=str(repo))

    monkeypatch.setattr(grm_module, "get_golden_repo_manager", lambda: _Manager())

    source = fs.content_seam_source(str(repo))

    assert source.backend.is_local
    assert fs.pod_directory(source) == repo


def test_the_local_implementation_is_resolved_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _park(monkeypatch, None)

    first = fs.content_seam_source(str(tmp_path)).backend
    second = fs.content_seam_source(str(tmp_path / "other")).backend

    assert first is second


# ── the reader and the writes each service picks, once ─────────────────────


def test_the_listing_service_reads_this_pods_disk_for_a_local_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _park(monkeypatch, None)
    (tmp_path / "a.py").write_text("x = 1\n")

    files = fs.FileListingService()._files_of(str(tmp_path))

    assert isinstance(files, fs._PodDiskFiles)
    assert files.present() and files.hashes_text
    assert files.read_bytes("a.py")[0] == b"x = 1\n"
    assert not fs._PodDiskFiles(fs.FileListingService(), str(tmp_path / "no")).present()


def test_the_listing_service_reads_a_delegated_source_through_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _park(monkeypatch, _Backend(is_local=False))
    monkeypatch.setattr(
        backend_wiring, "is_pod_local_golden_path", lambda repo_path: False
    )

    files = fs.FileListingService()._files_of(str(tmp_path / "missing-here"))

    assert isinstance(files, fs._SeamFiles)
    # This pod's disk says nothing about a delegated repository.
    assert files.present() and not files.hashes_text


def test_the_crud_service_writes_through_the_target_it_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from code_indexer.server.services import file_crud_service as crud

    service = crud.FileCRUDService()
    monkeypatch.setattr(service, "_golden_write_target", lambda alias, path: None)
    assert isinstance(service._writes_for("r", tmp_path), crud._PodDiskWrites)

    target = object()
    monkeypatch.setattr(service, "_golden_write_target", lambda alias, path: target)
    writes = service._writes_for("r", tmp_path)
    assert isinstance(writes, crud._WriterWrites)

    calls: List[Tuple[Any, str, str]] = []

    def through_writer(t: Any, p: str, c: str) -> Dict[str, Any]:
        calls.append((t, p, c))
        return {"ok": True}

    monkeypatch.setattr(service, "_create_through_writer", through_writer)
    assert writes.create("f.txt", "hi") == {"ok": True}
    assert calls == [(target, "f.txt", "hi")]


def test_pod_disk_writes_are_todays_local_writes(tmp_path: Path) -> None:
    from code_indexer.server.services import file_crud_service as crud

    writes = crud._PodDiskWrites(crud.FileCRUDService(), tmp_path)

    created = writes.create("d/f.txt", "one\n")
    edited = writes.edit("d/f.txt", "one", "two", created["content_hash"], False)
    writes.delete("d/f.txt", edited["content_hash"])

    assert edited["changes_made"] == 1
    assert not (tmp_path / "d" / "f.txt").exists()
