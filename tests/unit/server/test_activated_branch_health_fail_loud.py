"""An activated copy's branch and health on a mount-less pod: fail loud.

Under ``content_backend: delegated`` git runs in git-mcp, which reports a
transport or tool failure as ``CalledProcessError`` -- the same type a local
detached HEAD raises. ``ActivatedRepoManager.get_current_branch`` used to read
the registry's branch for ANY failure, and ``GET /api/repos/{alias}`` printed
``"unknown"`` / no branches, so a dead git-mcp looked like a healthy copy.

Now, delegated: a detached HEAD still reads the registry's branch (as locally);
any other failure raises, and the route answers 500. Local is unchanged and
pinned here.

``?health=true`` reads only the copy's index state (``.code-indexer`` on this
pod): proven against a pod path whose working tree does not exist.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
    GitOperationError,
)
from code_indexer.server.routers.inline_repos import (
    _check_filesystem_backend_health,
    register_repo_routes,
)
from code_indexer.server.startup import content_backend_wiring

from tests.unit.server.activated_seam_fakes import (
    ALIAS,
    INDEX_DIR,
    USER,
    ActivatedWorld,
    _git,
    install_local_io_guards,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ActivatedWorld:
    return ActivatedWorld(tmp_path, monkeypatch)


def _activate(manager: ActivatedRepoManager, branch: str = "main") -> None:
    manager._do_activate_repository(
        username=USER, golden_repo_alias=ALIAS, branch_name=branch, user_alias=ALIAS
    )


class _FailingGit:
    """A git backend whose ``current_branch`` fails as told, else delegates."""

    def __init__(self, inner: Any, error: Exception) -> None:
        self._inner = inner
        self._error = error

    def current_branch(self, repo_path: str, timeout: Any = None) -> str:
        raise self._error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _git_mcp_down() -> subprocess.CalledProcessError:
    """What the git-mcp plugin raises for a transport failure."""
    return subprocess.CalledProcessError(
        1, "git-mcp", stderr="git-mcp git_symbolic_ref request failed: refused"
    )


def _detached() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        128, "git", stderr="fatal: ref HEAD is not a symbolic ref"
    )


def _pair(world: ActivatedWorld, branch: str = "main") -> Any:
    """Both deployments activated; the delegated one is in force on return."""
    local = world.local_manager()
    _activate(local, branch)
    delegated = world.delegated_manager()
    with world.forbid_local_io():
        _activate(delegated, branch)
    return local, delegated


def _local_branch(world: ActivatedWorld, local: ActivatedRepoManager) -> str:
    """``local.get_current_branch`` with the local content backend in force."""
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(content_backend_wiring, "server_content_backend", lambda: None)
        return local.get_current_branch(USER, ALIAS)


# ---------------------------------------------------------------------------
# ActivatedRepoManager.get_current_branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["main", "feature"])
def test_branch_matches_local(world: ActivatedWorld, branch: str) -> None:
    local, delegated = _pair(world, branch)
    expected = _local_branch(world, local)
    with world.forbid_local_io():
        assert delegated.get_current_branch(USER, ALIAS) == expected == branch


def test_detached_head_reads_the_registry_branch_in_both(
    world: ActivatedWorld,
) -> None:
    local, delegated = _pair(world, "feature")
    for copy in (world.local_copy(), world.remote_copy()):
        _git(copy, "checkout", "-q", "--detach")
    expected = _local_branch(world, local)
    with world.forbid_local_io():
        assert delegated.get_current_branch(USER, ALIAS) == expected == "feature"


@pytest.mark.parametrize(
    "error",
    [_git_mcp_down(), RuntimeError("plugin bug"), ConnectionError("reset")],
    ids=["git-mcp-error", "runtime-error", "connection-error"],
)
def test_delegated_git_failure_raises(
    world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _, delegated = _pair(world)
    monkeypatch.setattr(
        delegated, "_git_backend", _FailingGit(delegated._git_backend, error)
    )
    with world.forbid_local_io(), pytest.raises(GitOperationError) as raised:
        delegated.get_current_branch(USER, ALIAS)
    assert raised.value.__cause__ is error


def test_delegated_detached_head_from_git_mcp_reads_the_registry(
    world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, delegated = _pair(world, "feature")
    monkeypatch.setattr(
        delegated, "_git_backend", _FailingGit(delegated._git_backend, _detached())
    )
    with world.forbid_local_io():
        assert delegated.get_current_branch(USER, ALIAS) == "feature"


@pytest.mark.parametrize(
    "error",
    [_git_mcp_down(), _detached(), RuntimeError("plugin bug")],
    ids=["called-process-error", "detached", "runtime-error"],
)
def test_local_default_still_reads_the_registry_branch(
    world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Pins today's local behaviour: any failure -> the registry's branch."""
    local = world.local_manager()
    _activate(local, "feature")
    monkeypatch.setattr(local, "_git_backend", _FailingGit(local._git_backend, error))
    assert local.get_current_branch(USER, ALIAS) == "feature"


