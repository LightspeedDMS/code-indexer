"""REST surfaces of an activated copy on a mount-less pod (content + git seams).

Each route is run twice over the same copy content:

* ``local``: the copy is a directory on this disk (today's layout);
* ``delegated``: the pod's copy path does NOT exist (only, in some cases, its
  ``.code-indexer`` index state); the files live in a "remote" tree reached
  through a non-local content backend at the POD's path, git through a
  git-mcp stand-in. Every local access to the pod copy outside its index state
  raises (``install_local_io_guards``).

The answers must be equal. Delegated failures must be errors, never an empty
repository or "not found".

Routes: ``GET /api/repos/{alias}`` (storage, activity), ``GET
/api/repos/{alias}/files``, ``GET /api/repositories/{id}`` (statistics),
``GET /api/repositories/{id}/files?content=true`` and ``GET
/api/repositories/{id}/stats``.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.routers.inline_repos import register_repo_routes
from code_indexer.server.routers.inline_repos_v2 import register_repos_v2_routes
from code_indexer.server.services import file_service as file_service_module
from code_indexer.server.services import stats_service as stats_module
from code_indexer.server.services.activated_copy_content import (
    CopyContentUnavailableError,
    activation_created_at,
)
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from tests.unit.server.activated_seam_fakes import (
    INDEX_DIR,
    LocalIOForbidden,
    install_local_io_guards,
)

USER = "alice"
ALIAS = "my-copy"
OTHER = "bob"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
}


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_GIT_ENV)
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _write_tree(root: Path) -> None:
    """Files of every kind the routes treat differently."""
    root.mkdir(parents=True)
    (root / "README.md").write_text("hello\n")
    (root / ".env.example").write_text("DOT=1\n" * 6000)  # counted only if not hidden
    (root / "late.bin").write_bytes(b"a" * 100 + b"\x00")  # NUL past the first bytes
    (root / "crlf.txt").write_bytes(b"one\r\ntwo\rthree\n")
    (root / "image.bin").write_bytes(b"\x89PNG\x00\x01\x02" * 50)
    (root / "latin1.txt").write_bytes(b"caf\xe9\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('app')\n")
    (root / "src" / "util.js").write_text("x = 1;\n")
    (root / "src" / "deep").mkdir()
    (root / "src" / "deep" / "a.java").write_text("class A {}\n")
    (root / "src" / "deep" / "b.ts").write_text("let b = 1;\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# guide\n" * 40)
    (root / ".github").mkdir()
    (root / ".github" / "ci.yml").write_text("on: push\n")
    for n, path in enumerate(sorted(root.rglob("*"))):
        stamp = 1_700_000_000_123_456_789 + n * 1_000_003
        os.utime(path, ns=(stamp, stamp))  # the same times in both trees


def _write_index(index: Path) -> None:
    (index / "index" / "coll").mkdir(parents=True)
    (index / "config.json").write_text('{"embedding_provider": "voyage-ai"}')
    (index / ".hidden-state").write_bytes(b"s" * 300)
    (index / "index" / "coll" / "hnsw_index.bin").write_bytes(b"\x00" * 200_000)


class FakeActivatedManager:
    """The registry: which copies THIS user activated, and where they live."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.records: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(self, username: str, alias: str) -> None:
        self.records[(username, alias)] = {
            "user_alias": alias,
            "username": username,
            "path": self.get_activated_repo_path(username, alias),
            "golden_repo_alias": "example-repo",
            "current_branch": "main",
            "activated_at": "2024-03-04T05:06:07+00:00",
            "last_accessed": "2024-03-05T05:06:07+00:00",
            "git_url": "https://example.com/r.git",
        }

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self.data_dir / "activated-repos" / username / user_alias)

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [copy.deepcopy(r) for (u, _), r in self.records.items() if u == username]

    def _load_metadata(self, username: str, alias: str) -> Optional[Dict[str, Any]]:
        record = self.records.get((username, alias))
        return copy.deepcopy(record) if record else None

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return (username, alias) in self.records

    def get_repository(
        self, username: str, user_alias: str, **_: Any
    ) -> Optional[Dict[str, Any]]:
        return self._load_metadata(username, user_alias)

    def get_current_branch(self, username: str, user_alias: str) -> str:
        return "main"

    def list_repository_branches(self, username: str, user_alias: str) -> dict:
        return {"branches": []}


