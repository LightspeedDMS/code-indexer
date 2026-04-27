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

import json
import logging
import subprocess
import time
from pathlib import Path
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

JOB_POLL_HTTP_TIMEOUT: float = 30.0
"""Per-request timeout when polling /jobs/{id}.

30s gives occasional event-loop stalls or connection-pool hiccups room
to recover without failing the whole fixture. Individual polls usually
respond in <100ms; this is a resilience ceiling, not an expected latency.
"""

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

GIT_SUBPROCESS_TIMEOUT: float = 5.0
"""Maximum seconds to wait for a git subprocess call (e.g., git config, git init)."""

CONFLICT_RESOLUTION_TIMEOUT: float = 600.0
"""Maximum seconds to wait for a Claude-assisted conflict resolution refresh job (Story #926 AC8).

Conflict resolution invokes the Claude CLI subprocess which may take several minutes.
"""


# ---------------------------------------------------------------------------
# Git subprocess helper
# ---------------------------------------------------------------------------


def run_git(args: list[str], cwd: "Path") -> str:
    """Run a git command in ``cwd`` and return stdout; raise on invalid input or failure.

    Uses GIT_SUBPROCESS_TIMEOUT as the process deadline.

    Args:
        args: git sub-command and arguments (must not be None and must be non-empty).
        cwd: Working directory (must not be None and must exist on disk).

    Returns:
        Stripped stdout from the git process.

    Raises:
        ValueError: If args is None, args is empty, cwd is None, or cwd does not exist.
        RuntimeError: If the git process exits with a non-zero code.
    """
    if args is None:
        raise ValueError("run_git: args must not be None")
    if not args:
        raise ValueError("run_git: args must be a non-empty list")
    if cwd is None:
        raise ValueError("run_git: cwd must not be None")
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        raise ValueError(f"run_git: cwd does not exist: {cwd_path}")
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd_path),
        capture_output=True,
        text=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd_path}:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# JSON section field patcher
# ---------------------------------------------------------------------------


def patch_json_field(
    base_dir: "Path",
    json_file: "Path",
    section: str,
    field: str,
    value: str,
) -> None:
    """Read a JSON file, merge one field into a named section dict, and write back.

    Validates that ``json_file`` is located within ``base_dir`` (using
    ``Path.resolve()`` + ``relative_to``) to prevent accidental writes outside
    the intended directory.

    Args:
        base_dir: Trusted root directory (must not be None).
        json_file: Path to the JSON file (created as empty dict if absent);
                   must not be None and must be inside ``base_dir``.
        section: Top-level key in the JSON object (must not be None or empty).
        field: Key inside ``section`` to set (must not be None or empty).
        value: String value to assign to ``field`` (must not be None).

    Raises:
        ValueError: If any required parameter is None or empty, or if
                    json_file resolves outside base_dir.
    """
    if base_dir is None:
        raise ValueError("patch_json_field: base_dir must not be None")
    if json_file is None:
        raise ValueError("patch_json_field: json_file must not be None")
    if section is None:
        raise ValueError("patch_json_field: section must not be None")
    if not section:
        raise ValueError("patch_json_field: section must be a non-empty string")
    if field is None:
        raise ValueError("patch_json_field: field must not be None")
    if not field:
        raise ValueError("patch_json_field: field must be a non-empty string")
    if value is None:
        raise ValueError("patch_json_field: value must not be None")

    resolved_base = Path(base_dir).resolve()
    resolved_file = Path(json_file).resolve()
    try:
        resolved_file.relative_to(resolved_base)
    except ValueError:
        raise ValueError(
            f"patch_json_field: json_file {json_file} is not inside base_dir {base_dir}"
        )

    data: dict = json.loads(resolved_file.read_text()) if resolved_file.exists() else {}
    section_data: dict = data.get(section, {})
    if not isinstance(section_data, dict):
        section_data = {}
    section_data[field] = value
    data[section] = section_data
    resolved_file.write_text(json.dumps(data, indent=2))


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
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    stdin_input: str | None = None,
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
        stdin_input: Optional text fed to the subprocess on stdin. Used to
            answer interactive prompts (e.g. "y\\n" for a y/N confirmation).

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
        input=stdin_input,
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
    token = response.json()["access_token"]
    assert isinstance(token, str), (
        f"access_token must be str, got {type(token).__name__}"
    )
    return token


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
        try:
            response = rest_call(
                client,
                "GET",
                f"/api/jobs/{job_id}",
                token,
                timeout=JOB_POLL_HTTP_TIMEOUT,
            )
            response.raise_for_status()
            status_data: dict[str, Any] = response.json()
            if status_data.get("status") in terminal_states:
                return status_data
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            logger.warning(
                "wait_for_job: transient poll timeout for job %s (%s); retrying",
                job_id,
                exc.__class__.__name__,
            )
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Job {job_id!r} did not reach a terminal state within {timeout}s"
    )


def wait_for_repo_activation(
    client: httpx.Client,
    alias: str,
    token: str | None = None,
    *,
    timeout: float = 90.0,
    poll_interval: float = JOB_POLL_INTERVAL,
) -> None:
    """Poll GET /api/repos/<alias> until the repo shows as activated (HTTP 200).

    After ``cidx repos activate`` returns rc=0 the activation job may still
    be running server-side.  This helper blocks until the repo appears in the
    user's activated repository list (200) or raises TimeoutError.

    Args:
        client: Shared httpx.Client bound to the server base URL.
        alias: User-facing alias of the activated repository to wait for.
        token: JWT access token.
        timeout: Maximum seconds to wait (must be > 0).
        poll_interval: Seconds between polls (must be > 0).

    Raises:
        ValueError: If alias is empty, timeout <= 0, or poll_interval <= 0.
        TimeoutError: If the repo is not activated within timeout seconds.
    """
    if not alias:
        raise ValueError("alias must not be empty")
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.request(
            "GET",
            f"/api/repos/{alias}",
            headers=_auth_headers(token),
            timeout=JOB_POLL_HTTP_TIMEOUT,
        )
        if response.status_code == 200:
            return
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Repository '{alias}' did not appear as activated within {timeout}s"
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


# ---------------------------------------------------------------------------
# cidx-meta backup config toggle (Story #926)
# ---------------------------------------------------------------------------


def toggle_cidx_meta_backup(
    client: "httpx.Client",
    token: str,
    *,
    enabled: bool,
    remote_url: str,
) -> None:
    """Enable or disable cidx-meta backup via the admin config endpoint.

    Args:
        client: Shared httpx.Client bound to the server base URL.
        token: JWT access token (must not be None or empty).
        enabled: True to enable backup, False to disable.
        remote_url: Remote git URL for backup; may be empty when disabling
                    (must not be None).

    Raises:
        ValueError: If client, token, or remote_url is None, or token is empty.
        httpx.HTTPStatusError: If the server returns a non-2xx response.
    """
    if client is None:
        raise ValueError("toggle_cidx_meta_backup: client must not be None")
    if token is None:
        raise ValueError("toggle_cidx_meta_backup: token must not be None")
    if not token:
        raise ValueError("toggle_cidx_meta_backup: token must not be empty")
    if remote_url is None:
        raise ValueError("toggle_cidx_meta_backup: remote_url must not be None")

    resp = rest_call(
        client,
        "POST",
        "/admin/config/cidx_meta_backup",
        token,
        data={
            "cidx_meta_backup_enabled": "true" if enabled else "false",
            "cidx_meta_backup_remote_url": remote_url,
        },
    )
    resp.raise_for_status()
