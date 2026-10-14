"""The activated-repos REST routes on a pod without the repositories.

Under ``content_backend='delegated'`` a user's activated copy is on the shared
volume only, at a path that does NOT exist on this machine in these tests. The
routes then take "is the copy there" from the registry (for THIS user), never
from a pod-path probe, and list branches through the git seam. Under the
local default they behave exactly as before.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryListingManager,
)
from code_indexer.server.routers import activated_repos as routes
from code_indexer.server.startup import content_backend_wiring as backend_wiring
from code_indexer.server.startup import git_backend_wiring
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

USER = "alice"
ALIAS = "example-repo"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=d@example.com", "-c", "user.name=D", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _NonLocal:
    is_local = False


class _MappedGit:
    """git-mcp by path: real git on the remote copy addressed by the pod path,
    returning what the git-mcp plugin returns (not what local git prints)."""

    def __init__(self, pod: Path, remote: Path) -> None:
        self._pod, self._remote = str(pod), remote
        self._git = LocalGitBackend()
        self.calls: List[str] = []

    def _map(self, path: str) -> str:
        assert path == self._pod, path
        return str(self._remote)

    def list_branches(self, repo_path: str, **kwargs: Any) -> List[str]:
        """git-mcp's git_branch_list is simple-git's branch summary: a
        detached HEAD is a bare ``<ref>`` entry, and names are unique."""
        self.calls.append("list_branches")
        names: List[str] = []
        for line in self._git.list_branches(self._map(repo_path), **kwargs):
            detached = re.match(r"^\(HEAD detached (?:at|from) (\S+)\)$", line)
            name = detached.group(1) if detached else line
            if name not in names:
                names.append(name)
        return names

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Any = None
    ) -> str:
        self.calls.append("symbolic_ref")
        return self._git.symbolic_ref(self._map(repo_path), short=short)

    def show_ref(
        self, repo_path: str, *args: str, timeout: Any = None
    ) -> "subprocess.CompletedProcess[str]":
        """git-mcp's git_show_ref never raises: any failure is ``found: false``."""
        self.calls.append("show_ref")
        result = self._git.show_ref(self._map(repo_path), *args)
        return subprocess.CompletedProcess(
            result.args, 0 if result.returncode == 0 else 1, result.stdout, ""
        )

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self.calls.append("rev_parse")
        return self._git.rev_parse(self._map(repo_path), *args)


class _TransportFailure(subprocess.CalledProcessError):
    """What the plugin raises when git-mcp is unreachable (no git stderr)."""

    def __init__(self) -> None:
        super().__init__(1, ["git-mcp", "git_symbolic_ref"], output=None, stderr=None)

    def __str__(self) -> str:
        return "git-mcp git_symbolic_ref: connection refused"


class _UnreachableHead(_MappedGit):
    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Any = None
    ) -> str:
        raise _TransportFailure()


class _NoDetachedEntry(_MappedGit):
    def list_branches(self, repo_path: str, **kwargs: Any) -> List[str]:
        return [n for n in super().list_branches(repo_path, **kwargs) if n.isalpha()]


class _BrokenShowRef(_MappedGit):
    def show_ref(
        self, repo_path: str, *args: str, timeout: Any = None
    ) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(["git", "show-ref", *args], 1, "", "")


class _Registry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return username == USER and alias == ALIAS

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self.root / username / user_alias)

    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return [
            {"username": USER, "user_alias": ALIAS, "golden_repo_alias": "golden"},
            {"username": USER, "user_alias": "two", "golden_repo_alias": "golden"},
            {"username": "bob", "user_alias": "x", "golden_repo_alias": "golden"},
            {"username": "carol", "user_alias": "y", "golden_repo_alias": "other"},
        ]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    local_root = tmp_path / "local" / "activated-repos"
    remote_root = tmp_path / "remote" / "activated-repos"
    pod_root = tmp_path / "not-mounted" / "activated-repos"
    for root in (local_root, remote_root):
        repo = root / USER / ALIAS
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        _git(repo, "branch", "feature")
        _git(repo, "branch", "zeta")
    app = FastAPI()
    app.include_router(routes.router)
    user = User(
        username=USER,
        password_hash="x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    git = _MappedGit(pod_root / USER / ALIAS, remote_root / USER / ALIAS)
    w = SimpleNamespace(
        local_root=local_root,
        remote_root=remote_root,
        pod_root=pod_root,
        git=git,
        client=TestClient(app, raise_server_exceptions=False),
        monkeypatch=monkeypatch,
    )
    _mode(w, delegated=False)
    yield w
    app.dependency_overrides.clear()


def _mode(w: SimpleNamespace, *, delegated: bool, git: Optional[Any] = None) -> None:
    backend: Optional[_NonLocal] = _NonLocal() if delegated else None
    w.monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: backend)
    chosen = (w.git if git is None else git) if delegated else None
    w.monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: chosen
    )
    root = w.pod_root if delegated else w.local_root
    w.monkeypatch.setattr(
        routes, "_get_activated_repo_manager", lambda: _Registry(root)
    )