def test_local_timeout_is_still_a_git_error(
    world: ActivatedWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = world.local_manager()
    _activate(local)
    timeout = subprocess.TimeoutExpired("git", 30)
    monkeypatch.setattr(local, "_git_backend", _FailingGit(local._git_backend, timeout))
    with pytest.raises(GitOperationError, match="timed out"):
        local.get_current_branch(USER, ALIAS)


# ---------------------------------------------------------------------------
# GET /api/repos/{alias}: branch and branches
# ---------------------------------------------------------------------------


class _NonLocal:
    is_local = False


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    delegated: bool,
    branch_error: Optional[Exception] = None,
    branches_error: Optional[Exception] = None,
) -> TestClient:
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        (lambda: _NonLocal()) if delegated else (lambda: None),
    )
    manager = Mock()
    manager.list_activated_repositories.return_value = [
        {"user_alias": ALIAS, "git_url": "u", "current_branch": "main"}
    ]
    # A path with nothing at it: the storage walk is skipped locally.
    manager.get_activated_repo_path.return_value = str(tmp_path / "absent")
    if branch_error is not None:
        manager.get_current_branch.side_effect = branch_error
    else:
        manager.get_current_branch.return_value = "main"
    if branches_error is not None:
        manager.list_repository_branches.side_effect = branches_error
    else:
        manager.list_repository_branches.return_value = {"branches": ["main"]}
    app = FastAPI()
    register_repo_routes(
        app,
        activated_repo_manager=manager,
        golden_repo_manager=Mock(),
        repository_listing_manager=Mock(),
        background_job_manager=Mock(),
    )
    user = Mock()
    user.username = USER
    app.dependency_overrides[dependencies.get_current_user] = lambda: user
    return TestClient(app)


def _info(client: TestClient, query: str = "") -> Any:
    return client.get(f"/api/repos/{ALIAS}{query}")


def test_route_branch_failure_is_500_under_delegated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(
        monkeypatch,
        tmp_path,
        delegated=True,
        branch_error=GitOperationError("Failed to get current branch: refused"),
    )
    response = _info(client)
    assert response.status_code == 500
    assert "refused" in response.json()["detail"]


def test_route_branch_failure_is_unknown_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(
        monkeypatch,
        tmp_path,
        delegated=False,
        branch_error=GitOperationError("Failed to get current branch: refused"),
    )
    response = _info(client)
    assert response.status_code == 200
    assert response.json()["current_branch"] == "unknown"


def test_route_branches_failure_is_500_under_delegated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(
        monkeypatch,
        tmp_path,
        delegated=True,
        branches_error=GitOperationError("git-mcp git_branch_list failed"),
    )
    response = _info(client, "?branches=true")
    assert response.status_code == 500
    assert "git_branch_list" in response.json()["detail"]


