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

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


def test_prepare_remote_operation_continues_with_a_warning_when_sanitization_fails(
    app: Any, activation: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The stored URL still authenticates, so the operation is not aborted."""
    _store_userinfo_origin(activation)
    config = activation / ".git" / "config"
    config.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            url = app.state.activated_repo_manager.prepare_remote_operation(
                USER, USER_ACTIVATION
            )
    finally:
        config.chmod(0o644)

    assert url is not None and SECRET in url  # the registered URL, as given
    caller_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name.endswith("activated_repo_manager")
    ]
    assert caller_warnings, [(r.name, r.getMessage()) for r in caplog.records]
    assert str(activation) in caller_warnings[0].getMessage()
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_sync_rewrites_stored_credentials(app: Any, activation: Path) -> None:
    _store_userinfo_origin(activation)

    app.state.activated_repo_manager.sync_with_golden_repository(USER, USER_ACTIVATION)

    assert SECRET not in (activation / ".git" / "config").read_text()
    assert REPO  # the golden repository the activation syncs from