def _branches(w: SimpleNamespace, alias: str = ALIAS) -> Tuple[int, Any]:
    response = w.client.get(f"/api/activated-repos/{alias}/branches")
    return response.status_code, response.json()


def test_branch_listing_is_identical_local_and_delegated(
    world: SimpleNamespace,
) -> None:
    local = _branches(world)
    _mode(world, delegated=True)
    delegated = _branches(world)

    assert local[0] == 200
    assert delegated == local
    assert delegated[1]["current_branch"] == "main"
    assert world.git.calls == ["list_branches", "symbolic_ref"]
    assert not world.pod_root.exists()


def test_another_users_alias_is_not_found_under_delegated(
    world: SimpleNamespace,
) -> None:
    _mode(world, delegated=True)
    status, _ = _branches(world, alias="someone-elses")
    assert status == 404
    assert world.git.calls == []


def test_no_delegated_git_backend_fails_loud(world: SimpleNamespace) -> None:
    world.monkeypatch.setattr(
        backend_wiring, "server_content_backend", lambda: _NonLocal()
    )
    world.monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )
    world.monkeypatch.setattr(
        routes, "_get_activated_repo_manager", lambda: _Registry(world.pod_root)
    )
    status, body = _branches(world)
    assert status == 500
    assert "git_backend='delegated'" in body["detail"]


def test_presence_comes_from_the_registry_not_the_pod_path(
    world: SimpleNamespace,
) -> None:
    _mode(world, delegated=True)
    present = routes._activated_copy_present(
        _Registry(world.pod_root), USER, ALIAS, world.pod_root / USER / ALIAS
    )
    absent = routes._activated_copy_present(
        _Registry(world.pod_root), "mallory", ALIAS, world.pod_root / USER / ALIAS
    )
    assert (present, absent) == (True, False)


def test_presence_is_the_directory_locally(world: SimpleNamespace) -> None:
    registry = _Registry(world.local_root)
    assert routes._activated_copy_present(
        registry, USER, ALIAS, world.local_root / USER / ALIAS
    )
    # Locally the directory decides, whatever the registry says.
    assert not routes._activated_copy_present(
        registry, USER, ALIAS, world.local_root / USER / "gone"
    )


def test_activation_count_comes_from_the_registry_under_delegated(
    world: SimpleNamespace,
) -> None:
    _mode(world, delegated=True)
    manager = RepositoryListingManager.__new__(RepositoryListingManager)
    setattr(manager, "activated_repo_manager", _Registry(world.pod_root))
    assert manager.get_activation_count("golden") == 2
    assert manager.get_activation_count("other") == 1
    assert manager.get_activation_count("nobody") == 0


def test_index_status_route_takes_presence_from_the_registry(
    world: SimpleNamespace,
) -> None:
    """A registered copy is not 404 just because this pod has no directory
    for it; an unregistered alias still is."""
    _mode(world, delegated=True)
    mine = world.client.get(f"/api/activated-repos/{ALIAS}/indexes")
    theirs = world.client.get("/api/activated-repos/someone-elses/indexes")
    assert mine.status_code == 200
    assert theirs.status_code == 404
    assert not world.pod_root.exists()


def _in_both_copies(w: SimpleNamespace, *args: str) -> None:
    for root in (w.local_root, w.remote_root):
        _git(root / USER / ALIAS, *args)