class RemoteContent:
    """workspace-mcp's content API: the POD path read from the remote tree."""

    is_local = False

    def __init__(self, pod_root: Path, remote_root: Path) -> None:
        self._pod_root = pod_root
        self._remote_root = remote_root
        self._local = LocalContentBackend()
        self.fail: Optional[Exception] = None
        self.fetch_error: Optional[str] = None
        self.calls: List[str] = []

    def _map(self, repo_path: Any) -> Path:
        mapped = self._remote_root / Path(os.fspath(repo_path)).relative_to(
            self._pod_root
        )
        if not mapped.is_dir():
            raise FileNotFoundError(f"404 path not found: {repo_path}")
        return mapped

    def manifest(self, repo_path: Any, **kwargs: Any) -> Any:
        self.calls.append("manifest")
        if self.fail is not None:
            raise self.fail
        return self._local.manifest(self._map(repo_path), **kwargs)

    def fetch(self, repo_path: Any, paths: Any) -> Iterator[Any]:
        self.calls.append("fetch")
        if self.fail is not None:
            raise self.fail
        if self.fetch_error is not None:
            error = self.fetch_error
            return iter(
                FetchedFile(path=p, blob_sha=None, data=b"", error=error) for p in paths
            )
        return self._local.fetch(self._map(repo_path), paths)


class RemoteGit:
    """git-mcp: git run on the remote tree, addressed by the POD path."""

    def __init__(self, pod_root: Path, remote_root: Path) -> None:
        self._pod_root = pod_root
        self._remote_root = remote_root
        self._inner = LocalGitBackend()
        self.calls: List[str] = []

    def log_raw(self, repo_path: str, fmt: str, *args: Any, **kwargs: Any) -> bytes:
        self.calls.append(f"log_raw {fmt}")
        mapped = self._remote_root / Path(repo_path).relative_to(self._pod_root)
        return self._inner.log_raw(str(mapped), fmt, *args, **kwargs)


