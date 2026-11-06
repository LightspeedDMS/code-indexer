"""Every feature hook driven through a real front door (modular phase 4a).

A real app (``create_app`` via ``isolated_app``, never ~/.cidx-server) is
built over a server home whose ``config.json`` enables the in-tree test
feature ``cidx_sample_feature`` (installed as a real distribution by
``sample_feature_site``). Each contribution is then reached the way a user
reaches it: MCP JSON-RPC ``POST /mcp``, the REST route, the web page, the
``cidx`` click CLI, the config API, the database (migration version row),
the job list, and ``/health``. A second app over a home WITHOUT the feature
enabled shows none of them exist.

The lifespan is not run (it starts the whole server); the app runs the same
three lifespan helpers it calls (``load_configured_features``,
``start_features``, ``stop_features``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import anyio
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.responses import Response

from code_indexer import cli_features
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.features.migrations import feature_schema_versions_sqlite
from code_indexer.server.features.registry import set_active_registry
from code_indexer.server.startup.feature_wiring import (
    load_configured_features,
    start_features,
    stop_features,
)
from tests.unit.server._isolated_app import isolated_app

pytestmark = pytest.mark.timeout(120)

PASSWORD = "Example-Feature-Passw0rd!"
ADMIN = "example_admin"
USER = "example_user"
TOOL = "sample_ping"
NOTES = "/api/features/sample/notes"
SWEEP = "/api/features/sample/jobs/sweep"
PAGE = "/admin/features/sample"
CONFIG_LIST = "/api/admin/features/config"
CONFIG_PUT = "/api/admin/features/sample/config"


def _write_server_config(root: Path, enabled: List[str]) -> Path:
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    (server / "config.json").write_text(
        json.dumps({"server_dir": str(server), "enabled_features": enabled}),
        encoding="utf-8",
    )
    return server


@pytest.fixture(scope="module")
def enabled_app(
    tmp_path_factory: pytest.TempPathFactory, sample_feature_site: Dict[str, str]
) -> Iterator[SimpleNamespace]:
    root = tmp_path_factory.mktemp("feature-on")
    server = _write_server_config(root, ["sample"])
    with isolated_app(root) as app:
        accounts = app.state.user_manager
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        accounts.create_user(USER, PASSWORD, UserRole.NORMAL_USER)
        anyio.run(load_configured_features, app, None)
        anyio.run(start_features, app)
        try:
            yield SimpleNamespace(
                app=app, server=server, client=TestClient(app, follow_redirects=False)
            )
        finally:
            anyio.run(stop_features, app)
            set_active_registry(None)


@pytest.fixture(scope="module")
def disabled_app(
    tmp_path_factory: pytest.TempPathFactory, sample_feature_site: Dict[str, str]
) -> Iterator[SimpleNamespace]:
    root = tmp_path_factory.mktemp("feature-off")
    server = _write_server_config(root, [])
    with isolated_app(root) as app:
        app.state.user_manager.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        anyio.run(load_configured_features, app, None)
        try:
            yield SimpleNamespace(
                app=app, server=server, client=TestClient(app, follow_redirects=False)
            )
        finally:
            set_active_registry(None)


def _bearer(client: TestClient, username: str) -> Dict[str, str]:
    account = getattr(client.app, "state").user_manager.get_user(username)
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def _mcp(client: TestClient, username: str, method: str, params: Any) -> Any:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=_bearer(client, username),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tool_names(client: TestClient, username: str) -> List[str]:
    body = _mcp(client, username, "tools/list", {})
    return [tool["name"] for tool in body["result"]["tools"]]


def _web_session(client: TestClient, username: str, role: str) -> None:
    """Sign *username* in to the Web UI (the session cookie a login sets)."""
    from code_indexer.server.web.auth import SESSION_COOKIE_NAME, get_session_manager

    response = Response()
    get_session_manager().create_session(response, username, role)
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    name, value = cookie.split("=", 1)
    assert name == SESSION_COOKIE_NAME
    client.cookies.clear()
    client.cookies.set(name, value)


def _health(client: TestClient) -> Dict[str, Any]:
    # The package conftest unpublishes the registry before every test; the
    # lifespan helper publishes it again, as the running server does.
    anyio.run(load_configured_features, client.app, None)
    response = client.get("/health", headers=_bearer(client, ADMIN))
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


# ── MCP tool ─────────────────────────────────────────────────────────────────


def test_mcp_tool_is_listed_and_called_through_json_rpc(enabled_app: Any) -> None:
    client = enabled_app.client
    assert TOOL in _tool_names(client, USER)
    body = _mcp(client, USER, "tools/call", {"name": TOOL, "arguments": {}})
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload == {"pong": True, "user": USER, "greeting": "hello"}


def test_mcp_tool_doc_comes_from_the_feature(enabled_app: Any) -> None:
    from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader

    doc = _get_tool_doc_loader().get_all_docs()[TOOL]
    assert doc.required_permission == "query_repos"
    assert "pong" in doc.description


# ── REST route ───────────────────────────────────────────────────────────────


def test_rest_route_requires_authentication(enabled_app: Any) -> None:
    client = enabled_app.client
    client.cookies.clear()
    assert client.get(NOTES).status_code == 401


def test_rest_route_answers_the_caller(enabled_app: Any) -> None:
    client = enabled_app.client
    response = client.get(NOTES, headers=_bearer(client, USER))
    assert response.status_code == 200, response.text
    assert response.json()["viewer"] == USER


# ── web page ─────────────────────────────────────────────────────────────────


def test_web_page_without_a_session_redirects_to_login(enabled_app: Any) -> None:
    client = enabled_app.client
    client.cookies.clear()
    response = client.get(PAGE)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_web_page_renders_inside_the_shell_with_its_nav_entry(
    enabled_app: Any,
) -> None:
    client = enabled_app.client
    _web_session(client, ADMIN, "admin")
    response = client.get(PAGE)
    assert response.status_code == 200, response.text
    html = response.text
    assert 'id="sample-feature-page"' in html
    assert "Viewer: example_admin" in html
    assert 'class="admin-nav"' in html  # the core shell
    assert f'<a href="{PAGE}" aria-current="page">Sample Feature</a>' in html


def test_web_nav_lists_the_feature_page_on_core_pages(enabled_app: Any) -> None:
    client = enabled_app.client
    _web_session(client, ADMIN, "admin")
    response = client.get("/admin/config")
    assert response.status_code == 200, response.text
    assert f'href="{PAGE}"' in response.text
    assert 'id="feature-settings"' in response.text


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli(server: Path, args: List[str]) -> Any:
    from code_indexer.cli import cli

    cli_features.reset_feature_commands()
    try:
        return CliRunner().invoke(
            cli, args, env={"CIDX_SERVER_DATA_DIR": str(server)}, catch_exceptions=False
        )
    finally:
        cli_features.reset_feature_commands()


def test_cli_command_runs_through_click(enabled_app: Any) -> None:
    result = _cli(enabled_app.server, ["sample-hello"])
    assert result.exit_code == 0, result.output
    assert "hello from sample" in result.output


def test_cli_help_lists_the_feature_command(enabled_app: Any) -> None:
    result = _cli(enabled_app.server, ["--help"])
    assert result.exit_code == 0, result.output
    assert "sample-hello" in result.output


# ── config section ───────────────────────────────────────────────────────────


def test_config_api_lists_the_section_with_bootstrap_and_runtime_keys(
    enabled_app: Any,
) -> None:
    client = enabled_app.client
    response = client.get(CONFIG_LIST, headers=_bearer(client, ADMIN))
    assert response.status_code == 200, response.text
    (section,) = response.json()["sections"]
    kinds = {key["name"]: key["kind"] for key in section["keys"]}
    assert section["feature"] == "sample"
    assert kinds == {
        "greeting": "runtime",
        "simulate_unhealthy": "runtime",
        "mode": "bootstrap",
    }


def test_config_api_is_admin_only(enabled_app: Any) -> None:
    client = enabled_app.client
    assert client.get(CONFIG_LIST, headers=_bearer(client, USER)).status_code == 403


def test_config_api_updates_a_runtime_key_seen_by_the_feature(
    enabled_app: Any,
) -> None:
    client = enabled_app.client
    response = client.put(
        CONFIG_PUT, json={"greeting": "bonjour"}, headers=_bearer(client, ADMIN)
    )
    assert response.status_code == 200, response.text
    try:
        body = _mcp(client, USER, "tools/call", {"name": TOOL, "arguments": {}})
        assert json.loads(body["result"]["content"][0]["text"])["greeting"] == "bonjour"
    finally:
        client.put(
            CONFIG_PUT, json={"greeting": "hello"}, headers=_bearer(client, ADMIN)
        )


def test_config_api_refuses_a_bootstrap_key_and_a_wrong_type(
    enabled_app: Any,
) -> None:
    client = enabled_app.client
    headers = _bearer(client, ADMIN)
    bootstrap = client.put(CONFIG_PUT, json={"mode": "other"}, headers=headers)
    assert bootstrap.status_code == 400
    assert "bootstrap" in bootstrap.json()["detail"]
    wrong = client.put(CONFIG_PUT, json={"simulate_unhealthy": "yes"}, headers=headers)
    assert wrong.status_code == 400


def _feature_form_csrf(html: str, feature: str) -> str:
    """The CSRF token of *feature*'s form on the config screen."""
    import re

    article = html[html.index(f'id="feature-settings-{feature}"') :]
    match = re.search(r'name="csrf_token" value="([^"]+)"', article)
    assert match is not None
    return match.group(1)