def test_route_branches_failure_is_empty_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(
        monkeypatch,
        tmp_path,
        delegated=False,
        branches_error=GitOperationError("boom"),
    )
    response = _info(client, "?branches=true")
    assert response.status_code == 200
    assert response.json()["branches"] == []


@pytest.mark.parametrize("delegated", [False, True])
def test_route_branches_parity_when_git_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delegated: bool
) -> None:
    client = _client(monkeypatch, tmp_path, delegated=delegated)
    body = _info(client, "?branches=true").json()
    assert body["current_branch"] == "main"
    assert [b["name"] for b in body["branches"]] == ["main"]


# ---------------------------------------------------------------------------
# ?health=true: the copy's index state only
# ---------------------------------------------------------------------------


def _write_config(copy: Path, provider: str) -> None:
    index = copy / INDEX_DIR
    index.mkdir(parents=True)
    (index / "config.json").write_text(
        json.dumps({"codebase_dir": str(copy), "vector_store": {"provider": provider}})
    )


def _guard_copy(copy: Path, violations: List[str]) -> pytest.MonkeyPatch:
    """Any local access to *copy* outside its index state raises (and is
    recorded in *violations*)."""
    root = str(copy)

    def forbidden(value: Any) -> bool:
        try:
            text = str(Path(value).absolute())
        except TypeError:
            return False
        if not (text == root or text.startswith(root + "/")):
            return False
        rel = Path(text).relative_to(root).parts
        if rel and rel[0] == INDEX_DIR:
            return False
        violations.append(text)
        return True

    patcher = pytest.MonkeyPatch()
    install_local_io_guards(patcher, forbidden)
    return patcher


def _health(monkeypatch: pytest.MonkeyPatch, copy: Path, *, delegated: bool) -> Any:
    """The health fields for *copy*; delegated, any local access to the copy
    outside its index state raises -- and a raise would read as an issue, so
    the guard's own record is checked too."""
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        (lambda: _NonLocal()) if delegated else (lambda: None),
    )
    if not delegated:
        return _check_filesystem_backend_health(str(copy))
    violations: List[str] = []
    patcher = _guard_copy(copy, violations)
    try:
        return _check_filesystem_backend_health(str(copy))
    finally:
        patcher.undo()
        assert violations == []


def test_health_with_a_config_for_another_directory_is_an_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pod_copy = tmp_path / "pod" / "copy"
    _write_config(pod_copy, "filesystem")
    config = pod_copy / INDEX_DIR / "config.json"
    config.write_text(
        json.dumps(
            {"codebase_dir": "/elsewhere", "vector_store": {"provider": "filesystem"}}
        )
    )
    actual = _health(monkeypatch, pod_copy, delegated=True)
    assert actual["issues"] == ["Unable to determine container status"]
    assert actual["query_ready"] is False


@pytest.mark.parametrize("provider", ["filesystem", "other"])
def test_health_reads_only_index_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    local_copy = tmp_path / "local" / "copy"
    local_copy.mkdir(parents=True)
    (local_copy / "README.md").write_text("x\n")
    _write_config(local_copy, provider)
    pod_copy = tmp_path / "pod" / "copy"  # index state only, no working tree
    _write_config(pod_copy, provider)

    expected = _health(monkeypatch, local_copy, delegated=False)
    actual = _health(monkeypatch, pod_copy, delegated=True)
    assert actual == expected
    assert actual["query_ready"] is (provider == "filesystem")


def test_health_without_index_state_reports_an_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_copy = tmp_path / "local" / "copy"
    local_copy.mkdir(parents=True)
    pod_copy = tmp_path / "pod" / "copy"  # nothing on this pod at all
    expected = _health(monkeypatch, local_copy, delegated=False)
    actual = _health(monkeypatch, pod_copy, delegated=True)
    assert actual == expected
    assert actual["issues"] == ["Unable to determine container status"]
    assert actual["query_ready"] is False
