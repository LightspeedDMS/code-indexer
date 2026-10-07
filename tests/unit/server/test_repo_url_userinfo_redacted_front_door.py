# ruff: noqa: F811
"""Repository URLs are returned with their userinfo redacted on every front
door (REST, MCP, Web), for administrators and granted users alike.

A golden repository is registered with a URL carrying userinfo, and its
clone carries that URL as its ``origin`` remote (see repo_url_userinfo_env).
The stored value keeps it (git operations need it), while every response
that returns a golden or global repository URL carries the single shared
redacted form from ``code_indexer.utils.credential_redaction``.
"""

from __future__ import annotations

import json
import logging
import time
from http.cookies import SimpleCookie
from typing import Any, Dict
from urllib.parse import quote

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    ADMIN,
    GLOBAL_ALIAS,
    REDACTED_URL,
    REPO,
    SECRET,
    USER,
    USER_ACTIVATION,
    USERINFO_URL,
    activate_for_user,
    app,
    assert_no_userinfo,
    client,
    get,
    mcp_text,
)

# Discovery response fields that echo the caller's own ``source`` argument.
ECHOED_SOURCE = ("query_url", "normalized_url")


# ---------------------------------------------------------------- stored value


def test_stored_registration_url_is_unchanged(app: Any) -> None:
    """Redaction applies to responses only: the stored URL keeps its
    userinfo, in the golden metadata and the global registry."""
    stored = app.state.golden_repo_manager.get_golden_repo(REPO)
    assert stored is not None
    assert stored.repo_url == USERINFO_URL
    registry_row = app.state.backend_registry.global_repos.get_repo(GLOBAL_ALIAS)
    assert registry_row["repo_url"] == USERINFO_URL


# ------------------------------------------------------------------------ REST


def test_admin_golden_repo_list_returns_redacted_url(
    client: TestClient, app: Any
) -> None:
    response = get(client, app, ADMIN, "/api/admin/golden-repos")
    assert response.status_code == 200, response.text
    assert_no_userinfo(response.text)
    repos = {r["alias"]: r for r in response.json()["golden_repositories"]}
    assert repos[REPO]["repo_url"] == REDACTED_URL


@pytest.mark.parametrize("username", [ADMIN, USER])
def test_golden_repo_details_return_redacted_url(
    client: TestClient, app: Any, username: str
) -> None:
    details = get(client, app, username, f"/api/repos/golden/{REPO}")
    assert details.status_code == 200, details.text
    assert_no_userinfo(details.text)
    assert details.json()["repo_url"] == REDACTED_URL

    v2 = get(client, app, username, f"/api/repositories/{REPO}")
    assert v2.status_code == 200, v2.text
    assert_no_userinfo(v2.text)
    assert v2.json()["git_info"]["remote_url"] == REDACTED_URL


@pytest.mark.parametrize("username", [ADMIN, USER])
def test_available_repo_listing_returns_and_searches_redacted_url(
    client: TestClient, app: Any, username: str
) -> None:
    """The listing returns the redacted URL, and its search filter matches
    the URL as returned: the userinfo matches nothing, the host still does."""
    listing = get(client, app, username, "/api/repos/available")
    assert listing.status_code == 200, listing.text
    assert_no_userinfo(listing.text)
    urls = {r["alias"]: r["repo_url"] for r in listing.json()["repositories"]}
    assert urls[REPO] == REDACTED_URL

    by_secret = get(client, app, username, f"/api/repos/available?search={SECRET}")
    assert by_secret.status_code == 200, by_secret.text
    assert by_secret.json()["total"] == 0, by_secret.text

    by_host = get(client, app, username, "/api/repos/available?search=git.example")
    assert by_host.status_code == 200, by_host.text
    assert [r["alias"] for r in by_host.json()["repositories"]] == [REPO]


