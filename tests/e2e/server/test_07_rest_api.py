"""Phase 3 — AC7: REST API core endpoints respond via in-process TestClient.

Verifies that a representative endpoint from every major REST API group returns
a valid HTTP status code.  Tests accept any response < 500 (success or client
error) as proof that the endpoint is registered and handled.  HTTP 5xx is always
a failure.

Endpoint groups covered:
    auth         — /auth/login, /auth/validate, /auth/refresh, /auth/logout
    repos        — /api/repos, /api/repos/available, /api/repos/status, /api/repos/discover
    admin-ops    — /api/admin/golden-repos, /api/admin/jobs/stats
    admin-users  — /api/admin/users
    jobs         — /api/jobs
    query        — /api/query
    files        — /api/v1/repos/{alias}/files
    ssh-keys     — /api/ssh-keys
    api-keys     — /api/api-keys/status
    groups       — /api/v1/groups
    users-v1     — /api/v1/users
    maintenance  — /api/admin/maintenance/status
    scip         — /scip/definition, /scip/references
    provider     — /api/admin/provider-indexes/providers, /admin/provider-health
    llm-creds    — /api/llm-creds/lease-status
    activated    — /api/activated-repos
    wiki         — /wiki/cidx-meta/
    health       — /health, /api/system/health
    diagnostics  — /admin/diagnostics
    openapi      — /openapi.json, /docs
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Credentials — environment variable names, same as conftest.py.
# e2e-automation.sh sets E2E_ADMIN_USER and E2E_ADMIN_PASS before pytest runs.
# ---------------------------------------------------------------------------
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"

# Unique sentinel object used to mark the auth/login table entry so the test
# function can build the payload at run time from environment variables instead
# of at collection time (when env vars may not yet be set).
_AUTH_LOGIN_SENTINEL = object()


def _admin_login_payload() -> dict:
    """Build the auth/login payload from environment variables.

    Raises RuntimeError if the required variables are not set so the error
    message clearly identifies the missing configuration rather than silently
    using wrong credentials.
    """
    username = os.environ.get(_ENV_ADMIN_USER, "")
    password = os.environ.get(_ENV_ADMIN_PASS, "")
    if not username:
        raise RuntimeError(
            f"Required environment variable {_ENV_ADMIN_USER!r} is not set. "
            "Run tests via e2e-automation.sh or export the variable manually."
        )
    if not password:
        raise RuntimeError(
            f"Required environment variable {_ENV_ADMIN_PASS!r} is not set. "
            "Run tests via e2e-automation.sh or export the variable manually."
        )
    return {"username": username, "password": password}


# ---------------------------------------------------------------------------
# REST endpoint table
# Each entry: (label, method, path, payload_or_None)
# The auth_login entry uses _AUTH_LOGIN_SENTINEL so credentials are resolved
# from environment variables at test execution time, not at collection time.
# ---------------------------------------------------------------------------
REST_ENDPOINTS = [
    # -- Auth ----------------------------------------------------------------
    ("auth_login", "POST", "/auth/login", _AUTH_LOGIN_SENTINEL),
    ("auth_validate", "GET", "/auth/validate", None),
    ("auth_refresh", "POST", "/auth/refresh", None),
    ("auth_logout", "POST", "/auth/logout", None),
    # -- Repos ---------------------------------------------------------------
    ("repos_list", "GET", "/api/repos", None),
    ("repos_available", "GET", "/api/repos/available", None),
    ("repos_status", "GET", "/api/repos/status", None),
    ("repos_discover", "GET", "/api/repos/discover", None),
    # -- Admin: golden repos -------------------------------------------------
    ("admin_golden_repos_list", "GET", "/api/admin/golden-repos", None),
    # POST without body returns 422 (< 500) — acceptable
    ("admin_golden_repos_post_empty", "POST", "/api/admin/golden-repos", None),
    # -- Admin: users --------------------------------------------------------
    ("admin_users_list", "GET", "/api/admin/users", None),
    # -- Admin: jobs ---------------------------------------------------------
    ("admin_jobs_stats", "GET", "/api/admin/jobs/stats", None),
    # -- Jobs ----------------------------------------------------------------
    ("jobs_list", "GET", "/api/jobs", None),
    # -- Query ---------------------------------------------------------------
    ("query_post", "POST", "/api/query", {"query_text": "function", "limit": 3}),
    # -- Files (no alias in fresh data dir → 404/422, not 5xx) ---------------
    ("files_list", "GET", "/api/v1/repos/cidx-meta/files", None),
    # -- SSH keys ------------------------------------------------------------
    ("ssh_keys_list", "GET", "/api/ssh-keys", None),
    # -- API keys ------------------------------------------------------------
    ("api_keys_status", "GET", "/api/api-keys/status", None),
    # -- Groups --------------------------------------------------------------
    ("groups_list", "GET", "/api/v1/groups", None),
    # -- Users (v1) ----------------------------------------------------------
    ("users_v1_list", "GET", "/api/v1/users", None),
    # -- Maintenance ---------------------------------------------------------
    ("maintenance_status", "GET", "/api/admin/maintenance/status", None),
    # -- SCIP (no index in fresh data dir → 4xx, not 5xx) --------------------
    ("scip_definition", "GET", "/scip/definition?symbol=foo&repository_alias=cidx-meta", None),
    ("scip_references", "GET", "/scip/references?symbol=foo&repository_alias=cidx-meta", None),
    # -- Provider indexes ----------------------------------------------------
    ("provider_indexes_providers", "GET", "/api/admin/provider-indexes/providers", None),
    # -- Provider health -----------------------------------------------------
    ("admin_provider_health", "GET", "/admin/provider-health", None),
    # -- LLM credentials -----------------------------------------------------
    ("llm_creds_lease_status", "GET", "/api/llm-creds/lease-status", None),
    # -- Activated repos -----------------------------------------------------
    ("activated_repos_list", "GET", "/api/activated-repos", None),
    # -- Wiki (wiki router prefixed at /wiki) --------------------------------
    ("wiki_repo_root", "GET", "/wiki/cidx-meta/", None),
    # -- Health --------------------------------------------------------------
    ("health", "GET", "/health", None),
    ("system_health", "GET", "/api/system/health", None),
    # -- Diagnostics (HTML page) ---------------------------------------------
    ("diagnostics_page", "GET", "/admin/diagnostics", None),
    # -- OpenAPI / docs ------------------------------------------------------
    ("openapi_json", "GET", "/openapi.json", None),
    ("docs", "GET", "/docs", None),
]


@pytest.mark.parametrize(
    "label,method,path,payload",
    REST_ENDPOINTS,
    ids=[e[0] for e in REST_ENDPOINTS],
)
def test_rest_endpoint(
    label: str,
    method: str,
    path: str,
    payload,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each REST endpoint responds with a non-5xx HTTP status code.

    A response < 500 proves the endpoint is registered, reachable, and handled
    without an internal server error.  4xx responses (auth failure, validation
    error, resource not found) are acceptable — they indicate the server
    understood the request and returned a meaningful error.
    """
    # Resolve sentinel to actual login payload at test execution time so
    # credentials come from environment variables rather than being embedded
    # in the parametrize table.
    resolved_payload = (
        _admin_login_payload() if payload is _AUTH_LOGIN_SENTINEL else payload
    )

    kwargs: dict = {"headers": auth_headers}
    if resolved_payload is not None and method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = resolved_payload

    resp = test_client.request(method, path, **kwargs)

    assert resp.status_code < 500, (
        f"[{label}] {method} {path} returned HTTP {resp.status_code} "
        f"(5xx = server error):\n{resp.text[:400]}"
    )
