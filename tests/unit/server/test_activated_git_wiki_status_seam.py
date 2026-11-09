"""Activated-copy routes that still read the pod's own disk, on a mount-less pod.

Each surface runs over the same copy twice:

* ``local``: the copy is a directory on this disk (today's layout);
* ``delegated``: the pod's copy path holds nothing (at most its
  ``.code-indexer``); the files are in a "remote" tree reached through a
  non-local content backend at the POD's path, git through a git-mcp
  stand-in. Every local access to the pod copy outside its index state, and
  every subprocess run in or naming it, raises (``install_local_io_guards``).

Surfaces: the REST git reads ``/cat``, ``/blame`` and ``/file-history``
(``routers/git.py``), the user wiki ``/wiki/u/<user>/<alias>/...``
(``wiki/routes.py``), the activated index status route
(``routers/activated_repos.py``) and the health route's copy presence
(``routers/repository_health.py``). The answers must be equal; a delegated
failure is an error, never "not found" or an empty wiki.
"""

from __future__ import annotations

import re

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from code_indexer.server.routers import activated_repos as activated_routes
from code_indexer.server.routers import git as git_routes
from code_indexer.server.routers import repository_health
from code_indexer.server.services.access_filtering_service import _ActivationSource
from code_indexer.server.services.git_tools_seam import GitToolsUnavailableError
from code_indexer.server.startup import git_backend_wiring, index_store_wiring
from code_indexer.server.storage.shared.git_backend import local_git_backend
from cidx_wiki.server import routes as wiki_routes
from cidx_wiki.server.routes import (
    get_current_user_hybrid,
    get_wiki_user_hybrid,
    wiki_router,
)
from tests.unit.server.activated_seam_fakes import INDEX_DIR, LocalIOForbidden
from tests.unit.server.test_activated_rest_surfaces_content_seam import (
    ALIAS,
    USER,
    World,
)


#: git-mcp's OBJECT_NAME_RE (git-api.service.ts): git_show_objects takes ids.
_GIT_MCP_OBJECT_NAME = re.compile(r"^[0-9a-fA-F]{4,64}$")

#: The git_exec options (git-exec.policy.ts EXEC_RULES) the read routes use;
#: anything else is refused there, so it is refused here.
_GIT_EXEC_OPTIONS = {
    "blame": ({"--porcelain"}, ()),
    "log": ({"--follow"}, ("--format=", "-n")),
}


def _require_git_exec_policy(args: Sequence[str]) -> None:
    """The subset of git-mcp's git_exec argv policy these routes reach."""
    if args[0] not in _GIT_EXEC_OPTIONS:
        raise RuntimeError(f"git_exec does not run {args[0]!r}")
    exact, prefixes = _GIT_EXEC_OPTIONS[args[0]]
    separated = False
    for arg in args[1:]:
        if arg == "--" and not separated:
            separated = True
        elif separated or not arg.startswith("-"):
            if arg.startswith("/") or arg == ".." or arg.startswith("../"):
                raise RuntimeError(f"git_exec refuses operand {arg!r}")
        elif arg not in exact and not arg.startswith(prefixes):
            raise RuntimeError(f"git_exec refuses option {arg!r} for {args[0]}")


class RemoteGitRunner:
    """git-mcp: every git op runs on the remote tree, addressed by POD path.

    The read surface the routes use: ``rev_parse``, ``show_objects`` and
    ``run`` (git-mcp ``git_exec``), so it is a ``GitCommandRunner``.
    """

    owns_credentials = True

    def __init__(self, pod_root: Path, remote_root: Path) -> None:
        self._pod_root = pod_root
        self._remote_root = remote_root
        self._inner = local_git_backend()
        self.calls: List[str] = []
        self.unreachable = False
        self.show_unreachable = False

    def _map(self, repo_path: str) -> str:
        return str(self._remote_root / Path(repo_path).relative_to(self._pod_root))

    def _check(self) -> None:
        if self.unreachable:
            raise subprocess.CalledProcessError(1, ["git-mcp"], stderr="unreachable")

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self.calls.append(f"rev_parse {' '.join(args)}")
        self._check()
        return self._inner.rev_parse(self._map(repo_path), *args, timeout=timeout)

    def show_objects(
        self, repo_path: str, specs: Sequence[Tuple[str, str]], timeout: Any = None
    ) -> List[subprocess.CompletedProcess]:
        self.calls.append("show_objects")
        self._check()
        for rev, _ in specs:
            if not _GIT_MCP_OBJECT_NAME.match(rev):
                # git-mcp git_show_objects (GitApiService.requireObjectName)
                raise RuntimeError(
                    "git_show_objects: Object names must be hexadecimal commit "
                    f"or blob ids: {rev!r}"
                )
        if self.show_unreachable:
            raise subprocess.CalledProcessError(1, ["git-mcp"], stderr="reset")
        return self._inner.show_objects(self._map(repo_path), specs, timeout=timeout)

    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Any = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        self.calls.append(f"run {args[0]}")
        self._check()
        _require_git_exec_policy(args)
        return self._inner.run(self._map(repo_path), args, timeout=timeout)

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Any = None,
    ) -> subprocess.CompletedProcess:
        raise AssertionError("a read route pushed")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch, with_git=True)


