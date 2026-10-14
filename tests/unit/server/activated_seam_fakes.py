"""Fakes for activated repositories on a pod without the repository volume.

An :class:`ActivatedWorld` builds ONE upstream git repository and two
deployments of cidx over it:

* ``local`` -- today's layout: the golden clone (with its ``.code-indexer``)
  and every activated copy are directories on this disk.
* ``pod`` + ``remote`` -- a mount-less pod. The pod's golden-repos dir holds
  the golden repo's INDEX STATE only; its files and every activated working
  copy live in ``remote`` ("workspace-mcp's storage"), reached at the POD's
  absolute paths through :class:`FakeWorkspaceMcp` (the clone-path endpoints
  behind a real :class:`DelegatedCloneBackend`) and :class:`FakeGitMcp` (git
  by ``cow_path``).

:meth:`ActivatedWorld.forbid_local_io` makes every local file access, and
every subprocess whose cwd or argv names a pod repository path, raise -- except
under a ``.code-indexer`` directory, which IS this pod's state.
"""

from __future__ import annotations

import builtins
import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.startup import content_backend_wiring
from code_indexer.server.storage.shared.clone_backend import (
    DelegatedCloneBackend,
    LocalCloneBackend,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from code_indexer.server.utils.config_manager import ServerResourceConfig

ALIAS = "example-repo"
USER = "alice"
INDEX_DIR = ".code-indexer"


class LocalIOForbidden(AssertionError):
    """A local access to repository content on the mount-less pod."""


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Example",
        GIT_AUTHOR_EMAIL="dev@example.com",
        GIT_COMMITTER_NAME="Example",
        GIT_COMMITTER_EMAIL="dev@example.com",
        GIT_AUTHOR_DATE="2024-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2024-01-01T00:00:00Z",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def tree(root: Path, skip: Sequence[str] = (".git", INDEX_DIR)) -> Dict[str, bytes]:
    """Every file under *root* outside *skip* dirs (relative path -> bytes)."""
    if not root.exists():
        return {}
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in skip:
            continue
        if p.is_file():
            out[str(rel)] = p.read_bytes()
    return out


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        return self._body


class FakeWorkspaceMcp(DelegatedCloneBackend):
    """A real DelegatedCloneBackend whose HTTP POSTs land on the remote tree."""

    def __init__(self, world: "ActivatedWorld") -> None:
        super().__init__(
            config=SimpleNamespace(
                endpoint="http://workspace-mcp.example", api_key="k", timeout_seconds=30
            ),
            versioned_base=str(world.pod_data / "golden-repos"),
            visibility_waiter=self._no_visibility_wait,
        )
        self._world = world
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.fail: Dict[str, int] = {}

    @staticmethod
    def _no_visibility_wait(path: str) -> None:
        raise AssertionError(f"mount-less activation waited for {path} on the pod")

    def _post(self, route: str, body: dict, timeout: Optional[int], what: str) -> Any:
        self.calls.append((route, dict(body)))
        if route in self.fail:
            return _Response(self.fail[route], {"message": "injected failure"})
        if route == "/clone-path":
            src = self._world.remote_of(body["source_path"])
            dest = self._world.remote_of(body["dest_path"])
            if body.get("if_absent") and os.path.lexists(dest):
                return _Response(409, {"message": "dest_path already exists"})
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, symlinks=True)
            return _Response(200, {"dest_path": body["dest_path"], "method": "fs-copy"})
        if route == "/clone-path/stat":
            target = self._world.remote_of(body["path"])
            exists = os.path.lexists(target)
            return _Response(
                200,
                {
                    "path": body["path"],
                    "exists": exists,
                    "is_dir": exists and target.is_dir() and not target.is_symlink(),
                },
            )
        if route == "/clone-path/delete":
            target = self._world.remote_of(body["path"])
            if not os.path.lexists(target):
                return _Response(200, {"path": body["path"], "deleted": False})
            shutil.rmtree(target)
            return _Response(200, {"path": body["path"], "deleted": True})
        return _Response(404, {"message": f"no route {route}"})

    def routes(self) -> List[str]:
        return [route for route, _ in self.calls]