class World:
    """The same copy, local and mount-less."""

    def __init__(
        self,
        tmp: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        with_git: bool = False,
        with_index: bool = False,
    ) -> None:
        self.monkeypatch = monkeypatch
        self.local_data = tmp / "local" / "data"
        self.pod_root = tmp / "pod"
        self.remote_root = tmp / "remote"
        self.pod_data = self.pod_root / "data"
        self.local = FakeActivatedManager(self.local_data)
        self.pod = FakeActivatedManager(self.pod_data)
        for manager in (self.local, self.pod):
            manager.add(USER, ALIAS)
            manager.add(OTHER, "theirs")
        local_copy = Path(self.local.get_activated_repo_path(USER, ALIAS))
        self.pod_copy = Path(self.pod.get_activated_repo_path(USER, ALIAS))
        self.remote_copy = self.remote_root / self.pod_copy.relative_to(self.pod_root)
        for root in (local_copy, self.remote_copy):
            _write_tree(root)
            if with_git:
                _git(root, "init", "-q", "-b", "main")
                _git(root, "add", ".")
                _git(root, "commit", "-q", "-m", "first")
                (root / "src" / "app.py").write_text("print('app 2')\n")
                _git(root, "commit", "-q", "-am", "second change")
        if with_index:
            _write_index(local_copy / INDEX_DIR)
            _write_index(self.pod_copy / INDEX_DIR)
            # A stale index inside the copy on workspace-mcp is not this
            # copy's index: the pod's is.
            (self.remote_copy / INDEX_DIR / "stale").mkdir(parents=True)
            (self.remote_copy / INDEX_DIR / "stale" / "x.bin").write_bytes(b"x" * 999)
        self.content = RemoteContent(self.pod_root, self.remote_root)
        self.git = RemoteGit(self.pod_root, self.remote_root)
        file_service_module._collect_cache.clear()

    # -- deployments -------------------------------------------------------

    def use_local(self) -> FakeActivatedManager:
        self.monkeypatch.setattr(
            content_backend_wiring, "server_content_backend", lambda: None
        )
        self.monkeypatch.setattr(
            git_backend_wiring, "server_delegated_git_backend", lambda: None
        )
        self._park(self.local)
        return self.local

    def use_delegated(self, *, git: bool = True) -> FakeActivatedManager:
        self.monkeypatch.setattr(
            content_backend_wiring, "server_content_backend", lambda: self.content
        )
        self.monkeypatch.setattr(
            git_backend_wiring,
            "server_delegated_git_backend",
            lambda: self.git if git else None,
        )
        self._park(self.pod)
        return self.pod

    def _park(self, manager: FakeActivatedManager) -> None:
        from code_indexer.server import app as app_module

        self.monkeypatch.setattr(
            app_module.app.state, "activated_repo_manager", manager, raising=False
        )

    def client(self, manager: FakeActivatedManager, username: str = USER) -> Any:
        app = FastAPI()
        register_repo_routes(
            app,
            activated_repo_manager=manager,
            golden_repo_manager=Mock(),
            repository_listing_manager=Mock(),
            background_job_manager=Mock(),
        )
        listing = Mock()
        listing.get_repository_details.side_effect = AssertionError(
            "fell through to golden repositories"
        )
        register_repos_v2_routes(
            app,
            activated_repo_manager=manager,
            repository_listing_manager=listing,
            background_job_manager=Mock(),
        )
        user = Mock()
        user.username = username
        app.dependency_overrides[dependencies.get_current_user] = lambda: user
        return TestClient(app)

    # -- the proof ---------------------------------------------------------

    def _forbidden(self, value: Any) -> bool:
        try:
            text = os.fspath(value)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        full = os.path.abspath(text)
        root = str(self.pod_copy)
        if not (full == root or full.startswith(root + os.sep)):
            return False
        rel = Path(full).relative_to(root).parts
        return not (rel and rel[0] == INDEX_DIR)

    def forbid_local_io(self) -> Any:
        patcher = pytest.MonkeyPatch()
        install_local_io_guards(patcher, self._forbidden)
        return patcher

    def run(self, method: str, url: str, *, delegated: bool, **kw: Any) -> Any:
        if not delegated:
            return getattr(self.client(self.use_local()), method)(url, **kw)
        client = self.client(self.use_delegated())
        patcher = self.forbid_local_io()
        try:
            return getattr(client, method)(url, **kw)
        finally:
            patcher.undo()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def _without_git(entries: List[dict]) -> List[dict]:
    return [
        e for e in entries if e["path"] != ".git" and not e["path"].startswith(".git/")
    ]


def _key(entries: List[dict]) -> List[tuple]:
    return sorted((e["path"], e["type"], e["size"], e["name"]) for e in entries)


# ---------------------------------------------------------------------------
# GET /api/repos/{alias}/files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?path=src",
        "?path=src/deep",
        "?path=./src/",
        "?recursive=true",
        "?recursive=true&path=src",
        "?recursive=true&max_depth=1",
        "?recursive=true&max_depth=2",
    ],
)
def test_files_listing_matches_local(world: World, query: str) -> None:
    local = world.run("get", f"/api/repos/{ALIAS}/files{query}", delegated=False)
    remote = world.run("get", f"/api/repos/{ALIAS}/files{query}", delegated=True)
    assert local.status_code == remote.status_code == 200
    a, b = local.json(), remote.json()
    assert _key(a["files"]) == _key(b["files"])
    assert {k: v for k, v in a.items() if k != "files"} == {
        k: v for k, v in b.items() if k != "files"
    }
    assert world.content.calls, "delegated listing never asked the content backend"