@pytest.mark.parametrize(
    "setup, expected",
    [
        pytest.param(
            [("checkout", "-q", "--detach", "HEAD")],
            "(HEAD detached at {sha})",
            id="detached-at-commit",
        ),
        pytest.param(
            [("tag", "v1"), ("checkout", "-q", "v1")],
            "(HEAD detached at v1)",
            id="detached-at-tag",
        ),
        pytest.param(
            [
                ("tag", "v1"),
                ("checkout", "-q", "v1"),
                ("commit", "-q", "--allow-empty", "-m", "moved"),
            ],
            "(HEAD detached from v1)",
            id="detached-from-tag",
        ),
        pytest.param(
            [("checkout", "-q", "--detach", "main")],
            "(HEAD detached at refs/heads/main)",
            id="detached-at-branch",
        ),
    ],
)
def test_detached_head_is_reported_like_local_git(
    world: SimpleNamespace, setup: List[Tuple[str, ...]], expected: str
) -> None:
    """A detached HEAD is a normal state, listed exactly as local `git branch`
    reports it -- not an empty current branch."""
    sha = _git(world.local_root / USER / ALIAS, "rev-parse", "--short", "HEAD")
    for step in setup:
        _in_both_copies(world, *step)
    local = _branches(world)
    _mode(world, delegated=True)
    delegated = _branches(world)

    assert local[0] == 200
    assert local[1]["current_branch"] == expected.format(sha=sha)
    assert local[1]["branches"][0] == local[1]["current_branch"]
    assert delegated == local


def test_a_detached_head_missing_from_the_list_is_named_by_its_commit(
    world: SimpleNamespace,
) -> None:
    """Should git-mcp's list ever carry no entry for the detached HEAD, the
    label names the commit rather than inventing a ref."""
    _in_both_copies(world, "checkout", "-q", "--detach", "HEAD")
    sha = _git(world.remote_root / USER / ALIAS, "rev-parse", "--short", "HEAD")
    pod = world.pod_root / USER / ALIAS
    git = _NoDetachedEntry(pod, world.remote_root / USER / ALIAS)
    _mode(world, delegated=True, git=git)
    status, body = _branches(world)

    assert status == 200
    assert body["current_branch"] == f"(HEAD detached at {sha})"
    assert body["branches"] == [body["current_branch"], "feature", "main", "zeta"]


def test_git_mcp_failure_reading_head_is_a_500_not_a_detached_head(
    world: SimpleNamespace,
) -> None:
    pod = world.pod_root / USER / ALIAS
    _mode(
        world,
        delegated=True,
        git=_UnreachableHead(pod, world.remote_root / USER / ALIAS),
    )
    status, body = _branches(world)

    assert status == 500
    assert "connection refused" in body["detail"]


def test_listings_that_disagree_on_a_detached_head_are_a_500(
    world: SimpleNamespace,
) -> None:
    """show-ref reporting no branches while the branch list has several is
    a git-mcp failure (its show-ref swallows errors), never a guess."""
    _in_both_copies(world, "checkout", "-q", "--detach", "HEAD")
    pod = world.pod_root / USER / ALIAS
    _mode(
        world, delegated=True, git=_BrokenShowRef(pod, world.remote_root / USER / ALIAS)
    )
    status, body = _branches(world)

    assert status == 500
    assert "disagree" in body["detail"]


_PRESENCE_GATED_ROUTES = [
    pytest.param("post", "reindex", {"index_types": ["fts"]}, id="reindex"),
    pytest.param("post", "indexes/fts", None, id="add-index"),
    pytest.param("post", "health/check", None, id="health"),
    pytest.param("post", "sync", {"reindex": False}, id="sync"),
    pytest.param("post", "branch", {"branch_name": "feature"}, id="switch"),
]


@pytest.mark.parametrize("method, suffix, body", _PRESENCE_GATED_ROUTES)
def test_every_presence_gated_route_takes_presence_from_the_registry(
    world: SimpleNamespace, method: str, suffix: str, body: Optional[Dict[str, Any]]
) -> None:
    """Each route passes the registered copy's presence check although this
    pod has no directory for it (what happens next is that route's own
    business), and 404s another user's alias."""
    _mode(world, delegated=True)

    # The real job manager pulls in the whole server app; an inert stand-in
    # is enough to reach the presence check.
    world.monkeypatch.setattr(
        routes, "_get_background_job_manager", lambda: SimpleNamespace()
    )
    call = getattr(world.client, method)
    kwargs = {} if body is None else {"json": body}
    mine = call(f"/api/activated-repos/{ALIAS}/{suffix}", **kwargs)
    theirs = call(f"/api/activated-repos/someone-elses/{suffix}", **kwargs)

    not_found = f"Activated repository '{ALIAS}' not found"
    assert mine.json().get("detail") != not_found, mine.json()
    assert theirs.status_code == 404
    assert theirs.json()["detail"] == "Activated repository 'someone-elses' not found"
    assert not world.pod_root.exists()
