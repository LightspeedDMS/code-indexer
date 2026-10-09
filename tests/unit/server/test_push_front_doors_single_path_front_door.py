# ruff: noqa: F811
"""Both push front doors -- REST ``POST .../git/push`` (the registered
repository credential) and MCP ``git_push`` (the user's PAT) -- push through
ONE implementation, ``GitOperationsService.git_push``.

Driven through a real app (repo_url_userinfo_env): ADMIN's activation has
origin = the registered https URL; the activation's own
``url.<bare>.pushInsteadOf`` delivers every push to a real local bare
repository, so git really pushes and really records tracking. The PAT is
seeded in the real credential store (no forge call). Hosts, usernames and
secrets are neutral placeholders.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    ADMIN,
    SECRET,
    activate_for_user,
    app,
    bearer,
    client,
    mcp_call,
    mcp_text,
)

ACTIVATION = "admin-push-front-doors"
PAT = "example-pat-321"
GitRun = Tuple[List[str], Dict[str, str]]


def _git(args: List[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _upstream(repo: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{u}}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


@pytest.fixture(scope="module")
def bare(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("push-front-doors") / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tuple[str, Path]]:
    """(url, repository): a remote served over http requiring the PAT (as
    username and password) -- the MCP front door's forge."""
    from tests.unit.server.git.auth_http_git_server import served_bare_remote

    root = tmp_path_factory.mktemp("push-front-doors-served")
    with served_bare_remote(root, PAT, PAT) as url:
        yield url, root / "served" / "remote.git"


