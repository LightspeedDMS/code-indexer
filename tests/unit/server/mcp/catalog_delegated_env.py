"""The catalog file doors on a mount-less pod (``content_backend: delegated``).

``CatalogFileDoorEnv`` serves the catalog from this disk. Here the catalog
alias points at a pod-local versioned snapshot that holds ``.code-indexer``
only -- what a refresh leaves on a delegated pod -- and the files are read
through a NON-local backend over the catalog's base clone. The dependency map
the projection needs is then read from the pod's read mirror, synced through
the same backend. Local repository reads are made to fail, so a door that
skipped the seam (or the judgement) cannot pass by reading the disk.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional, Sequence, Union

import pytest

from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.cidx_meta_read_mirror import reset_mirrors
from code_indexer.server.services.file_service import FileListingService
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.startup import content_writer_wiring as writer_wiring
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)

from .catalog_file_door_env import CATALOG_ALIAS, CatalogFileDoorEnv, point_alias

SNAPSHOT_LEAF = "v_1760000000"


class DelegatedCatalogBackend(LocalContentBackend):
    """A NON-local backend over the very same tree (workspace-mcp's view)."""

    @property
    def is_local(self) -> bool:
        return False


class MappedCatalogBackend(DelegatedCatalogBackend):
    """A NON-local backend that holds *served*'s files at *content*: what a
    mount-less pod sees, its own directory holding index state only while
    workspace-mcp holds the content at the same absolute path."""

    def __init__(self, served: Path, content: Path) -> None:
        super().__init__()
        self._served = str(served)
        self._content = str(content)

    def _mapped(self, repo_path: Union[str, Path]) -> str:
        path = str(repo_path)
        if path == self._served or path.startswith(self._served + "/"):
            return self._content + path[len(self._served) :]
        return path

    def manifest(self, repo_path: Union[str, Path], **options: Any) -> Any:
        return super().manifest(self._mapped(repo_path), **options)

    def fetch(
        self, repo_path: Union[str, Path], paths: Sequence[str]
    ) -> Iterator[FetchedFile]:
        return super().fetch(self._mapped(repo_path), paths)


def _no_local_read(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a local repository read ran under delegation")


def serve_catalog_delegated(
    env: CatalogFileDoorEnv, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Serve *env*'s catalog the delegated way; returns the served snapshot."""
    snapshot = env.golden_root / ".versioned" / "cidx-meta" / SNAPSHOT_LEAF
    (snapshot / ".code-indexer").mkdir(parents=True)
    point_alias(env.golden_root, CATALOG_ALIAS, snapshot)
    base_clone = str(env.fx.root)

    def record(name: str) -> Optional[Any]:
        if name != "cidx-meta":
            return None
        return SimpleNamespace(clone_path=base_clone, repo_url="local://cidx-meta")

    backend = DelegatedCatalogBackend()
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)
    monkeypatch.setattr(wiring, "_golden_repo_record", record)
    monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: object())
    for name in (
        "_collect_files_uncached",
        "_collect_single_level",
        "_load_gitignore_spec",
        "_read_bytes_local",
    ):
        monkeypatch.setattr(FileListingService, name, _no_local_read)
    reset_mirrors()
    fs_mod._collect_cache.clear()
    fs_mod.clear_manifest_cache()
    return snapshot


def reset_delegated_caches() -> None:
    """Forget the mirrors and listings a delegated test built."""
    reset_mirrors()
    fs_mod._collect_cache.clear()
    fs_mod.clear_manifest_cache()
