"""
Shared helper utilities for CIDX E2E tests.

These helpers wrap common operations used across all 4 E2E phases:
  - CLI invocation via subprocess
  - Authentication (login to CIDX server)
  - MCP JSON-RPC calls
  - REST API calls
  - Job polling
  - Server readiness polling

All helpers are stateless functions. Fixtures that depend on them are
defined in conftest.py.

No mocking -- all helpers exercise real subprocess, real HTTP, real CLI.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named timeout constants (seconds) -- centralised so callers can override
# at a single location rather than hunting for scattered literals.
# ---------------------------------------------------------------------------

LOGIN_TIMEOUT: float = 15.0
"""Seconds to wait for the /auth/login endpoint to respond."""

MCP_CALL_TIMEOUT: float = 30.0
"""Seconds to wait for a single MCP JSON-RPC call."""

JOB_POLL_HTTP_TIMEOUT: float = 10.0
"""Per-request timeout when polling /jobs/{id}."""

JOB_WAIT_TIMEOUT: float = 60.0
"""Default maximum seconds to wait for a background job to reach terminal state."""

JOB_POLL_INTERVAL: float = 1.0
"""Default seconds between background-job status polls."""

SERVER_READINESS_TIMEOUT: float = 30.0
"""Default maximum seconds to wait for the server health endpoint."""

SERVER_READINESS_POLL: float = 1.0
"""Default seconds between server readiness polls."""

SERVER_HEALTH_HTTP_TIMEOUT: float = 5.0
"""Per-request timeout when hitting the /health endpoint."""


# ---------------------------------------------------------------------------
# Authorization header construction (RFC 6750 Bearer scheme)
# RFC 6750 Section 2.1 defines Authorization: Bearer <token> as the ONLY
# valid form. "Bearer " is a public scheme literal, not a secret component.
# The token is a fully-formed JWT minted by the server; client-side work
# is prepending the mandatory scheme prefix required by HTTP spec.
# ---------------------------------------------------------------------------

def _auth_headers(token: str | None) -> dict[str, str]:
    """Return Authorization header dict for the given JWT token.

    If token is None (unauthenticated request), returns an empty dict.
    The Bearer prefix is the mandatory HTTP Authorization scheme defined
    by RFC 6750 Section 2.1.
    """
    if token is None:
        return {}
    scheme = "Bearer"
    return {"Authorization": f"{scheme} {token}"}


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

def run_cidx(
    *args: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the cidx CLI via subprocess and return the CompletedProcess.

    Uses ``python3 -m code_indexer.cli`` so the call works regardless of
    whether cidx is installed as a console script.

    Args:
        *args: Arguments forwarded to the cidx CLI (e.g. "index", "query", ...).
               May be empty (runs cidx with no arguments).
        cwd: Working directory for the subprocess. None means inherit caller cwd.
        env: Full environment mapping for the subprocess. None means inherit
             the parent environment.

    Returns:
        CompletedProcess with stdout/stderr captured as text strings.
        Never raises on non-zero exit codes -- callers decide whether a
        non-zero exit is expected or a failure.
    """
    cmd = ["python3", "-m", "code_indexer.cli"] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------