@pytest.fixture
def activation(
    app: Any, monkeypatch: pytest.MonkeyPatch, bare: Path, served: Tuple[str, Path]
) -> Path:
    """ADMIN's activation: origin (the registered URL's remote) pushes to a
    local bare repository; remote ``pat`` is the PAT's forge."""
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    repo = activate_for_user(app, monkeypatch, ACTIVATION, username=ADMIN)
    origin = _git(["config", "--get", "remote.origin.url"], repo)
    _git(["config", "--replace-all", f"url.{bare}.pushInsteadOf", origin], repo)
    remotes = _git(["remote"], repo).split()
    verb = "set-url" if "pat" in remotes else "add"
    _git(["remote", verb, "pat", served[0]], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    return repo


@pytest.fixture
def pat(app: Any, activation: Path) -> str:
    """ADMIN's PAT for the ``pat`` remote's forge host, in the real store."""
    from code_indexer.server.mcp.handlers.git_write import _get_credential_manager
    from code_indexer.server.services.git_credential_helper import (
        GitCredentialHelper,
    )

    forge = _git(["config", "--get", "remote.pat.url"], activation)
    host = GitCredentialHelper.extract_host_from_remote_url(forge)
    assert host is not None, forge
    manager = _get_credential_manager()
    manager._backend.upsert_credential(
        credential_id=str(uuid.uuid4()),
        username=ADMIN,
        forge_type="github",
        forge_host=host,
        encrypted_token=manager._encrypt_token(PAT),
    )
    return PAT


@pytest.fixture
def git_runs(monkeypatch: pytest.MonkeyPatch) -> List[GitRun]:
    """argv and env of every git subprocess; every one runs for real."""
    runs: List[GitRun] = []
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else []
        if argv[:1] == ["git"]:
            runs.append((argv, dict(kwargs.get("env") or {})))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return runs


@pytest.fixture
def push_calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Every call of the single push implementation (the real method runs)."""
    calls: List[Dict[str, Any]] = []
    real_git_push = git_operations_service.git_push

    def spy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls.append(kwargs)
        result: Dict[str, Any] = real_git_push(*args, **kwargs)
        return result

    monkeypatch.setattr(git_operations_service, "git_push", spy)
    return calls


def _push(
    front_door: str,
    client: TestClient,
    app: Any,
    branch: Optional[str],
    set_upstream: bool,
) -> str:
    """Push through the front door; the response body as text."""
    # REST: the registered repository's origin; MCP: the PAT's forge remote.
    remote = "origin" if front_door == "rest" else "pat"
    arguments: Dict[str, Any] = {"remote": remote, "set_upstream": set_upstream}
    if branch:
        arguments["branch"] = branch
    if front_door == "rest":
        client.cookies.clear()
        response = client.post(
            f"/api/v1/repos/{ACTIVATION}/git/push",
            json=arguments,
            headers=bearer(app, ADMIN),
        )
        assert response.status_code == 200, response.text
        return response.text
    text = mcp_text(
        client, app, ADMIN, "git_push", {"repository_alias": ACTIVATION, **arguments}
    )
    assert json.loads(text)["success"] is True, text
    return text


@pytest.mark.parametrize("front_door", ["rest", "mcp"])
@pytest.mark.parametrize("set_upstream", [True, False])
@pytest.mark.parametrize("named", [True, False])
def test_push_runs_through_the_single_implementation(
    client: TestClient,
    app: Any,
    activation: Path,
    bare: Path,
    served: Tuple[str, Path],
    pat: str,
    git_runs: List[GitRun],
    push_calls: List[Dict[str, Any]],
    front_door: str,
    set_upstream: bool,
    named: bool,
) -> None:
    """REST pushes origin, whose destination is a local repository, not the
    registered credential's host: the credential is withheld. MCP pushes the
    PAT's own forge: a real authenticated push carrying the PAT at run
    time."""
    remote = "origin" if front_door == "rest" else "pat"
    destination = bare if front_door == "rest" else served[1]
    branch = f"{front_door}-{set_upstream}-{named}-{uuid.uuid4().hex[:6]}".lower()
    _git(["checkout", "-q", "-b", branch], activation)
    (activation / f"{branch}.txt").write_text(branch)
    _git(["add", f"{branch}.txt"], activation)
    _git(["commit", "-q", "-m", branch], activation)
    if front_door == "rest" and not named:
        # A REST push naming no branch goes to the branch's own upstream.
        _git(["push", "-q", "--set-upstream", "origin", branch], activation)
        git_runs.clear()
    expected_upstream = (
        f"{remote}/{branch}"
        if set_upstream or (front_door == "rest" and not named)
        else ""
    )

    body = _push(front_door, client, app, branch if named else None, set_upstream)

    assert len(push_calls) == 1
    assert _git(["rev-parse", branch], destination) == _git(
        ["rev-parse", "HEAD"], activation
    )
    assert _upstream(activation, branch) == expected_upstream
    credential = SECRET if front_door == "rest" else pat
    supplied = None if front_door == "rest" else pat
    pushes = [env for argv, env in git_runs if argv[1:2] == ["push"]]
    assert [env.get("CIDX_GIT_REMOTE_PASSWORD") for env in pushes] == [supplied]
    for argv, _env in git_runs:
        assert not any(credential in part for part in argv), argv
    assert credential not in (activation / ".git" / "config").read_text()
    assert credential not in body


def test_mcp_push_tracks_by_default(
    client: TestClient,
    app: Any,
    activation: Path,
    served: Tuple[str, Path],
    pat: str,
    push_calls: List[Dict[str, Any]],
) -> None:
    branch = f"mcp-default-{uuid.uuid4().hex[:6]}"
    _git(["checkout", "-q", "-b", branch], activation)

    text = mcp_text(
        client,
        app,
        ADMIN,
        "git_push",
        {"repository_alias": ACTIVATION, "remote": "pat"},
    )

    assert json.loads(text)["success"] is True, text
    assert push_calls[0]["set_upstream"] is True
    assert _git(["rev-parse", branch], served[1]) == _git(
        ["rev-parse", "HEAD"], activation
    )
    assert _upstream(activation, branch) == f"pat/{branch}"


def _echoing_hook(repo: Path, glued: bool = False) -> Path:
    """A pre-push hook that echoes the credential from its environment to
    stderr (glued to text when ``glued``) and fails the push -- any
    credential git output carries."""
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    echoed = (
        "xx${CIDX_GIT_REMOTE_PASSWORD}yy" if glued else "${CIDX_GIT_REMOTE_PASSWORD}"
    )
    hook.write_text(f'#!/bin/sh\necho "hook-saw:{echoed}" >&2\nexit 1\n')
    hook.chmod(0o700)
    return hook


@pytest.mark.parametrize(
    "case, credential",
    [("plain", PAT), ("glued", PAT), ("short", "abc")],
)
def test_rest_push_hook_echo_never_reaches_response_or_logs(
    client: TestClient,
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    case: str,
    credential: str,
) -> None:
    """REST: a repository registered with a credential in its URL supplies
    it at run time to its own host; git echoing it -- whole, glued to text,
    or however short -- never reaches the response or the logs."""
    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
    from tests.unit.server.git.auth_http_git_server import served_bare_remote
    from tests.unit.server.repo_url_userinfo_env import (
        _wait_for_job,
        init_cloned_repo,
    )

    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    glued = case == "glued"
    alias = f"push-hook-{case}-{uuid.uuid4().hex[:6]}"
    user_alias = f"admin-{alias}"
    golden = app.state.golden_repo_manager
    manager = app.state.activated_repo_manager
    monkeypatch.setattr(manager, "_clone_backend", LocalCloneBackend())
    with served_bare_remote(tmp_path, credential, credential) as url:
        registered = http_credentials_url(url, credential, credential)
        assert registered is not None
        clone_path = Path(golden.golden_repos_dir) / alias
        init_cloned_repo(clone_path, registered)
        golden._sqlite_backend.add_repo(
            alias=alias,
            repo_url=registered,
            default_branch="main",
            clone_path=str(clone_path),
            created_at="2024-01-01T00:00:00+00:00",
        )
        job_id = manager.activate_repository(
            username=ADMIN, golden_repo_alias=alias, user_alias=user_alias
        )
        _wait_for_job(app, job_id, ADMIN)
        repo = Path(manager.get_activated_repo_path(ADMIN, user_alias))
        _echoing_hook(repo, glued=glued)
        client.cookies.clear()

        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            response = client.post(
                f"/api/v1/repos/{user_alias}/git/push",
                json={
                    "remote": "origin",
                    "branch": f"main:refs/heads/h-{uuid.uuid4().hex[:6]}",
                },
                headers=bearer(app, ADMIN),
            )

    echoed = f"xx{credential}yy" if glued else credential
    # A token holding the credential is masked whole, glued text included.
    masked = "hook-saw:***"
    assert response.status_code != 200
    assert (f"hook-saw:{echoed}" in response.text) is False
    assert (f"hook-saw:{echoed}" in caplog.text) is False
    assert masked in response.text
    assert masked in caplog.text


def test_mcp_push_hook_echo_never_reaches_result_or_logs(
    client: TestClient,
    app: Any,
    activation: Path,
    pat: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook = _echoing_hook(activation)
    try:
        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            body = mcp_call(
                client,
                app,
                ADMIN,
                "git_push",
                {
                    "repository_alias": ACTIVATION,
                    "remote": "pat",
                    "branch": f"h-{uuid.uuid4().hex[:6]}",
                },
            )
    finally:
        hook.unlink()

    result = json.dumps(body)
    assert "hook-saw:" in result
    assert "hook-saw:" in caplog.text
    assert (pat in result) is False
    assert (pat in caplog.text) is False
