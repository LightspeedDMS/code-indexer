"""Shared front-door environment for the repository-URL userinfo tests.

A real app (``create_app`` via ``isolated_app``, never ~/.cidx-server) with
the pieces the lifespan would attach (backend registry, group access
manager, access filtering service) built the way the lifespan builds them.

One golden repository is registered, and globally activated, with a URL
carrying userinfo. Its clone is laid out exactly as ``git clone <url>``
leaves it: a real ``origin`` remote whose URL carries the same userinfo in
``.git/config``. A user's activation (``activate_for_user``) is made by the
real ActivatedRepoManager, which copies that origin into the activation.

A test module imports the fixtures it uses (``app``, ``client``); each
importing module gets its own app.

Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.storage.factory import StorageFactory
from tests.unit.server._isolated_app import isolated_app

SECRET = "example-token-123"
URL_USER = "example-user"
USERINFO_URL = (
    f"https://{URL_USER}:{SECRET}@git.example.com:8443/example/userinfo-repo.git"
)
REDACTED_URL = "https://***@git.example.com:8443/example/userinfo-repo.git"
REPO = "userinfo-repo"
GLOBAL_ALIAS = f"{REPO}-global"

ADMIN = "example_admin"
USER = "example_user"
PASSWORD = "Example-Redaction-Passw0rd!"
USER_ACTIVATION = "my-userinfo-repo"


# Committed symlinks whose RESOLVED location is inside the repository's .git
# (names carry no ``.git`` segment), and one legitimate in-repo symlink.
GIT_FILE_LINKS = ("link.py", "link.md", "link.txt", "linknoext")
GIT_DIR_LINK = "gitdir"
OK_LINK = "ok.md"
OK_PY_LINK = "ok.py"


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def init_cloned_repo(path: Path, origin_url: str) -> None:
    """A real one-commit repository on 'main' whose ``origin`` remote is
    *origin_url*, as ``git clone <origin_url>`` leaves it, with the
    committed symlinks above."""
    path.mkdir(parents=True)
    (path / "README.md").write_text("example\n")
    for name in GIT_FILE_LINKS:
        (path / name).symlink_to(".git/config")
    (path / GIT_DIR_LINK).symlink_to(".git")
    (path / OK_LINK).symlink_to("README.md")
    (path / "main.py").write_text("example = 1\n")
    (path / OK_PY_LINK).symlink_to("main.py")
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "add",
        "README.md",
        "main.py",
        *GIT_FILE_LINKS,
        GIT_DIR_LINK,
        OK_LINK,
        OK_PY_LINK,
    )
    _git(
        path,
        "-c",
        "user.name=example",
        "-c",
        "user.email=example@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )
    _git(path, "remote", "add", "origin", origin_url)


def _wire_lifespan_state(app: Any, server_dir: Path) -> ThreadPoolExecutor:
    """Attach what the lifespan attaches (TestClient(app) never runs it),
    including the dedicated X-Ray executor and cell limiter; returns the
    executor for shutdown."""
    from code_indexer.server.mcp.handlers.xray import (
        set_xray_cell_limiter,
        set_xray_executor,
    )
    from code_indexer.server.routers.groups import set_group_manager
    from code_indexer.server.services.resizable_limiter import ResizableLimiter

    xray_executor = ThreadPoolExecutor(max_workers=2)
    app.state.xray_executor = xray_executor
    set_xray_executor(xray_executor)
    limiter = ResizableLimiter(initial=4, k_min=1, k_max=50)
    app.state.xray_cell_limiter = limiter
    set_xray_cell_limiter(limiter)

    registry = StorageFactory.create_backends(
        config={"storage_mode": "sqlite"}, data_dir=str(app.state.data_dir)
    )
    app.state.backend_registry = registry
    app.state.golden_repos_dir = app.state.golden_repo_manager.golden_repos_dir
    group_manager = GroupAccessManager(
        server_dir / "groups.db", storage_backend=registry.groups
    )
    set_group_manager(group_manager)
    app.state.group_manager = group_manager
    app.state.access_filtering_service = AccessFilteringService(
        group_manager, activated_repo_manager=app.state.activated_repo_manager
    )
    return xray_executor


def _seed_repository(app: Any) -> Path:
    """Register REPO (golden + global) with USERINFO_URL as its URL."""
    golden_manager = app.state.golden_repo_manager
    golden_dir = Path(golden_manager.golden_repos_dir)
    clone_path = golden_dir / REPO
    init_cloned_repo(clone_path, USERINFO_URL)
    golden_manager._sqlite_backend.add_repo(
        alias=REPO,
        repo_url=USERINFO_URL,
        default_branch="main",
        clone_path=str(clone_path),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
    )
    app.state.backend_registry.global_repos.register_repo(
        alias_name=GLOBAL_ALIAS,
        repo_name=REPO,
        repo_url=USERINFO_URL,
        index_path=str(clone_path),
    )
    AliasManager(str(golden_dir / "aliases")).create_alias(
        GLOBAL_ALIAS, str(clone_path), repo_name=REPO
    )
    return clone_path


def _grant(app: Any) -> None:
    """USER's group is granted REPO; ADMIN is in the admins group."""
    groups: GroupAccessManager = app.state.group_manager
    group = groups.create_group("restricted", "example group")
    groups.assign_user_to_group(USER, group.id, assigned_by="test")
    groups.grant_repo_access(REPO, group.id, granted_by="test")
    admins = groups.get_group_by_name("admins")
    assert admins is not None
    groups.assign_user_to_group(ADMIN, admins.id, assigned_by="test")


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    root = tmp_path_factory.mktemp("repo-url-userinfo-app")
    with isolated_app(root) as real_app:
        accounts = real_app.state.user_manager
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        accounts.create_user(USER, PASSWORD, UserRole.NORMAL_USER)
        xray_executor = _wire_lifespan_state(real_app, root / "server")
        try:
            _seed_repository(real_app)
            _grant(real_app)
            yield real_app
        finally:
            xray_executor.shutdown(wait=True)