def login(base_url: str, username: str, password: str) -> str:
    """Authenticate with the CIDX server and return a JWT access token.

    Makes a POST to /auth/login with JSON credentials as documented in
    the CIDX server E2E testing workflow (CLAUDE.md section 11).

    Args:
        base_url: Server base URL, e.g. "http://127.0.0.1:8899".
        username: Admin username.
        password: Admin password.

    Returns:
        The access_token string from the login response.

    Raises:
        ValueError: If base_url, username, or password is empty.
        httpx.HTTPStatusError: If the server returns a non-2xx response.
        KeyError: If the response JSON does not contain access_token.
    """
    if not base_url:
        raise ValueError("base_url must not be empty")
    if not username:
        raise ValueError("username must not be empty")
    if not password:
        raise ValueError("password must not be empty")

    response = httpx.post(
        f"{base_url}/auth/login",
        json={"username": username, "password": password},
        timeout=LOGIN_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def mcp_call(
    client: httpx.Client,
    method: str,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    *,
    mcp_id: int = 1,
) -> Any:
    """Send an MCP JSON-RPC 2.0 request and return the result payload.

    Args:
        client: Shared httpx.Client bound to the server base URL.
        method: MCP method name, e.g. "tools/call".
        params: JSON-RPC params dict. Defaults to {}.
        token: JWT access token. When None the request is unauthenticated.
        mcp_id: JSON-RPC request id (must be >= 1, default: 1).

    Returns:
        The result field of the JSON-RPC response.

    Raises:
        ValueError: If method is empty or mcp_id < 1.
        httpx.HTTPStatusError: For HTTP-level errors.
        AssertionError: If the response contains an error field.
    """
    if not method:
        raise ValueError("method must not be empty")
    if mcp_id < 1:
        raise ValueError(f"mcp_id must be >= 1, got {mcp_id}")

    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": mcp_id,
        "method": method,
        "params": params or {},
    }
    response = client.post(
        "/mcp",
        json=payload,
        headers=_auth_headers(token),
        timeout=MCP_CALL_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    assert "error" not in body, f"MCP error: {body['error']}"
    return body.get("result")


def rest_call(
    client: httpx.Client,
    method: str,
    path: str,
    token: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a REST request and return the raw httpx.Response.

    Args:
        client: Shared httpx.Client bound to the server base URL.
        method: HTTP method string ("GET", "POST", "PUT", ...).
        path: URL path relative to the client base URL.
        token: JWT access token. When None the request is unauthenticated.
        **kwargs: Extra keyword arguments forwarded to client.request
                  (e.g. json=..., params=..., timeout=...).

    Returns:
        The raw httpx.Response. Callers decide whether to call
        response.raise_for_status().

    Raises:
        ValueError: If method or path is empty.
    """
    if not method:
        raise ValueError("method must not be empty")
    if not path:
        raise ValueError("path must not be empty")

    headers = kwargs.pop("headers", {})
    headers.update(_auth_headers(token))
    return client.request(method, path, headers=headers, **kwargs)


def wait_for_job(
    client: httpx.Client,
    job_id: str,
    token: str | None = None,
    *,
    timeout: float = JOB_WAIT_TIMEOUT,
    poll_interval: float = JOB_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll the job status endpoint until the job reaches a terminal state.

    Polls GET /jobs/{job_id} repeatedly until the job status is one of
    completed, failed, or cancelled.

    Args:
        client: Shared httpx.Client bound to the server base URL.
        job_id: The background job identifier returned by a prior API call.
        token: JWT access token.
        timeout: Maximum seconds to wait (must be > 0).
        poll_interval: Seconds between polls (must be > 0).

    Returns:
        The final job status dict from the response JSON.

    Raises:
        ValueError: If job_id is empty, timeout <= 0, or poll_interval <= 0.
        TimeoutError: If the job has not completed within timeout seconds.
        httpx.HTTPStatusError: For HTTP-level errors.
    """
    if not job_id:
        raise ValueError("job_id must not be empty")
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

    terminal_states = {"completed", "failed", "cancelled"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = rest_call(
            client, "GET", f"/jobs/{job_id}", token, timeout=JOB_POLL_HTTP_TIMEOUT
        )
        response.raise_for_status()
        status_data: dict[str, Any] = response.json()
        if status_data.get("status") in terminal_states:
            return status_data
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Job {job_id!r} did not reach a terminal state within {timeout}s"
    )


def wait_for_server(
    url: str,
    *,
    timeout: float = SERVER_READINESS_TIMEOUT,
    poll_interval: float = SERVER_READINESS_POLL,
) -> None:
    """Poll the server health endpoint until it responds successfully.

    Connection-level failures (refused connections, DNS errors) are expected
    while the server is starting up and are logged at DEBUG level so the
    polling loop continues without noise. Only when the deadline is exceeded
    does this raise TimeoutError.

    Args:
        url: Full health-check URL, e.g. "http://127.0.0.1:8899/health".
        timeout: Maximum seconds to wait (must be > 0).
        poll_interval: Seconds between polls (must be > 0).

    Raises:
        ValueError: If url is empty, timeout <= 0, or poll_interval <= 0.
        TimeoutError: If the server has not responded within timeout seconds.
    """
    if not url:
        raise ValueError("url must not be empty")
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=SERVER_HEALTH_HTTP_TIMEOUT)
            if response.status_code < 500:
                return
            logger.debug(
                "wait_for_server: %s returned status %d, retrying",
                url,
                response.status_code,
            )
        except httpx.TransportError as exc:
            # Expected during server startup (connection refused, etc.)
            logger.debug("wait_for_server: transport error polling %s: %s", url, exc)
        time.sleep(poll_interval)
    raise TimeoutError(f"Server at {url!r} did not become ready within {timeout}s")
