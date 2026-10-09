# ruff: noqa: F811
"""Activated repositories: repository credentials are supplied to git at
run time and never stored in a clone's configuration.

Driven through a real app (repo_url_userinfo_env): the golden repository is
registered with a URL carrying userinfo, and its base clone still stores
that URL (an existing clone). The sample host is not reachable, so the
network git subcommands (fetch/pull/push/clone/ls-remote) are intercepted
-- their argv and environment are recorded and they report success --
while every local git command runs for real. Hosts, usernames and secrets
are neutral placeholders.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from fastapi.testclient import TestClient

from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    ADMIN,
    REPO,
    SECRET,
    USER,
    USER_ACTIVATION,
    store_userinfo_origin,
    activate_for_user,
    app,
    assert_no_userinfo,
    bearer,
    client,
    mcp_call,
)

_NETWORK_SUBCOMMANDS = {"fetch", "pull", "push", "clone", "ls-remote"}
_PASSWORD_VAR = "CIDX_GIT_REMOTE_PASSWORD"


@pytest.fixture
def network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Tuple[List[str], Dict[str, str]]]:
    calls: List[Tuple[List[str], Dict[str, str]]] = []
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else []
        if argv[:1] == ["git"] and _NETWORK_SUBCOMMANDS & set(argv[1:4]):
            calls.append((argv, dict(kwargs.get("env") or {})))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.fixture
def activation(app: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    return activate_for_user(app, monkeypatch)


def _origin(repo: Path) -> str:
    return subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


_store_userinfo_origin = store_userinfo_origin


def _assert_credentials_only_at_run_time(
    calls: List[Tuple[List[str], Dict[str, str]]], repo: Path
) -> None:
    assert calls, "expected a network git call"
    for argv, env in calls:
        assert not any(SECRET in part for part in argv), argv
        assert env.get(_PASSWORD_VAR) == SECRET, sorted(env)
    assert SECRET not in (repo / ".git" / "config").read_text()


def test_activation_stores_no_credentials(activation: Path) -> None:
    config = (activation / ".git" / "config").read_text()
    assert SECRET not in config, config
    assert (
        _origin(activation) == "https://git.example.com:8443/example/userinfo-repo.git"
    )


ADMIN_ACTIVATION = "admin-userinfo-repo"


@pytest.fixture
def admin_activation(app: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An activation owned by ADMIN, who holds repository:write."""
    return activate_for_user(app, monkeypatch, ADMIN_ACTIVATION, username=ADMIN)


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("git_fetch", {"repository_alias": ADMIN_ACTIVATION}),
        ("git_pull", {"repository_alias": ADMIN_ACTIVATION}),
        ("switch_branch", {"user_alias": ADMIN_ACTIVATION, "branch_name": "main"}),
    ],
)
def test_network_tools_supply_credentials_at_run_time(
    client: TestClient,
    app: Any,
    admin_activation: Path,
    network_calls: List[Tuple[List[str], Dict[str, str]]],
    tool: str,
    arguments: Dict[str, Any],
) -> None:
    _store_userinfo_origin(admin_activation)

    body = mcp_call(client, app, ADMIN, tool, dict(arguments))

    assert SECRET not in str(body)
    assert "error" not in body, body
    _assert_credentials_only_at_run_time(network_calls, admin_activation)


def test_mcp_git_push_rewrites_stored_credentials_before_resolving_the_remote(
    client: TestClient,
    app: Any,
    admin_activation: Path,
    network_calls: List[Tuple[List[str], Dict[str, str]]],
) -> None:
    """The PAT push resolves the remote URL from the clone; the stored origin
    is credential-free before that. (No PAT is configured here, so the push
    itself is refused.)"""
    _store_userinfo_origin(admin_activation)

    body = mcp_call(
        client, app, ADMIN, "git_push", {"repository_alias": ADMIN_ACTIVATION}
    )

    assert SECRET not in str(body)
    assert SECRET not in (admin_activation / ".git" / "config").read_text()
    for argv, _env in network_calls:
        assert not any(SECRET in part for part in argv), argv


def test_rest_git_push_answers_200_and_supplies_credentials_at_run_time(
    client: TestClient,
    app: Any,
    admin_activation: Path,
    network_calls: List[Tuple[List[str], Dict[str, str]]],
) -> None:
    """REST push through the real ActivatedRepoManager: a successful push
    answers 200 with the documented GitPushResponse body, the response
    carries no userinfo, and the registered URL's credentials reach git
    only through the environment (never argv, never the clone config)."""
    _store_userinfo_origin(admin_activation)
    client.cookies.clear()

    response = client.post(
        f"/api/v1/repos/{ADMIN_ACTIVATION}/git/push",
        json={"remote": "origin", "branch": "main"},
        headers=bearer(app, ADMIN),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "remote": "origin",
        "branch": "main",
        "commits_pushed": 0,
    }
    assert_no_userinfo(response.text)
    assert [argv[1] for argv, _env in network_calls] == ["push"]
    assert "--set-upstream" in network_calls[0][0]  # set_upstream defaults on
    _assert_credentials_only_at_run_time(network_calls, admin_activation)