def test_files_recursive_order_is_sorted_top_down(world: World) -> None:
    body = world.run(
        "get", f"/api/repos/{ALIAS}/files?recursive=true", delegated=True
    ).json()
    paths = [e["path"] for e in body["files"]]
    # root: subdirs sorted, then files sorted; then descend sorted.
    assert paths[:3] == [".github", "docs", "src"]
    assert paths[3:9] == [
        ".env.example",
        "README.md",
        "crlf.txt",
        "image.bin",
        "late.bin",
        "latin1.txt",
    ]
    assert paths[9:] == [
        ".github/ci.yml",
        "docs/guide.md",
        "src/deep",
        "src/app.py",
        "src/util.js",
        "src/deep/a.java",
        "src/deep/b.ts",
    ]


@pytest.mark.parametrize("limit", [1, 3, 9, 10])
def test_files_truncation_matches_local_on_the_root(world: World, limit: int) -> None:
    url = f"/api/repos/{ALIAS}/files?recursive=true&max_depth=1&limit={limit}"
    a = world.run("get", url, delegated=False).json()
    b = world.run("get", url, delegated=True).json()
    assert (a["count"], a["truncated"]) == (b["count"], b["truncated"])
    if not a["truncated"]:
        assert _key(a["files"]) == _key(b["files"])


@pytest.mark.parametrize(
    "path,status",
    [
        ("nope", 404),
        ("src/missing", 404),
        ("README.md", 400),
        ("../other", 400),
        ("/etc", 400),
    ],
)
def test_files_errors_match_local(world: World, path: str, status: int) -> None:
    url = f"/api/repos/{ALIAS}/files?path={path}"
    local = world.run("get", url, delegated=False)
    remote = world.run("get", url, delegated=True)
    assert local.status_code == remote.status_code == status
    assert local.json() == remote.json()


def test_files_other_users_copy_is_not_found_and_not_read(world: World) -> None:
    response = world.client(world.use_delegated()).get(f"/api/repos/{ALIAS}/files")
    assert response.status_code == 200
    client = world.client(world.use_delegated(), username="mallory")
    world.content.calls.clear()
    response = client.get(f"/api/repos/{ALIAS}/files")
    assert response.status_code == 404
    assert world.content.calls == []


def test_files_backend_failure_is_500_not_empty(world: World) -> None:
    world.content.fail = ConnectionError("workspace-mcp unreachable")
    response = world.run("get", f"/api/repos/{ALIAS}/files", delegated=True)
    assert response.status_code == 500
    assert "workspace-mcp unreachable" in response.json()["detail"]


def test_files_copy_missing_on_workspace_mcp_is_500(world: World) -> None:
    shutil.rmtree(world.remote_copy)
    response = world.run("get", f"/api/repos/{ALIAS}/files", delegated=True)
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/repos/{alias}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_index", [False, True])
def test_info_storage_and_activity_match_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_index: bool
) -> None:
    world = World(tmp_path, monkeypatch, with_git=True, with_index=with_index)
    url = f"/api/repos/{ALIAS}?activity=true"
    local = world.run("get", url, delegated=False)
    remote = world.run("get", url, delegated=True)
    assert local.status_code == remote.status_code == 200
    a, b = local.json(), remote.json()
    assert a["storage_info"] == b["storage_info"]
    assert ("index_size_mb" in b["storage_info"]) is with_index
    assert a["activity"] == b["activity"]
    assert len(b["activity"]["recent_commits"]) == 2
    assert world.git.calls == ["log_raw %h %s"]


def test_info_git_failure_is_500_not_empty_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, monkeypatch, with_git=False)  # no repository: git fails
    response = world.run("get", f"/api/repos/{ALIAS}?activity=true", delegated=True)
    assert response.status_code == 500