def test_repo_discovery_returns_redacted_git_urls(
    client: TestClient,
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Golden and activated matches return the repository URL redacted.
    ``query_url`` / ``normalized_url`` echo the caller's own ``source``;
    the logs carry it redacted. Driven as ADMIN: the access rule returns
    activated matches of every user to an administrator."""
    activate_for_user(app, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        response = get(
            client, app, ADMIN, f"/api/repos/discover?source={quote(USERINFO_URL)}"
        )
    # Server log records only: httpx logs the test client's own request URL.
    server_logs = [
        r.getMessage() for r in caplog.records if not r.name.startswith("httpx")
    ]
    assert not [m for m in server_logs if SECRET in m], server_logs
    assert response.status_code == 200, response.text
    body = response.json()
    matches = body["golden_repositories"] + body["activated_repositories"]
    assert {m["repository_type"] for m in matches} == {"golden", "activated"}, body
    assert {m["git_url"] for m in matches} == {REDACTED_URL}, body
    assert_no_userinfo(
        json.dumps({k: v for k, v in body.items() if k not in ECHOED_SOURCE})
    )


def test_activated_repository_listings_and_details_carry_no_userinfo(
    client: TestClient, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    activate_for_user(app, monkeypatch)
    for username, path in (
        (USER, "/api/repos"),
        (USER, f"/api/repos/{USER_ACTIVATION}"),
        (ADMIN, "/api/admin/activated-repos"),
    ):
        response = get(client, app, username, path)
        assert response.status_code == 200, (path, response.text)
        assert USER_ACTIVATION in response.text, (path, response.text)
        assert_no_userinfo(response.text)


@pytest.mark.parametrize("username", [ADMIN, USER])
def test_rest_global_repo_list_and_status_return_redacted_url(
    client: TestClient, app: Any, username: str
) -> None:
    listing = get(client, app, username, "/global/repos")
    assert listing.status_code == 200, listing.text
    assert_no_userinfo(listing.text)
    urls = {r["alias"]: r["url"] for r in listing.json()["repos"]}
    assert urls[GLOBAL_ALIAS] == REDACTED_URL

    status = get(client, app, username, f"/global/repos/{GLOBAL_ALIAS}/status")
    assert status.status_code == 200, status.text
    assert_no_userinfo(status.text)
    assert status.json()["url"] == REDACTED_URL


# ------------------------------------------------------------------------- MCP


MCP_TOOL_CALLS = [
    ("list_global_repos", {}),
    ("repository_status", {"alias": GLOBAL_ALIAS}),
    ("list_repositories", {}),
    ("get_all_repositories_status", {}),
    ("discover_repositories", {}),
]


@pytest.mark.parametrize("username", [ADMIN, USER])
@pytest.mark.parametrize(
    "tool,arguments", MCP_TOOL_CALLS, ids=[t for t, _ in MCP_TOOL_CALLS]
)
def test_mcp_repository_tools_return_redacted_url(
    client: TestClient,
    app: Any,
    username: str,
    tool: str,
    arguments: Dict[str, Any],
) -> None:
    text = mcp_text(client, app, username, tool, arguments)
    payload = json.loads(text)
    assert payload.get("success") is True, payload
    assert REDACTED_URL in text, payload
    assert_no_userinfo(text)


def _finished_add_job(app: Any) -> str:
    """An add_golden_repo job whose metadata carries the registration URL
    (as GoldenRepoManager submits it), finished and persisted."""
    jobs = app.state.background_job_manager
    job_id: str = jobs.submit_job(
        operation_type="add_golden_repo",
        func=lambda: {"success": True, "message": "done"},
        submitter_username=ADMIN,
        is_admin=True,
        repo_alias="userinfo-job-repo",
        metadata={"repo_url": USERINFO_URL, "alias": "userinfo-job-repo"},
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = jobs.get_job_status(job_id, ADMIN, is_admin=True)
        if status and status["status"] == "completed" and job_id not in jobs.jobs:
            return job_id
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within 30s")


def test_mcp_job_details_return_redacted_registration_url(
    client: TestClient, app: Any
) -> None:
    job_id = _finished_add_job(app)
    stored = app.state.background_job_manager._sqlite_backend.get_job(job_id)
    assert stored["metadata"]["repo_url"] == USERINFO_URL

    payload = json.loads(
        mcp_text(client, app, ADMIN, "get_job_details", {"job_id": job_id})
    )
    assert payload.get("success") is True, payload
    assert payload["job"]["metadata"]["repo_url"] == REDACTED_URL


def test_mcp_ci_forge_detection_error_returns_redacted_url(
    client: TestClient, app: Any
) -> None:
    """The host is neither GitHub nor GitLab: the error names the repository's
    remote URL, redacted."""
    payload = json.loads(
        mcp_text(client, app, ADMIN, "ci_list_runs", {"repository_alias": GLOBAL_ALIAS})
    )
    assert payload.get("success") is False, payload
    assert payload["remote_url"] == REDACTED_URL


# ------------------------------------------------------------------------- Web


def _web_client(app: Any) -> TestClient:
    """A client holding a real admin Web session."""
    from code_indexer.server.web import auth as web_auth

    session_response = Response()
    web_auth.get_session_manager().create_session(session_response, ADMIN, "admin")
    cookie: SimpleCookie = SimpleCookie()
    for header in session_response.headers.getlist("set-cookie"):
        cookie.load(header)
    web = TestClient(app, follow_redirects=False)
    web.cookies.set(
        web_auth.SESSION_COOKIE_NAME, cookie[web_auth.SESSION_COOKIE_NAME].value
    )
    return web


def test_web_golden_repos_page_and_details_return_redacted_url(app: Any) -> None:
    web = _web_client(app)
    page = web.get("/admin/golden-repos")
    assert page.status_code == 200, page.text
    assert_no_userinfo(page.text)
    assert REDACTED_URL in page.text

    details = web.get(f"/admin/partials/golden-repos/{REPO}/details")
    assert details.status_code == 200, details.text
    assert_no_userinfo(details.text)
    assert REDACTED_URL in details.text


def test_web_add_golden_repo_error_returns_redacted_url(app: Any) -> None:
    """Registering an unreachable URL that carries userinfo: the page's
    error names the URL redacted."""
    from code_indexer.server.web import routes as web_routes
    from tests.unit.server.self_service_elevation_harness import enforcement

    web = _web_client(app)
    csrf_response = Response()
    web_routes.set_csrf_cookie(csrf_response, "example-csrf-token")
    cookie: SimpleCookie = SimpleCookie()
    for header in csrf_response.headers.getlist("set-cookie"):
        cookie.load(header)
    web.cookies.set(
        web_routes.CSRF_COOKIE_NAME, cookie[web_routes.CSRF_COOKIE_NAME].value
    )
    unreachable = USERINFO_URL.replace("userinfo-repo", "unreachable-repo")
    with enforcement(False):
        page = web.post(
            "/admin/golden-repos/add",
            data={
                "alias": "unreachable-repo",
                "repo_url": unreachable,
                "csrf_token": "example-csrf-token",
            },
        )
    assert page.status_code == 200, page.text
    assert_no_userinfo(page.text)
    assert "***@git.example.com:8443/example/unreachable-repo.git" in page.text