def test_rest_git_push_without_branch_answers_200(
    client: TestClient,
    app: Any,
    admin_activation: Path,
    network_calls: List[Tuple[List[str], Dict[str, str]]],
) -> None:
    """``branch`` is optional on the request (git pushes per its own
    push.default); the response then reports no branch. (A branchless
    set_upstream push needs an existing upstream, which this activation
    has none of -- covered in test_git_push_refspec.py.)"""
    client.cookies.clear()

    response = client.post(
        f"/api/v1/repos/{ADMIN_ACTIVATION}/git/push",
        json={"remote": "origin", "set_upstream": False},
        headers=bearer(app, ADMIN),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "remote": "origin",
        "branch": None,
        "commits_pushed": 0,
    }
    _assert_credentials_only_at_run_time(network_calls, admin_activation)


def test_prepare_remote_operation_fails_when_sanitization_fails(
    app: Any, activation: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A clone whose stored URLs cannot be made credential-free gets no
    credentials: the operation fails, saying what failed."""
    from code_indexer.server.services.git_operations_service import GitCommandError

    _store_userinfo_origin(activation)
    config = activation / ".git" / "config"
    config.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(GitCommandError) as exc_info:
                app.state.activated_repo_manager.prepare_remote_operation(
                    USER, USER_ACTIVATION
                )
    finally:
        config.chmod(0o644)

    message = str(exc_info.value)
    assert "removing credentials from the stored remote URL failed" in message
    assert SECRET not in message
    assert all(SECRET not in r.getMessage() for r in caplog.records)


_MISSING_ALIAS = "never-activated-repo"


@pytest.mark.parametrize(
    "door", ["mcp_fetch", "mcp_pull", "rest_fetch", "rest_pull", "rest_push"]
)
def test_remote_operation_on_a_missing_clone_is_not_found_before_sanitization(
    client: TestClient,
    app: Any,
    network_calls: List[Tuple[List[str], Dict[str, str]]],
    caplog: pytest.LogCaptureFixture,
    door: str,
) -> None:
    """A repository with no clone on disk is a client error ("not found")
    at every door, decided before the stored URLs are sanitized: no
    sanitization failure, no ERROR log, no network call."""
    operation = door.split("_")[1]
    with caplog.at_level(logging.WARNING):
        if door.startswith("mcp"):
            body = mcp_call(
                client,
                app,
                ADMIN,
                f"git_{operation}",
                {"repository_alias": _MISSING_ALIAS},
            )
            text = str(body)
            assert '"success": false' in text.lower(), text
        else:
            client.cookies.clear()
            response = client.post(
                f"/api/v1/repos/{_MISSING_ALIAS}/git/{operation}",
                json={"remote": "origin"},
                headers=bearer(app, ADMIN),
            )
            assert response.status_code == 404, response.text
            text = response.text

    assert "not found" in text.lower(), text
    assert "removing credentials" not in text, text
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], errors
    assert network_calls == []


@contextlib.contextmanager
def _block_configuration(repo: Path, tmp_path: Path) -> Iterator[None]:
    """Include a FIFO in the clone's configuration while inside: every git
    command that reads it blocks until its timeout. The original
    configuration is restored on exit (the activation is shared)."""
    fifo = tmp_path / "blocking-include"
    os.mkfifo(fifo)
    config = repo / ".git" / "config"
    original = config.read_text()
    config.write_text(original + f"[include]\n\tpath = {fifo}\n")
    try:
        yield
    finally:
        config.write_text(original)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("door", ["mcp_push", "rest_push", "rest_pull", "rest_fetch"])
def test_stored_url_sanitization_timeout_fails_the_operation_before_credentials(
    client: TestClient,
    app: Any,
    admin_activation: Path,
    network_calls: List[Tuple[List[str], Dict[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    door: str,
) -> None:
    from code_indexer.utils import git_runner

    _store_userinfo_origin(admin_activation)
    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 0.5)
    expected = "removing credentials from the stored remote URL timed out after 0.5s"

    with _block_configuration(admin_activation, tmp_path):
        if door == "mcp_push":
            body = mcp_call(
                client, app, ADMIN, "git_push", {"repository_alias": ADMIN_ACTIVATION}
            )
            text = str(body)
            assert '"success": false' in text.lower(), text
        else:
            client.cookies.clear()
            response = client.post(
                f"/api/v1/repos/{ADMIN_ACTIVATION}/git/{door.split('_')[1]}",
                json={"remote": "origin"},
                headers=bearer(app, ADMIN),
            )
            assert response.status_code >= 400, response.text
            text = response.text

    assert expected in text, text
    assert "resolving remote" not in text
    assert SECRET not in text
    assert network_calls == []


def test_sync_rewrites_stored_credentials(app: Any, activation: Path) -> None:
    _store_userinfo_origin(activation)

    app.state.activated_repo_manager.sync_with_golden_repository(USER, USER_ACTIVATION)

    assert SECRET not in (activation / ".git" / "config").read_text()
    assert REPO  # the golden repository the activation syncs from