def test_info_without_delegated_git_is_500_not_local_git(world: World) -> None:
    client = world.client(world.use_delegated(git=False))
    patcher = world.forbid_local_io()
    try:
        response = client.get(f"/api/repos/{ALIAS}?activity=true")
    finally:
        patcher.undo()
    assert response.status_code == 500
    assert "git_backend='delegated'" in response.json()["detail"]


def test_info_backend_failure_is_500_not_zero_usage(world: World) -> None:
    world.content.fail = ConnectionError("workspace-mcp unreachable")
    response = world.run("get", f"/api/repos/{ALIAS}", delegated=True)
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/repositories/{id}  and  /files?content=true
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_index", [False, True])
def test_details_statistics_match_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_index: bool
) -> None:
    world = World(tmp_path, monkeypatch, with_index=with_index)
    a = world.run("get", f"/api/repositories/{ALIAS}", delegated=False).json()
    b = world.run("get", f"/api/repositories/{ALIAS}", delegated=True).json()
    assert b["errors"] == []
    assert a["statistics"]["total_files"] == b["statistics"]["total_files"]
    assert a["statistics"]["total_size_bytes"] == b["statistics"]["total_size_bytes"]
    assert sorted(a["statistics"]["languages"]) == sorted(b["statistics"]["languages"])
    for body in (a, b):
        body["statistics"]["languages"].sort()  # a set's order, either side
    assert {k: v for k, v in a.items() if k != "path"} == {
        k: v for k, v in b.items() if k != "path"
    }


def test_details_backend_failure_is_500_not_degraded_200(world: World) -> None:
    world.content.fail = ConnectionError("workspace-mcp unreachable")
    response = world.run("get", f"/api/repositories/{ALIAS}", delegated=True)
    assert response.status_code == 500
    assert "workspace-mcp unreachable" in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "crlf.txt",
        "image.bin",
        "late.bin",
        "latin1.txt",
        "src/app.py",
        "./src/app.py",
    ],
)
def test_file_content_matches_local(world: World, path: str) -> None:
    url = f"/api/repositories/{ALIAS}/files?content=true&path={path}"
    local = world.run("get", url, delegated=False)
    remote = world.run("get", url, delegated=True)
    assert local.status_code == remote.status_code == 200
    assert local.json() == remote.json()


@pytest.mark.parametrize(
    "path,status", [("nope.txt", 404), ("src", 400), (".", 400), ("../x", 404)]
)
def test_file_content_errors_match_local(world: World, path: str, status: int) -> None:
    url = f"/api/repositories/{ALIAS}/files?content=true&path={path}"
    local = world.run("get", url, delegated=False)
    remote = world.run("get", url, delegated=True)
    assert local.status_code == remote.status_code == status
    assert local.json() == remote.json()


def test_file_content_fetch_record_error_is_500_not_empty(world: World) -> None:
    world.content.fetch_error = "EIO: read failed"
    url = f"/api/repositories/{ALIAS}/files?content=true&path=README.md"
    response = world.run("get", url, delegated=True)
    assert response.status_code == 500
    assert "EIO" in response.json()["detail"]


def test_file_content_backend_failure_is_500(world: World) -> None:
    world.content.fail = ConnectionError("workspace-mcp unreachable")
    url = f"/api/repositories/{ALIAS}/files?content=true&path=README.md"
    response = world.run("get", url, delegated=True)
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# RepositoryStatsService (/api/repositories/{id}/stats, repository_status)
# ---------------------------------------------------------------------------


def _strip_activity(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "activity"}


def test_stats_match_local_for_a_tree_without_git(world: World) -> None:
    url = f"/api/repositories/{ALIAS}/stats"
    local = world.run("get", url, delegated=False)
    remote = world.run("get", url, delegated=True)
    assert local.status_code == remote.status_code == 200
    assert _strip_activity(local.json()) == _strip_activity(remote.json())


