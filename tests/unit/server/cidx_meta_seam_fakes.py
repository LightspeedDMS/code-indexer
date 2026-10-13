"""Fakes for cidx-meta on a pod without the golden-repos volume.

A :class:`MetaWorld` holds two golden-repos directories:

* ``local`` -- today's layout: cidx-meta's files on this disk.
* ``pod`` -- a mount-less pod: ``pod/golden-repos/cidx-meta`` holds index state
  only (``.code-indexer``). Its content lives in ``remote`` -- "workspace-mcp"
  -- served at the POD path by :class:`FakeBackend` (manifest/fetch) and
  written by :class:`FakeWriter` (put/delete with git-blob compare-and-swap).

:meth:`MetaWorld.delegated` parks the fakes as the running server's backend
and writer; :meth:`MetaWorld.forbid_local_io` makes every local file access
under the pod's cidx-meta raise, so a test proves a code path never touches
this pod's disk for cidx-meta content.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pytest

from code_indexer.server.startup import content_backend_wiring as backend_wiring
from code_indexer.server.startup import content_writer_wiring as writer_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.blob_hash import git_blob_hash_bytes


class LocalIOForbidden(AssertionError):
    """A local file access under the pod's cidx-meta content."""


class FakeBackend:
    """A NON-local content backend: the remote tree, at the pod's paths."""

    def __init__(self, pod_root: Path, remote_root: Path) -> None:
        self.pod_root = PurePosixPath(pod_root)
        self.remote_root = remote_root
        self._inner = LocalContentBackend()
        self.fail: Optional[Exception] = None
        self.manifest_calls = 0
        self.fetch_calls: List[List[str]] = []

    @property
    def is_local(self) -> bool:
        return False

    def _map(self, repo_path: Union[str, Path]) -> Path:
        rel = PurePosixPath(str(repo_path)).relative_to(self.pod_root)
        return self.remote_root / str(rel)

    def manifest(self, repo_path: Any, **kwargs: Any) -> ContentPage:
        self.manifest_calls += 1
        if self.fail is not None:
            raise self.fail
        return self._inner.manifest(self._map(repo_path), **kwargs)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.fetch_calls.append(list(paths))
        if self.fail is not None:
            raise self.fail
        return iter(list(self._inner.fetch(self._map(repo_path), paths)))