def test_config_screen_form_saves_runtime_keys(enabled_app: Any) -> None:
    """The config screen's Feature Settings form: a session, its CSRF token,
    form strings converted to the keys' types (an unchecked box is False),
    then the same audited commit as the API."""
    client = enabled_app.client
    _web_session(client, ADMIN, "admin")
    page = client.get("/admin/config")
    assert page.status_code == 200, page.text
    assert 'name="setting__greeting"' in page.text
    form = {
        "csrf_token": _feature_form_csrf(page.text, "sample"),
        "setting__greeting": "bonjour",
    }
    try:
        response = client.post(
            "/admin/features/config/sample", data=form, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        assert response.headers["location"] == "/admin/config#feature-settings"
        listed = client.get(CONFIG_LIST, headers=_bearer(client, ADMIN)).json()
        values = {k["name"]: k["value"] for k in listed["sections"][0]["keys"]}
        assert values["greeting"] == "bonjour"
        assert values["simulate_unhealthy"] is False
    finally:
        client.put(
            CONFIG_PUT, json={"greeting": "hello"}, headers=_bearer(client, ADMIN)
        )


def test_config_screen_form_refuses_a_bad_csrf_token(enabled_app: Any) -> None:
    client = enabled_app.client
    _web_session(client, ADMIN, "admin")
    response = client.post(
        "/admin/features/config/sample",
        data={"csrf_token": "forged", "setting__greeting": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 403


# ── migration ────────────────────────────────────────────────────────────────


def _db_path(app: Any) -> str:
    return str(app.state.db_path_str)


def test_migration_applied_with_a_version_row(enabled_app: Any) -> None:
    db = _db_path(enabled_app.app)
    assert feature_schema_versions_sqlite(db) == {"sample": 1}
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "sample_notes" in tables


# ── background job ───────────────────────────────────────────────────────────


def _wait_for_job(client: TestClient, job_id: str) -> Dict[str, Any]:
    deadline = time.monotonic() + 30
    while True:
        response = client.get(f"/api/jobs/{job_id}", headers=_bearer(client, ADMIN))
        assert response.status_code == 200, response.text
        job: Dict[str, Any] = response.json()
        if job["status"] in ("completed", "failed") or time.monotonic() > deadline:
            return job
        time.sleep(0.1)


def test_background_job_runs_in_the_job_manager_and_is_listed(
    enabled_app: Any,
) -> None:
    client = enabled_app.client
    response = client.post(SWEEP, headers=_bearer(client, ADMIN))
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    job = _wait_for_job(client, job_id)
    assert job["status"] == "completed", job
    listed = client.get("/api/jobs?limit=100", headers=_bearer(client, ADMIN)).json()
    ours = [j for j in listed["jobs"] if j["job_id"] == job_id]
    assert [j["operation_type"] for j in ours] == ["sample_sweep"]
    notes = client.get(NOTES, headers=_bearer(client, ADMIN)).json()["notes"]
    assert "swept" in notes


def test_recurring_job_is_scheduled(enabled_app: Any) -> None:
    scheduler = enabled_app.app.state.feature_job_scheduler
    assert [(e["feature"], e["job"].name) for e in scheduler.recurring()] == [
        ("sample", "sweep")
    ]


# ── health ───────────────────────────────────────────────────────────────────


def test_health_lists_the_feature_and_degrades_on_an_unhealthy_report(
    enabled_app: Any,
) -> None:
    from code_indexer.server.models.api_models import HealthStatus
    from code_indexer.server.services.health_service import HealthCheckService

    client = enabled_app.client
    (loaded,) = _health(client)["features"]["loaded"]
    assert loaded["name"] == "sample" and loaded["started"] is True
    assert loaded["health"][0]["status"] == "healthy"
    put = client.put(
        CONFIG_PUT, json={"simulate_unhealthy": True}, headers=_bearer(client, ADMIN)
    )
    assert put.status_code == 200, put.text
    try:
        body = _health(client)
        assert body["features"]["loaded"][0]["health"][0]["status"] == "unhealthy"
        assert body["status"] == "degraded"
        system = HealthCheckService().get_system_health()
        reasons = system.failure_reasons
        assert any(r.startswith("Feature sample") for r in reasons), reasons
        # DEGRADED at most: UNHEALTHY only if this host has another error.
        assert system.status != HealthStatus.HEALTHY
        if system.status == HealthStatus.UNHEALTHY:
            assert any(not r.startswith("Feature ") for r in reasons)
    finally:
        client.put(
            CONFIG_PUT,
            json={"simulate_unhealthy": False},
            headers=_bearer(client, ADMIN),
        )


# ── feature not enabled: none of it exists ───────────────────────────────────


def test_disabled_feature_contributes_nothing(disabled_app: Any) -> None:
    client = disabled_app.client
    assert TOOL not in _tool_names(client, ADMIN)
    body = _mcp(client, ADMIN, "tools/call", {"name": TOOL, "arguments": {}})
    assert "error" in body
    assert client.get(NOTES, headers=_bearer(client, ADMIN)).status_code == 404
    _web_session(client, ADMIN, "admin")
    assert client.get(PAGE).status_code == 404
    listed = client.get(CONFIG_LIST, headers=_bearer(client, ADMIN))
    assert listed.json() == {"sections": []}
    assert feature_schema_versions_sqlite(_db_path(disabled_app.app)) == {}
    health = _health(client)["features"]
    assert health["loaded"] == [] and "sample" in health["installed_not_enabled"]
    # The CLI is the exception, by design (phase 4c): a feature's commands
    # need no server, so an INSTALLED feature's commands work whatever the
    # server's enabled_features says.
    result = _cli(disabled_app.server, ["sample-hello"])
    assert result.exit_code == 0, result.output
    assert "hello from sample" in result.output