def _delegate_git(world: World, monkeypatch: pytest.MonkeyPatch) -> RemoteGitRunner:
    runner = RemoteGitRunner(world.pod_root, world.remote_root)
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: runner
    )
    return runner


def _no_access_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_routes, "_require_activation_access", lambda a, u: None)


def _user() -> Any:
    user = MagicMock()
    user.username = USER
    return user


def _git_answer(call: Any) -> Tuple[int, Any]:
    try:
        return 200, call()
    except HTTPException as exc:
        return exc.status_code, exc.detail


GIT_CALLS = {
    "cat head": lambda: git_routes.git_cat(
        alias=ALIAS, path="src/app.py", rev="HEAD", user=_user()
    ),
    "cat first": lambda: git_routes.git_cat(
        alias=ALIAS, path="src/app.py", rev="HEAD~1", user=_user()
    ),
    "cat latin1": lambda: git_routes.git_cat(
        alias=ALIAS, path="latin1.txt", rev="HEAD", user=_user()
    ),
    "cat binary": lambda: git_routes.git_cat(
        alias=ALIAS, path="image.bin", rev="main", user=_user()
    ),
    "cat missing file": lambda: git_routes.git_cat(
        alias=ALIAS, path="nope.txt", rev="HEAD", user=_user()
    ),
    "cat bad rev": lambda: git_routes.git_cat(
        alias=ALIAS, path="src/app.py", rev="no-such-rev", user=_user()
    ),
    "blame": lambda: git_routes.git_blame(
        alias=ALIAS, path="src/app.py", rev="HEAD", user=_user()
    ),
    "blame missing": lambda: git_routes.git_blame(
        alias=ALIAS, path="nope.txt", rev="HEAD", user=_user()
    ),
    "history": lambda: git_routes.git_file_history(
        alias=ALIAS, path="src/app.py", rev=None, limit=50, user=_user()
    ),
    "history one": lambda: git_routes.git_file_history(
        alias=ALIAS, path="src/app.py", rev="HEAD", limit=1, user=_user()
    ),
    "history bad rev": lambda: git_routes.git_file_history(
        alias=ALIAS, path="src/app.py", rev="no-such-rev", limit=5, user=_user()
    ),
}