class FakeGitMcp:
    """git-mcp: every git op runs on the remote tree, addressed by POD path.

    Any string argument naming a pod path is mapped onto the remote tree, so
    remote URLs recorded in a copy's git config are remote paths -- exactly
    what git-mcp, which sees the shared volume, records.
    """

    owns_credentials = True

    def __init__(self, world: "ActivatedWorld") -> None:
        self._world = world
        self._inner = LocalGitBackend()
        self.calls: List[Tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            mapped = [self._world.map_arg(a) for a in args]
            mapped_kwargs = {k: self._world.map_arg(v) for k, v in kwargs.items()}
            self.calls.append((name, str(args[0]) if args else ""))
            return method(*mapped, **mapped_kwargs)

        return call


class _NonLocalContentBackend:
    is_local = False


class ActivatedWorld:
    """An upstream repo, a local cidx deployment and a mount-less one."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.tmp = tmp_path
        self.upstream = tmp_path / "upstream.git"
        self._make_upstream(tmp_path / "seed")

        # local deployment: golden clone + its index on this disk
        self.local_data = tmp_path / "local" / "data"
        self.local_golden = self.local_data / "golden-repos" / ALIAS
        self._clone_golden(self.local_golden)
        self._write_index(self.local_golden / INDEX_DIR)

        # mount-less deployment: pod holds the index, remote holds the files
        self.pod_root = tmp_path / "pod"
        self.remote_root = tmp_path / "remote"
        self.pod_data = self.pod_root / "data"
        self.pod_golden = self.pod_data / "golden-repos" / ALIAS
        self._clone_golden(self.remote_of(self.pod_golden))
        self._write_index(self.pod_golden / INDEX_DIR)

        self.workspace_mcp = FakeWorkspaceMcp(self)
        self.git_mcp = FakeGitMcp(self)
        self.fix_config_calls: List[str] = []

    # -- fixture repositories ----------------------------------------------

    def _make_upstream(self, seed: Path) -> None:
        seed.mkdir()
        _git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("example\n")
        (seed / "src").mkdir()
        (seed / "src" / "app.py").write_text("print('main')\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", "initial")
        _git(seed, "checkout", "-q", "-b", "feature")
        (seed / "src" / "feature.py").write_text("print('feature')\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", "feature work")
        _git(seed, "checkout", "-q", "main")
        _git(self.tmp, "clone", "-q", "--bare", str(seed), str(self.upstream))

    def _clone_golden(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(self.tmp, "clone", "-q", str(self.upstream), str(dest))

    @staticmethod
    def _write_index(index: Path) -> None:
        (index / "index" / "collection").mkdir(parents=True)
        (index / "config.json").write_text('{"embedding_provider": "voyage-ai"}')
        (index / "index" / "collection" / "chunks.db").write_bytes(b"\x00chunks\x01")

    # -- path mapping (pod path <-> workspace-mcp storage) -------------------

    def remote_of(self, path: Any) -> Path:
        rel = Path(os.fspath(path)).relative_to(self.pod_root)
        return self.remote_root / rel

    def map_arg(self, value: Any) -> Any:
        if isinstance(value, (str, Path)):
            text = os.fspath(value)
            root = str(self.pod_root)
            if text == root or text.startswith(root + os.sep):
                mapped = self.remote_of(text)
                return str(mapped) if isinstance(value, str) else mapped
        if isinstance(value, list):
            return [self.map_arg(v) for v in value]
        return value

    # -- managers -----------------------------------------------------------

    def _golden_manager(self, golden_path: Path) -> MagicMock:
        golden = GoldenRepo(
            alias=ALIAS,
            repo_url=str(self.upstream),
            default_branch="main",
            clone_path=str(golden_path),
            created_at="2024-01-01T00:00:00Z",
        )
        manager = MagicMock()
        manager.get_golden_repo.return_value = golden
        manager.get_actual_repo_path.return_value = str(golden_path)
        manager.resource_config = ServerResourceConfig()
        manager._refresh_scheduler = None
        return manager

    def _patch_side_effects(self, manager: ActivatedRepoManager) -> None:
        self._monkeypatch.setattr(
            manager,
            "_fix_cloned_cidx_config",
            lambda dest: self.fix_config_calls.append(str(dest)),
        )
        from code_indexer.server.repositories import activated_repo_manager as mod

        resolver = MagicMock()
        resolver.return_value.resolve_committer_email.return_value = (
            "dev@example.com",
            None,
        )
        self._monkeypatch.setattr(mod, "CommitterResolutionService", resolver)

    def local_manager(self) -> ActivatedRepoManager:
        self._monkeypatch.setattr(
            content_backend_wiring, "server_content_backend", lambda: None
        )
        manager = ActivatedRepoManager(
            data_dir=str(self.local_data),
            golden_repo_manager=self._golden_manager(self.local_golden),
            background_job_manager=MagicMock(),
            clone_backend=LocalCloneBackend(),
            git_backend=LocalGitBackend(),
        )
        self._patch_side_effects(manager)
        return manager

    def delegated_manager(
        self, *, clone_backend: Any = None, git_backend: Any = None
    ) -> ActivatedRepoManager:
        self.delegated()
        manager = ActivatedRepoManager(
            data_dir=str(self.pod_data),
            golden_repo_manager=self._golden_manager(self.pod_golden),
            background_job_manager=MagicMock(),
            clone_backend=clone_backend or self.workspace_mcp,
            git_backend=git_backend or self.git_mcp,
        )
        self._patch_side_effects(manager)
        return manager

    def delegated(self) -> None:
        self._monkeypatch.setattr(
            content_backend_wiring,
            "server_content_backend",
            lambda: _NonLocalContentBackend(),
        )

    def pod_copy(self, user_alias: str = ALIAS) -> Path:
        return self.pod_data / "activated-repos" / USER / user_alias

    def remote_copy(self, user_alias: str = ALIAS) -> Path:
        return self.remote_of(self.pod_copy(user_alias))

    def local_copy(self, user_alias: str = ALIAS) -> Path:
        return self.local_data / "activated-repos" / USER / user_alias

    # -- the proof ----------------------------------------------------------

    @contextlib.contextmanager
    def forbid_local_io(self) -> Iterator[None]:
        """Inside the block, any local access to a pod REPOSITORY path (the
        golden clone or an activated copy) outside its ``.code-indexer``
        raises, as does any subprocess run in or naming one."""
        with pytest.MonkeyPatch.context() as patcher:
            self._install_guards(patcher)
            yield

    def _forbidden(self, value: Any) -> bool:
        try:
            text = os.fspath(value)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        full = os.path.abspath(text)
        roots = [str(self.pod_golden), str(self.pod_data / "activated-repos" / USER)]
        for root in roots:
            if not (full == root or full.startswith(root + os.sep)):
                continue
            rel = Path(full).relative_to(root).parts
            if root == str(self.pod_golden):
                return not (rel and rel[0] == INDEX_DIR)
            # activated-repos/<user>: <alias>/.code-indexer is pod state, and so
            # is solo mode's <alias>_metadata.json registry beside the copies.
            if len(rel) <= 1:
                return bool(rel) and not rel[0].endswith("_metadata.json")
            return rel[1] != INDEX_DIR
        return False

    def _install_guards(self, patcher: pytest.MonkeyPatch) -> None:
        forbidden = self._forbidden

        def _guard(owner: Any, name: str, path_arg: int = 0) -> None:
            original = getattr(owner, name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                candidates = list(args[: path_arg + 1]) + [
                    kwargs.get("path"),
                    kwargs.get("file"),
                ]
                if any(c is not None and forbidden(c) for c in candidates):
                    raise LocalIOForbidden(f"{name}{args!r}")
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
            "exists",
            "is_file",
            "is_dir",
            "iterdir",
            "glob",
            "rglob",
            "stat",
        ):
            _guard(Path, name)
        _guard(builtins, "open")
        for name in ("listdir", "scandir", "walk", "remove", "unlink", "makedirs"):
            _guard(os, name)
        _guard(os.path, "exists")
        _guard(os.path, "isdir")
        _guard(os.path, "isfile")
        _guard(shutil, "rmtree")
        _guard(shutil, "move", path_arg=1)

        def _guard_spawn(owner: Any, name: str) -> None:
            original = getattr(owner, name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                argv = args[0] if args else kwargs.get("args", [])
                values = list(argv) if isinstance(argv, (list, tuple)) else [argv]
                if any(forbidden(v) for v in [kwargs.get("cwd"), *values] if v):
                    raise LocalIOForbidden(f"{name} {argv!r} cwd={kwargs.get('cwd')}")
                return original(*args, **kwargs)

            patcher.setattr(owner, name, wrapper)

        _guard_spawn(subprocess, "run")
        _guard_spawn(subprocess, "Popen")
        _guard_spawn(subprocess, "check_output")