class FakeWriteError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class FakeWriter:
    """workspace-mcp's POST /content/write over the remote tree."""

    def __init__(self, backend: FakeBackend) -> None:
        self._backend = backend
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.fail: Optional[Exception] = None

    def _target(self, path: str) -> Path:
        return self._backend._map(path)

    def _check(self, target: Path, kwargs: Dict[str, Any]) -> None:
        if "expected_blob_sha" not in kwargs:
            return
        current = git_blob_hash_bytes(target.read_bytes()) if target.is_file() else None
        if current != kwargs["expected_blob_sha"]:
            raise FakeWriteError("ECONFLICT", "expected_blob_sha does not match")

    def put(self, path: str, data: bytes, **kwargs: Any) -> str:
        self.calls.append(("put", path, kwargs))
        if self.fail is not None:
            raise self.fail
        target = self._target(path)
        self._check(target, kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return git_blob_hash_bytes(data)

    def delete(self, path: str, **kwargs: Any) -> bool:
        self.calls.append(("delete", path, kwargs))
        if self.fail is not None:
            raise self.fail
        target = self._target(path)
        self._check(target, kwargs)
        if not target.exists():
            return False
        target.unlink()
        return True


def tree(root: Path) -> Dict[str, bytes]:
    """Every file under *root* (relative path -> bytes), for comparisons."""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class MetaWorld:
    """A local golden-repos dir, a mount-less pod's, and the remote content."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.local_golden = tmp_path / "local" / "golden-repos"
        self.local_meta = self.local_golden / "cidx-meta"
        self.pod_golden = tmp_path / "pod" / "golden-repos"
        self.pod_meta = self.pod_golden / "cidx-meta"
        self.remote_meta = tmp_path / "workspace-mcp" / "cidx-meta"
        for meta in (self.local_meta, self.remote_meta):
            meta.mkdir(parents=True)
        # The pod's own cidx-meta: index state only.
        (self.pod_meta / ".code-indexer").mkdir(parents=True)
        (self.pod_meta / ".code-indexer" / "config.json").write_text("{}")
        self.backend = FakeBackend(self.pod_meta, self.remote_meta)
        self.writer = FakeWriter(self.backend)
        self.local_mode()

    # ── modes ───────────────────────────────────────────────────────────

    def local_mode(self) -> None:
        """No backend parked: the local default."""
        self._monkeypatch.setattr(
            backend_wiring, "server_content_backend", lambda: None
        )
        self._monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: None)

    def delegated(self, *, writer: bool = True) -> None:
        """Park the fakes as the running server's backend (and writer)."""
        self._monkeypatch.setattr(
            backend_wiring, "server_content_backend", lambda: self.backend
        )
        parked = self.writer if writer else None
        self._monkeypatch.setattr(
            writer_wiring, "server_content_writer", lambda: parked
        )

    # ── seeding ─────────────────────────────────────────────────────────

    def seed(self, rel: str, data: Union[str, bytes]) -> None:
        """Put the same file in local cidx-meta AND the remote content."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        for meta in (self.local_meta, self.remote_meta):
            target = meta / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

    def local_tree(self) -> Dict[str, bytes]:
        return tree(self.local_meta)

    def remote_tree(self) -> Dict[str, bytes]:
        return tree(self.remote_meta)

    def pod_tree(self) -> Dict[str, bytes]:
        return tree(self.pod_meta)

    # ── the proof ───────────────────────────────────────────────────────

    @contextlib.contextmanager
    def forbid_local_io(self) -> Iterator[None]:
        """Inside the block every local read, write, listing or stat under the
        pod's cidx-meta raises LocalIOForbidden (its .code-indexer index
        state included: nothing here may look at it either)."""
        with pytest.MonkeyPatch.context() as patcher:
            self._install_guards(patcher)
            yield

    def _install_guards(self, patcher: pytest.MonkeyPatch) -> None:
        roots = {os.path.realpath(self.pod_meta), os.path.abspath(self.pod_meta)}

        def _under(path: Any) -> bool:
            try:
                text = os.fspath(path)
            except TypeError:
                return False
            if isinstance(text, bytes):
                text = text.decode(errors="replace")
            full = os.path.abspath(text)
            return any(full == r or full.startswith(r + os.sep) for r in roots)

        def _guard(owner: Any, name: str, path_arg: int = 0) -> None:
            original = getattr(owner, name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                candidates = list(args[: path_arg + 1]) + [
                    kwargs.get("dir"),
                    kwargs.get("path"),
                    kwargs.get("file"),
                ]
                if any(c is not None and _under(c) for c in candidates):
                    raise LocalIOForbidden(f"{name}{args!r} touched {self.pod_meta}")
                return original(*args, **kwargs)

            patcher.setattr(owner, name, wrapper)

        for name in (
            "read_text",
            "read_bytes",
            "write_text",
            "write_bytes",
            "open",
            "unlink",
            "rename",
            "replace",
            "exists",
            "is_file",
            "is_dir",
            "glob",
            "rglob",
            "iterdir",
            "mkdir",
            "stat",
            "touch",
        ):
            _guard(Path, name)
        _guard(builtins, "open")
        for name in (
            "replace",
            "rename",
            "unlink",
            "remove",
            "walk",
            "scandir",
            "listdir",
            "stat",
            "makedirs",
        ):
            _guard(os, name, path_arg=1)
        _guard(tempfile, "mkstemp")
        _guard(tempfile, "NamedTemporaryFile")
        _guard(shutil, "rmtree")
        _guard(shutil, "copy2", path_arg=1)
        _guard(shutil, "move", path_arg=1)