class TestRestGitReads:
    @pytest.mark.parametrize("name", sorted(GIT_CALLS))
    def test_delegated_matches_local(
        self, world: World, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        _no_access_check(monkeypatch)
        world.use_local()
        local = _git_answer(GIT_CALLS[name])
        world.use_delegated()
        runner = _delegate_git(world, monkeypatch)
        patcher = world.forbid_local_io()
        try:
            remote = _git_answer(GIT_CALLS[name])
        finally:
            patcher.undo()
        assert remote == local
        assert runner.calls, "the delegated route never asked git-mcp"

    def test_the_fixture_exercises_hits_and_misses(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_access_check(monkeypatch)
        world.use_local()
        answers = {name: _git_answer(call) for name, call in GIT_CALLS.items()}
        assert answers["cat head"][0] == 200
        assert answers["cat head"][1]["content"] == "print('app 2')\n"
        assert answers["cat first"][1]["content"] == "print('app')\n"
        assert answers["cat latin1"][1]["content"] == "café\n"
        assert answers["cat missing file"][0] == 404
        assert answers["cat bad rev"][0] == 404
        assert answers["blame missing"][0] == 404
        assert len(answers["history"][1]["commits"]) == 2
        assert answers["history bad rev"][0] == 404

    @pytest.mark.parametrize("name", ["cat head", "blame", "history"])
    def test_unreachable_git_mcp_is_an_error_not_not_found(
        self, world: World, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        _no_access_check(monkeypatch)
        world.use_delegated()
        runner = _delegate_git(world, monkeypatch)
        runner.unreachable = True
        patcher = world.forbid_local_io()
        try:
            with pytest.raises(GitToolsUnavailableError):
                GIT_CALLS[name]()
        finally:
            patcher.undo()

    def test_a_show_transport_failure_is_an_error_not_not_found(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_access_check(monkeypatch)
        world.use_delegated()
        runner = _delegate_git(world, monkeypatch)
        runner.show_unreachable = True
        patcher = world.forbid_local_io()
        try:
            with pytest.raises(GitToolsUnavailableError):
                GIT_CALLS["cat head"]()
        finally:
            patcher.undo()

    @pytest.mark.parametrize("name", ["cat head", "blame", "history"])
    def test_no_delegated_git_backend_refuses(
        self, world: World, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        from code_indexer.server.services.delegated_refusal import (
            DelegatedRepositoryUnsupportedError,
        )

        _no_access_check(monkeypatch)
        world.use_delegated(git=False)
        patcher = world.forbid_local_io()
        try:
            with pytest.raises(DelegatedRepositoryUnsupportedError):
                GIT_CALLS[name]()
        finally:
            patcher.undo()

    def test_the_guard_sees_a_local_git_run(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proof bites: local git in the pod copy raises under the guard."""
        patcher = world.forbid_local_io()
        try:
            with pytest.raises(LocalIOForbidden):
                subprocess.run(["git", "status"], cwd=str(world.pod_copy))
        finally:
            patcher.undo()


# ---------------------------------------------------------------------------
# user wiki
# ---------------------------------------------------------------------------

WIKI_FILES: Dict[str, bytes] = {
    "home.md": b"---\ntitle: Welcome\n---\n# Home\nSee [g](guide/intro.md)\n"
    b'<img src="img/logo.png">\n',
    "guide/intro.md": b"---\ncategory: Basics\n---\n# Intro\nBody\n",
    "a-b.md": b"# Dash\n",
    "a/b.md": b"---\ncategory: Zed\n---\n# Slash\n",
    ".hidden/secret.md": b"# Secret\n",
    "img/logo.png": b"\x89PNG\r\n\x1a\nfake-png-bytes",
    "bad.md": b"\xff\xfe not utf-8 \x80",
    "script.sh": b"#!/bin/sh\n",
}

WIKI_ROUTES = [
    "/wiki/u/alice/my-copy/",
    "/wiki/u/alice/my-copy/guide/intro",
    "/wiki/u/alice/my-copy/guide/intro.md",
    "/wiki/u/alice/my-copy/a-b",
    "/wiki/u/alice/my-copy/a/b",
    "/wiki/u/alice/my-copy/bad",
    "/wiki/u/alice/my-copy/missing",
    "/wiki/u/alice/my-copy/script.sh",
    "/wiki/u/alice/my-copy/%2E%2E/outside",
    "/wiki/u/alice/my-copy/_assets/img/logo.png",
    "/wiki/u/alice/my-copy/_assets/img/missing.png",
    "/wiki/u/alice/my-copy/_assets/script.sh",
    "/wiki/u/alice/other/",
]


def _seed_wiki(world: World, files: Dict[str, bytes]) -> None:
    local_copy = Path(world.local.get_activated_repo_path(USER, ALIAS))
    for root in (local_copy, world.remote_copy):
        for rel, data in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _wiki_client(tmp_path: Path, name: str, manager: Any) -> TestClient:
    wiki_routes._reset_wiki_cache()
    viewer = _user()
    app = FastAPI()
    app.dependency_overrides[get_current_user_hybrid] = lambda: viewer
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: viewer
    app.include_router(wiki_router, prefix="/wiki")
    state_dir = tmp_path / f"wiki-state-{name}"
    state_dir.mkdir()
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=state_dir)
    os.close(fd)
    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.db_path = db_path
    app.state.golden_repo_manager.golden_repos_dir = str(state_dir / "golden-repos")
    access = MagicMock()
    access.is_admin_user.return_value = False
    access.get_accessible_repos.return_value = {"example-repo"}
    access.caller_activation_sources.return_value = {
        ALIAS: _ActivationSource(golden=("example-repo",), collides=False)
    }
    app.state.access_filtering_service = access
    manager.get_wiki_enabled = lambda username, alias: True
    app.state.activated_repo_manager = manager
    return TestClient(app)


def _wiki_snapshot(client: TestClient) -> Dict[str, Tuple[int, Any, bytes]]:
    out = {}
    for url in WIKI_ROUTES:
        resp = client.get(url)
        out[url] = (resp.status_code, resp.headers.get("content-type"), resp.content)
    return out


class TestUserWiki:
    @pytest.fixture(autouse=True)
    def _fresh(self) -> Any:
        wiki_routes._reset_wiki_cache()
        yield
        wiki_routes._reset_wiki_cache()

    def test_every_route_matches_the_local_wiki(
        self, world: World, tmp_path: Path
    ) -> None:
        _seed_wiki(world, WIKI_FILES)
        local = _wiki_snapshot(_wiki_client(tmp_path, "local", world.use_local()))
        client = _wiki_client(tmp_path, "seam", world.use_delegated())
        patcher = world.forbid_local_io()
        try:
            seam = _wiki_snapshot(client)
        finally:
            patcher.undo()
        assert seam == local
        assert "manifest" in world.content.calls
        # The fixture exercises what it claims to.
        assert local["/wiki/u/alice/my-copy/"][0] == 200
        assert b"Welcome" in local["/wiki/u/alice/my-copy/"][2]
        assert b"u/alice/my-copy" in local["/wiki/u/alice/my-copy/guide/intro"][2]
        assert local["/wiki/u/alice/my-copy/missing"][0] == 404
        assert local["/wiki/u/alice/my-copy/bad"][0] == 404
        assert local["/wiki/u/alice/other/"][0] == 404
        assert (
            local["/wiki/u/alice/my-copy/_assets/img/logo.png"][2]
            == WIKI_FILES["img/logo.png"]
        )

    def test_index_page_without_home_matches(
        self, world: World, tmp_path: Path
    ) -> None:
        files = {k: v for k, v in WIKI_FILES.items() if k != "home.md"}
        _seed_wiki(world, files)
        local = _wiki_client(tmp_path, "local", world.use_local()).get(
            "/wiki/u/alice/my-copy/"
        )
        client = _wiki_client(tmp_path, "seam", world.use_delegated())
        patcher = world.forbid_local_io()
        try:
            seam = client.get("/wiki/u/alice/my-copy/")
        finally:
            patcher.undo()
        assert local.status_code == seam.status_code == 200
        assert seam.text == local.text
        assert "Secret" not in seam.text
        assert "guide/intro" in seam.text

    def test_backend_failure_is_503_not_an_empty_wiki(
        self, world: World, tmp_path: Path
    ) -> None:
        _seed_wiki(world, WIKI_FILES)
        client = _wiki_client(tmp_path, "seam", world.use_delegated())
        world.content.fail = OSError("workspace-mcp unreachable")
        patcher = world.forbid_local_io()
        try:
            for url in (
                "/wiki/u/alice/my-copy/",
                "/wiki/u/alice/my-copy/guide/intro",
                "/wiki/u/alice/my-copy/_assets/img/logo.png",
            ):
                resp = client.get(url)
                assert resp.status_code == 503, url
                assert "unreachable" in resp.text
        finally:
            patcher.undo()

    def test_presence_is_the_registry_for_this_user(
        self, world: World, tmp_path: Path
    ) -> None:
        """No pod directory at all: the registry decides, never a pod probe."""
        _seed_wiki(world, WIKI_FILES)
        manager = world.use_delegated()
        assert not world.pod_copy.exists()
        client = _wiki_client(tmp_path, "seam", manager)
        patcher = world.forbid_local_io()
        try:
            assert client.get("/wiki/u/alice/my-copy/").status_code == 200
            manager.records.pop((USER, ALIAS))
            wiki_routes._reset_wiki_cache()
            assert client.get("/wiki/u/alice/my-copy/").status_code == 404
        finally:
            patcher.undo()


# ---------------------------------------------------------------------------
# activated index status / health presence
# ---------------------------------------------------------------------------


class _DelegatedStore:
    is_local = False


class TestIndexStatusServesFirst:
    def _status(self, manager: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        async def _allowed(user_alias: str, current_user: Any) -> None:
            return None

        monkeypatch.setattr(activated_routes, "_require_activation_access", _allowed)
        monkeypatch.setattr(
            activated_routes, "_get_activated_repo_manager", lambda: manager
        )
        manager.activated_repos_dir = str(manager.data_dir / "activated-repos")
        return asyncio.run(
            activated_routes.get_indexes_status(ALIAS, current_user=_user(), owner=None)
        )

    def test_delegated_store_serves_the_recorded_index_before_reading(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = world.use_delegated()
        served: List[Tuple[str, str]] = []

        def serve_index_here(username: str, user_alias: str) -> str:
            # The checkout: the recorded version lands in the pod's index root.
            served.append((username, user_alias))
            (world.pod_copy / INDEX_DIR / "tantivy_index").mkdir(parents=True)
            (world.pod_copy / INDEX_DIR / "index" / "c").mkdir(parents=True)
            (world.pod_copy / INDEX_DIR / "index" / "c" / "hnsw_index.bin").write_bytes(
                b"\x00" * 10
            )
            return str(world.pod_copy)

        monkeypatch.setattr(
            manager, "serve_index_here", serve_index_here, raising=False
        )
        monkeypatch.setattr(
            index_store_wiring, "server_index_store", lambda: _DelegatedStore()
        )
        patcher = world.forbid_local_io()
        try:
            body = self._status(manager, monkeypatch)
        finally:
            patcher.undo()
        assert served == [(USER, ALIAS)]
        present = {i.index_type: i.exists for i in body.indexes}
        assert present["semantic"] is True
        assert present["fts"] is True

    def test_temporal_this_pod_cannot_tell_is_503_not_absent(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Temporal on, its pointer recording no temporal version, nothing on
        this pod: "not served here" (503), never temporal exists=False."""
        manager = world.use_delegated()

        def serve_index_here(username: str, user_alias: str) -> str:
            (world.pod_copy / INDEX_DIR / "index").mkdir(parents=True, exist_ok=True)
            return str(world.pod_copy)

        monkeypatch.setattr(
            manager, "serve_index_here", serve_index_here, raising=False
        )
        monkeypatch.setattr(
            index_store_wiring, "server_index_store", lambda: _DelegatedStore()
        )
        manager.golden_repo_manager.get_golden_repo.return_value.enable_temporal = True
        with pytest.raises(HTTPException) as caught:
            self._status(manager, monkeypatch)
        assert caught.value.status_code == 503
        assert "not served on this pod" in str(caught.value.detail)

    def test_a_failed_checkout_is_an_error_not_unindexed(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = world.use_delegated()

        def serve_index_here(username: str, user_alias: str) -> str:
            raise RuntimeError("index store unreachable")

        monkeypatch.setattr(
            manager, "serve_index_here", serve_index_here, raising=False
        )
        monkeypatch.setattr(
            index_store_wiring, "server_index_store", lambda: _DelegatedStore()
        )
        with pytest.raises(HTTPException) as caught:
            self._status(manager, monkeypatch)
        assert caught.value.status_code == 500
        assert "index store unreachable" in str(caught.value.detail)

    def test_local_store_serves_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = world.use_local()

        def serve_index_here(username: str, user_alias: str) -> str:
            raise AssertionError("served under the local store")

        monkeypatch.setattr(
            manager, "serve_index_here", serve_index_here, raising=False
        )
        body = self._status(manager, monkeypatch)
        assert {i.index_type for i in body.indexes} == {
            "semantic",
            "fts",
            "temporal",
            "scip",
        }


class TestHealthCopyPresence:
    def test_local_is_the_directory(self, world: World) -> None:
        manager = world.use_local()
        path = manager.get_activated_repo_path(USER, ALIAS)
        assert repository_health._activated_copy_present(manager, USER, ALIAS, path)
        assert not repository_health._activated_copy_present(
            manager, USER, "nope", manager.get_activated_repo_path(USER, "nope")
        )

    def test_delegated_is_the_registry_with_no_pod_probe(self, world: World) -> None:
        manager = world.use_delegated()
        path = manager.get_activated_repo_path(USER, ALIAS)
        assert not Path(path).exists()
        patcher = world.forbid_local_io()
        try:
            assert repository_health._activated_copy_present(manager, USER, ALIAS, path)
            assert not repository_health._activated_copy_present(
                manager, "bob", ALIAS, path
            )
        finally:
            patcher.undo()
