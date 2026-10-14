"""GET /api/repositories/{id}/branches: the current branch of a delegated golden
repository comes from git-mcp.

The route used to ask a LOCAL GitTopologyService for the current branch. On a
pod without the repository that found no repository and the response said
"master" whatever git-mcp had checked out. Under ``content_backend: delegated``
a golden repository's branch is now asked of the git seam; a failure to read it
is a 500 with the reason, never "master". Locally nothing changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import code_indexer.server.startup.content_backend_wiring as content_wiring
import code_indexer.server.startup.git_backend_wiring as git_wiring
from code_indexer.server.auth import dependencies
from code_indexer.server.routers import inline_repos_v2, repo_access_http
from code_indexer.server.routers.inline_repos_v2 import (
    _current_branch_name,
    register_repos_v2_routes,
)

SHA = "ab" * 20
ALIAS = "example-repo"


class _GitMcp:
    """git-mcp holding a repository whose checkout is on ``feature/x``."""

    def __init__(self, *, branch: Any = "feature/x", not_a_repo: bool = False) -> None:
        self.branch = branch
        self.not_a_repo = not_a_repo
        self.paths: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.paths.append(repo_path)
        if self.not_a_repo:
            raise subprocess.CalledProcessError(
                128, "git-mcp", stderr="fatal: not a git repository"
            )
        return SHA

    def symbolic_ref(self, repo_path: str, **kw: Any) -> str:
        if isinstance(self.branch, BaseException):
            raise self.branch
        return str(self.branch)


def _delegated(monkeypatch: pytest.MonkeyPatch, backend: Optional[Any]) -> None:
    monkeypatch.setattr(content_wiring, "is_content_seam_delegated", lambda: True)
    monkeypatch.setattr(git_wiring, "server_delegated_git_backend", lambda: backend)


def _forbidden_topology() -> MagicMock:
    local = MagicMock()
    local.get_current_branch.side_effect = AssertionError("local git asked")
    return local


# --- the helper -------------------------------------------------------------


def test_local_server_uses_the_local_topology_unchanged(tmp_path: Path) -> None:
    local = MagicMock()
    local.get_current_branch.return_value = "develop"
    assert _current_branch_name(local, tmp_path, golden=True) == "develop"
    local.get_current_branch.return_value = None
    assert _current_branch_name(local, tmp_path, golden=True) == "master"


def test_delegated_golden_branch_comes_from_git_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = _GitMcp()
    _delegated(monkeypatch, git)
    assert _current_branch_name(_forbidden_topology(), tmp_path, golden=True) == (
        "feature/x"
    )
    assert git.paths == [str(tmp_path)]


def test_delegated_activated_repo_keeps_local_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _delegated(monkeypatch, _GitMcp())
    local = MagicMock()
    local.get_current_branch.return_value = "mine"
    assert _current_branch_name(local, tmp_path, golden=False) == "mine"


def test_delegated_branch_failure_raises_instead_of_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _delegated(monkeypatch, _GitMcp(branch=ConnectionError("git-mcp down")))
    with pytest.raises(RuntimeError, match="current branch"):
        _current_branch_name(_forbidden_topology(), tmp_path, golden=True)


def test_delegated_non_repository_keeps_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _delegated(monkeypatch, _GitMcp(not_a_repo=True))
    assert _current_branch_name(_forbidden_topology(), tmp_path, golden=True) == (
        "master"
    )


def test_delegated_without_git_mcp_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _delegated(monkeypatch, None)
    with pytest.raises(RuntimeError, match="git_backend is 'local'"):
        _current_branch_name(_forbidden_topology(), tmp_path, golden=True)


def test_snapshot_path_is_mapped_to_the_golden_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = _GitMcp()
    _delegated(monkeypatch, git)
    base = tmp_path / "golden-repos" / ALIAS
    monkeypatch.setattr(
        content_wiring, "golden_repo_content_root", lambda path, consumer: base
    )
    _current_branch_name(_forbidden_topology(), tmp_path / "snap", golden=True)
    assert git.paths == [str(base)]


# --- the route --------------------------------------------------------------


class _NoBranches:
    """BranchService stand-in: the branch LIST has its own tests (WP3); these
    pin only the current branch the route reports."""

    def __init__(self, **kw: Any) -> None:
        pass

    def __enter__(self) -> "_NoBranches":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def list_branches(self, include_remote: bool = False) -> List[Any]:
        return []


def _client(clone_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(repo_access_http, "enforce_repo_access", lambda *a: None)
    monkeypatch.setattr(inline_repos_v2, "BranchService", _NoBranches)
    listing = MagicMock()
    listing.get_golden_clone_path.return_value = str(clone_path)
    # The endpoint needs only the path: no statistics, no manifest listing.
    listing.get_repository_details.side_effect = AssertionError(
        "branches route computed the repository details"
    )
    app = FastAPI()
    register_repos_v2_routes(
        app,
        activated_repo_manager=None,
        repository_listing_manager=listing,
        background_job_manager=None,
    )
    user = SimpleNamespace(username="example_admin", role="admin")
    app.dependency_overrides[dependencies.get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_route_reports_git_mcp_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "golden-repos" / "route-ok"
    clone.mkdir(parents=True)
    _delegated(monkeypatch, _GitMcp())
    resp = _client(clone, monkeypatch).get(f"/api/repositories/{ALIAS}/branches")
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_branch"] == "feature/x"


def test_route_surfaces_a_branch_failure_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "golden-repos" / "route-fail"
    clone.mkdir(parents=True)
    _delegated(monkeypatch, _GitMcp(branch=ConnectionError("git-mcp down")))
    resp = _client(clone, monkeypatch).get(f"/api/repositories/{ALIAS}/branches")
    assert resp.status_code == 500
    assert "current branch" in resp.json()["detail"]


def test_helper_is_what_the_route_uses() -> None:
    import inspect

    src = inspect.getsource(inline_repos_v2.register_repos_v2_routes)
    assert "_current_branch_name(" in src
    assert '.get_current_branch() or "master"' not in src
