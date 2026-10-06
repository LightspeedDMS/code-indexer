"""Shared front-door environment for the activated-repository access tests.

A real app (``create_app`` via ``isolated_app``, never ~/.cidx-server)
whose routes reach A15's real services (QueryAccessEnv): AccessFilteringService
over a real GroupAccessManager, real ActivatedRepoManager activations (real
git clones), GoldenRepoManager and the global registry. Nothing about the
access decision is mocked.

A test module imports the fixtures it uses (``client``, ``env``,
``server_db_template``); each importing module gets its own app.

Repository aliases and usernames are neutral placeholders.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    GRANTED_REPO,
    GRANTED_REPO_2,
    USER,
    QueryAccessEnv,
    build_server_db_template,
)

PASSWORD = "Example-Activation-Passw0rd!"
# A second non-admin in USER's group: granted the same repositories.
PEER = "example_peer"
PEER_ACTIVATION = "peer-repo"
# ADMIN *role* (every route permission) but a member of the restricted
# group, not of "admins": the access model's admin is the admins group
# (the definition MCP uses), so the activation guard applies to this user
# on every route, including the repository:admin ones.
SWEEPER = "example_sweeper"
COMPOSITE_ALIAS = "my-composite"
# An alias no activation or golden repository carries.
UNKNOWN_ALIAS = "never-activated-repo"
# Every example repository's README.md holds this text (_init_git_repo).
README = "README.md"
README_TEXT = "example"

# (method, path template, request kwargs, status for an allowed caller)
Probe = Tuple[str, str, Dict[str, Any], int]

# Every ActivatedRepoManager instance attribute: the app's own manager
# adopts each from the env's manager (see _route_to_env).
_MANAGER_STATE = (
    "data_dir",
    "activated_repos_dir",
    "_pool",
    "logger",
    "golden_repo_manager",
    "background_job_manager",
    "_clone_backend",
    "_index_manager",
    "_query_tracker",
)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("activated-rest-app")) as app:
        accounts: UserManager = app.state.user_manager
        accounts.create_user(USER, PASSWORD, UserRole.POWER_USER)
        accounts.create_user(PEER, PASSWORD, UserRole.POWER_USER)
        accounts.create_user(SWEEPER, PASSWORD, UserRole.ADMIN)
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        yield TestClient(app, follow_redirects=False)


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("server_db_template"))


def _app_state(client: TestClient) -> Any:
    # TestClient.app is typed as a bare ASGI callable; it is the FastAPI
    # app whose ``state`` attributes are attached at runtime.
    return client.app.state  # type: ignore[attr-defined]


@pytest.fixture
def env(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_db_template: Path,
) -> Iterator[QueryAccessEnv]:
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path, server_db_template)
    for member in (PEER, SWEEPER):
        e.group_manager.assign_user_to_group(member, e.group_id, assigned_by="test")
    try:
        with e.installed(e.access_service):
            _route_to_env(e, _app_state(client), monkeypatch)
            yield e
    finally:
        e.close()


def _route_to_env(
    e: QueryAccessEnv, real_app_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every route's repository and access lookups at the env's real
    services, wherever the route reads them from."""
    # Routers that read the module-level app (git, files, indexing,
    # activated-repos): its stand-in (QueryAccessEnv.installed) carries the
    # env's access service but not these two names, so they are ADDED
    # (raising=False: the attribute does not exist yet).
    stand_in = vars(importlib.import_module("code_indexer.server.app"))["app"]
    monkeypatch.setattr(
        stand_in.state,
        "activated_repo_manager",
        e.activated_repo_manager,
        raising=False,
    )
    monkeypatch.setattr(
        stand_in.state, "background_job_manager", e.job_manager, raising=False
    )
    # Routers that read the request's app (inline repos, v1/v2, wiki). The
    # access service is installed by the lifespan, which TestClient(app)
    # without a context manager never runs, so it is ADDED here too.
    monkeypatch.setattr(
        real_app_state, "access_filtering_service", e.access_service, raising=False
    )
    # The inline repo routes hold the app's own ActivatedRepoManager
    # instance (captured at registration): it adopts the env manager's
    # state, so it reads the env's activations, golden repos and clones.
    app_manager = real_app_state.activated_repo_manager
    env_state = vars(e.activated_repo_manager)
    assert set(env_state) == set(_MANAGER_STATE), (
        "ActivatedRepoManager state changed; update _MANAGER_STATE: "
        f"{sorted(set(env_state) ^ set(_MANAGER_STATE))}"
    )
    for name in _MANAGER_STATE:
        monkeypatch.setattr(app_manager, name, env_state[name])
    # git_operations_service resolves activations through its plain
    # backing attribute (reading the property would build a manager under
    # the real home).
    monkeypatch.setattr(
        git_operations_service, "_activated_repo_manager_lazy", e.activated_repo_manager
    )


def call(
    client: TestClient,
    username: str,
    probe: Probe,
    alias: str,
    params: Optional[Dict[str, Any]] = None,
) -> Response:
    """One request as *username*, with ``{a}`` in *probe* set to *alias*."""
    method, template, kwargs, _ = probe
    account = _app_state(client).user_manager.get_user(username)
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    client.cookies.clear()
    filled = json.loads(json.dumps(kwargs).replace("{a}", alias))
    if params:
        filled["params"] = {**filled.get("params", {}), **params}
    return client.request(
        method,
        template.replace("{a}", alias),
        headers={"Authorization": f"Bearer {token}"},
        **filled,
    )


def shape(response: Response, alias: str) -> Tuple[int, str]:
    """Status and body with the requested alias masked."""
    return response.status_code, response.text.replace(alias, "<alias>")


def assert_refused_as_unknown(
    client: TestClient, username: str, probe: Probe, alias: str
) -> None:
    refused = call(client, username, probe, alias)
    unknown = call(client, username, probe, UNKNOWN_ALIAS)
    assert shape(refused, alias) == shape(unknown, UNKNOWN_ALIAS)
    assert refused.status_code >= 400, refused.text


def activate_composite(env: QueryAccessEnv, username: str) -> None:
    job_id = env.activated_repo_manager.activate_repository(
        username=username,
        golden_repo_aliases=[GRANTED_REPO, GRANTED_REPO_2],
        user_alias=COMPOSITE_ALIAS,
    )
    status = env.wait_for_job(job_id, username)
    assert status["status"] == "completed", status