@pytest.fixture(scope="module")
def client(app: Any) -> TestClient:
    return TestClient(app, follow_redirects=False)


def golden_clone_path(app: Any) -> Path:
    return Path(app.state.golden_repo_manager.golden_repos_dir) / REPO


def store_userinfo_origin(repo: Path) -> None:
    """Make *repo*'s stored origin the URL with userinfo, as a clone made
    before credentials were supplied at run time still holds it."""
    _git(repo, "config", "remote.origin.url", USERINFO_URL)
    assert SECRET in (repo / ".git" / "config").read_text()


def bearer(app: Any, username: str) -> Dict[str, str]:
    account = app.state.user_manager.get_user(username)
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    return {"Authorization": f"Bearer {token}"}


def get(
    client: TestClient, app: Any, username: str, path: str, **kwargs: Any
) -> Response:
    client.cookies.clear()
    return client.get(path, headers=bearer(app, username), **kwargs)


def assert_no_userinfo(body: str) -> None:
    """Neither the secret nor the userinfo's username appears anywhere."""
    assert SECRET not in body, body
    assert f"{URL_USER}:" not in body, body
    assert f"{URL_USER}@" not in body, body


def mcp_call(
    client: TestClient, app: Any, username: str, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """One ``tools/call`` through ``POST /mcp``; the JSON-RPC body."""
    client.cookies.clear()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=bearer(app, username),
    )
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def mcp_text(
    client: TestClient, app: Any, username: str, tool: str, arguments: Dict[str, Any]
) -> str:
    """The tool's text payload; the whole response carries no userinfo."""
    body = mcp_call(client, app, username, tool, arguments)
    import json

    assert_no_userinfo(json.dumps(body))
    assert "error" not in body, body
    text: str = body["result"]["content"][0]["text"]
    return text


def activate_for_user(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
    user_alias: str = USER_ACTIVATION,
    username: str = USER,
) -> Path:
    """Activate REPO for *username* as *user_alias* through the app's real
    ActivatedRepoManager, with the clone backend wired the way the lifespan
    wires it; returns the activation's path."""
    from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

    manager = app.state.activated_repo_manager
    monkeypatch.setattr(manager, "_clone_backend", LocalCloneBackend())
    if not any(
        r["user_alias"] == user_alias
        for r in manager.list_activated_repositories(username)
    ):
        job_id = manager.activate_repository(
            username=username, golden_repo_alias=REPO, user_alias=user_alias
        )
        _wait_for_job(app, job_id, username)
    return Path(manager.get_activated_repo_path(username, user_alias))


def _wait_for_job(app: Any, job_id: str, username: str) -> None:
    jobs = app.state.background_job_manager
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = jobs.get_job_status(job_id, username)
        if status and status["status"] in ("completed", "failed", "cancelled"):
            assert status["status"] == "completed", status
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within 60s")