def test_stats_include_index_state_like_the_local_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path, monkeypatch, with_index=True)
    url = f"/api/repositories/{ALIAS}/stats"
    a = world.run("get", url, delegated=False).json()
    b = world.run("get", url, delegated=True).json()
    assert _strip_activity(a) == _strip_activity(b)


def test_stats_file_records_match_local(world: World) -> None:
    """Path, size, language and modified time per file, local vs manifest."""
    world.use_local()
    service = stats_module.RepositoryStatsService()
    local_copy = world.local.get_activated_repo_path(USER, ALIAS)
    local = service._collect_file_statistics(local_copy)
    world.use_delegated()
    source = file_service_module.content_seam_source(str(world.pod_copy))
    assert source is not None
    patcher = world.forbid_local_io()
    try:
        remote = service._collect_file_statistics_via_seam(str(world.pod_copy), source)
    finally:
        patcher.undo()

    def rows(stats: list) -> list:
        return sorted(
            (s.path, s.size_bytes, s.language, s.is_indexed, s.modified_at)
            for s in stats
        )

    assert rows(local) == rows(remote)


def test_stats_activity_comes_from_the_registry(world: World) -> None:
    body = world.run("get", f"/api/repositories/{ALIAS}/stats", delegated=True).json()
    assert body["activity"]["created_at"].startswith("2024-03-04T05:06:07")


def test_stats_other_user_is_404_and_nothing_is_read(world: World) -> None:
    client = world.client(world.use_delegated(), username="mallory")
    world.content.calls.clear()
    response = client.get(f"/api/repositories/{ALIAS}/stats")
    assert response.status_code == 404
    assert world.content.calls == []


def test_stats_backend_failure_raises_not_empty(world: World) -> None:
    world.use_delegated()
    world.content.fail = ConnectionError("workspace-mcp unreachable")
    with pytest.raises(CopyContentUnavailableError):
        stats_module.RepositoryStatsService().get_repository_stats(ALIAS, username=USER)


def test_stats_local_default_is_unchanged(world: World) -> None:
    """Pins the local walk: rglob over everything, .git included."""
    world.use_local()
    local_copy = Path(world.local.get_activated_repo_path(USER, ALIAS))
    _git(local_copy, "init", "-q", "-b", "main")
    stats = stats_module.RepositoryStatsService().get_repository_stats(
        ALIAS, username=USER
    )
    expected = [p for p in local_copy.rglob("*") if p.is_file()]
    assert stats.files.total == len(expected)
    assert stats.storage.repository_size_bytes == sum(
        p.stat().st_size for p in expected
    )
    assert world.content.calls == []


def test_info_local_default_is_unchanged(world: World) -> None:
    """Pins the local storage walk: non-dot files outside .git."""
    _write_index(Path(world.local.get_activated_repo_path(USER, ALIAS)) / INDEX_DIR)
    body = world.run("get", f"/api/repos/{ALIAS}", delegated=False).json()
    local_copy = Path(world.local.get_activated_repo_path(USER, ALIAS))
    counted = [
        p for p in local_copy.rglob("*") if p.is_file() and not p.name.startswith(".")
    ]
    index = [p for p in (local_copy / INDEX_DIR).rglob("*") if p.is_file()]
    assert body["storage_info"] == {
        "disk_usage_mb": round(sum(p.stat().st_size for p in counted) / 1048576, 2),
        "index_size_mb": round(sum(p.stat().st_size for p in index) / 1048576, 2),
    }
    assert world.content.calls == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-03-04T05:06:07+00:00", "2024-03-04T05:06:07+00:00"),
        ("2024-03-04 05:06:07", "2024-03-04T05:06:07+00:00"),
        ("2024-03-04T07:06:07+02:00", "2024-03-04T05:06:07+00:00"),
    ],
)
def test_activation_created_at(raw: str, expected: str) -> None:
    assert activation_created_at({"activated_at": raw}).isoformat() == expected


@pytest.mark.parametrize("record", [{}, {"activated_at": ""}, {"activated_at": "x"}])
def test_activation_created_at_refuses_to_guess(record: dict) -> None:
    with pytest.raises(CopyContentUnavailableError):
        activation_created_at(record)


def test_guard_catches_a_pod_copy_read(world: World) -> None:
    """The proof is live: a local read of the pod copy raises."""
    world.pod_copy.mkdir(parents=True)
    patcher = world.forbid_local_io()
    try:
        with pytest.raises(LocalIOForbidden):
            os.listdir(world.pod_copy)
        os.listdir(world.pod_copy.parent)  # outside the copy: allowed
    finally:
        patcher.undo()


# ---------------------------------------------------------------------------
# GET /api/repositories/{id}/files on a composite copy
# ---------------------------------------------------------------------------

COMPOSITE = "combo"


def _composite(world: World, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """A composite activation in both registries; its listing recorded."""
    from code_indexer.server.routers import inline_repos_v2

    for manager in (world.local, world.pod):
        manager.add(USER, COMPOSITE)
        manager.records[(USER, COMPOSITE)]["is_composite"] = True
    calls: List[Any] = []

    def listing(repo: Any, path: str = "", recursive: bool = False) -> list:
        calls.append(path)
        return []

    monkeypatch.setattr(inline_repos_v2, "_list_composite_files", listing)
    return calls


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("path", ["/etc", "..", "../x", "a/../../x", "a/.."])
def test_composite_listing_refuses_traversal(
    world: World, monkeypatch: pytest.MonkeyPatch, delegated: bool, path: str
) -> None:
    calls = _composite(world, monkeypatch)
    url = f"/api/repositories/{COMPOSITE}/files?path={path}"
    response = world.run("get", url, delegated=delegated)
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("delegated", [False, True])
def test_composite_listing_passes_a_relative_path(
    world: World, monkeypatch: pytest.MonkeyPatch, delegated: bool
) -> None:
    calls = _composite(world, monkeypatch)
    url = f"/api/repositories/{COMPOSITE}/files?path=src/deep"
    response = world.run("get", url, delegated=delegated)
    assert response.status_code == 200
    assert response.json() == {"files": []}
    assert calls == ["src/deep"]


@pytest.mark.parametrize("delegated", [False, True])
def test_composite_listing_failure_is_500_not_the_regular_listing(
    world: World, monkeypatch: pytest.MonkeyPatch, delegated: bool
) -> None:
    from code_indexer.server.routers import inline_repos_v2

    _composite(world, monkeypatch)

    def broken(repo: Any, path: str = "", recursive: bool = False) -> list:
        raise ConnectionError("workspace-mcp unreachable")

    monkeypatch.setattr(inline_repos_v2, "_list_composite_files", broken)
    regular = Mock(side_effect=AssertionError("fell through to the regular listing"))
    monkeypatch.setattr(inline_repos_v2.file_service, "list_files", regular)
    response = world.run(
        "get", f"/api/repositories/{COMPOSITE}/files", delegated=delegated
    )
    assert response.status_code == 500
    assert "workspace-mcp unreachable" in response.json()["detail"]
    regular.assert_not_called()


@pytest.mark.parametrize("delegated", [False, True])
def test_registry_failure_is_500_not_the_regular_listing(
    world: World, monkeypatch: pytest.MonkeyPatch, delegated: bool
) -> None:
    from code_indexer.server.routers import inline_repos_v2

    for manager in (world.local, world.pod):
        monkeypatch.setattr(
            manager, "get_repository", Mock(side_effect=RuntimeError("registry down"))
        )
    regular = Mock(side_effect=AssertionError("fell through to the regular listing"))
    monkeypatch.setattr(inline_repos_v2.file_service, "list_files", regular)
    response = world.run("get", f"/api/repositories/{ALIAS}/files", delegated=delegated)
    assert response.status_code == 500
    assert "registry down" in response.json()["detail"]
    regular.assert_not_called()
