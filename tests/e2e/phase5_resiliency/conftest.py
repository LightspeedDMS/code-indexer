"""
Shared pytest fixtures for Phase 5 resiliency E2E tests.

Session-scoped fixtures:
  fault_http_client      -- unauthenticated httpx.Client bound to the fault server
  fault_admin_client     -- FaultAdminClient with .re_login() for per-test token refresh
  fault_workspace        -- temp dir with `cidx init --remote` pointing at fault server
  indexed_golden_repo    -- markupsafe registered + indexed + activated on fault server

Function-scoped autouse fixture:
  clear_all_faults       -- re-logs in + POST /admin/fault-injection/reset before each test

All configuration is read from environment variables set by e2e-automation.sh --phase 5.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Generator

import httpx
import pytest

from tests.e2e.helpers import (
    GIT_SUBPROCESS_TIMEOUT,
    _auth_headers,  # builds pre-assembled Authorization header dict (helpers owns assembly)
    login,
    run_cidx,
    wait_for_job,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (no magic numbers in fixture bodies)
# ---------------------------------------------------------------------------

# Default timeout for dual-provider golden repo indexing.  900 s gives coarse
# headroom for: dual-provider indexing (Voyage + Cohere) + Claude-CLI
# meta-description hook (variable-latency, 30-90 s on top of indexing).
# This is fixture setup, not a test target — bias toward not flaking.
DEFAULT_GOLDEN_REPO_TIMEOUT_SECONDS: float = 900.0

# Poll interval when waiting for background indexing jobs
JOB_POLL_INTERVAL_SECONDS: float = 2.0

# HTTP client timeout for test requests.  Fault-injected parallel-strategy queries
# must wait for one provider to fail before fusion completes; 60 s gives ample
# headroom (actual latency: 200-400 ms × intercepted calls + RRF coalescing +
# retries) while still surfacing a genuinely hung server.
PHASE5_HTTP_CLIENT_TIMEOUT_SECONDS: float = 60.0

# Expected HTTP status code for a successful fault reset
FAULT_RESET_OK: int = 200


# ---------------------------------------------------------------------------
# Environment variable readers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required Phase 5 env var {name!r} is not set. "
            "Run via ./e2e-automation.sh --phase 5."
        )
    return value


def _optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# CLI environment builder — intended single source of truth for subprocess env.
#
# New Phase 5 test files import _build_cli_env from this module (MESSI rule 4:
# anti-duplication).  One legacy local copy (_build_test_cli_env) remains in
# test_positive_control.py and is pending cleanup in a follow-up.
# ---------------------------------------------------------------------------


def _build_cli_env() -> dict[str, str]:
    """Return an environment dict suitable for cidx CLI subprocess invocations.

    Sets PYTHONPATH to include the project src/ directory.
    Sets VOYAGE_API_KEY from E2E_VOYAGE_API_KEY or the ambient env.
    """
    src_dir = str(Path(__file__).parent.parent.parent.parent / "src")
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{src_dir}:{existing}" if existing else src_dir

    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath

    voyage_api_key = _optional_env("E2E_VOYAGE_API_KEY") or _optional_env("VOYAGE_API_KEY")
    if voyage_api_key:
        env["VOYAGE_API_KEY"] = voyage_api_key

    return env


# ---------------------------------------------------------------------------
# FaultAdminClient — authenticated client with per-test token refresh
# ---------------------------------------------------------------------------


class FaultAdminClient:
    """HTTP client bound to the fault server with admin auth and token refresh.

    Stores pre-built Authorization headers produced by helpers._auth_headers
    (all Bearer assembly happens in helpers.py, not here).  re_login() refreshes
    the stored headers by calling login() + _auth_headers() in helpers.

    Individual test methods call .get(), .put(), .patch(), .delete(), .post()
    with the pre-built auth headers automatically merged into each request.
    """

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url
        self._username = username
        self._password = password
        token = login(base_url, username, password)
        # _auth_headers() in helpers.py owns all Bearer assembly — we store result verbatim
        self._headers: dict[str, str] = _auth_headers(token)
        self._token = token
        self._client = httpx.Client(base_url=base_url, timeout=PHASE5_HTTP_CLIENT_TIMEOUT_SECONDS)

    @property
    def token(self) -> str:
        """Return the current active bearer token (public)."""
        return self._token

    def re_login(self) -> None:
        """Re-authenticate and refresh the pre-built Authorization headers."""
        self._token = login(self._base_url, self._username, self._password)
        # helpers._auth_headers owns Bearer assembly; conftest stores result verbatim
        self._headers = _auth_headers(self._token)

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        return self._client.get(path, headers=self._headers, **kwargs)

    def put(self, path: str, **kwargs: object) -> httpx.Response:
        return self._client.put(path, headers=self._headers, **kwargs)

    def patch(self, path: str, **kwargs: object) -> httpx.Response:
        return self._client.patch(path, headers=self._headers, **kwargs)

    def delete(self, path: str, **kwargs: object) -> httpx.Response:
        return self._client.delete(path, headers=self._headers, **kwargs)

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        return self._client.post(path, headers=self._headers, **kwargs)

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# fault_http_client (session) — unauthenticated; used for auth-enforcement tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fault_http_client() -> Generator[httpx.Client, None, None]:
    """Yield an unauthenticated httpx.Client bound to the fault server.

    No bearer token is attached.  Tests that verify auth enforcement (AC4)
    use this fixture to send requests without credentials.
    """
    host = _require_env("E2E_FAULT_SERVER_HOST")
    port = _require_env("E2E_FAULT_SERVER_PORT")
    base_url = f"http://{host}:{port}"
    with httpx.Client(base_url=base_url, timeout=PHASE5_HTTP_CLIENT_TIMEOUT_SECONDS) as client:
        yield client


# ---------------------------------------------------------------------------
# fault_admin_client (session) — authenticated, supports re_login()
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fault_admin_client() -> Generator[FaultAdminClient, None, None]:
    """Yield a FaultAdminClient authenticated as the admin user.

    Session-scoped so the client lives for the whole test session.
    Individual tests must NOT cache the token — they rely on clear_all_faults
    to call re_login() before each test body runs.
    """
    host = _require_env("E2E_FAULT_SERVER_HOST")
    port = _require_env("E2E_FAULT_SERVER_PORT")
    username = _require_env("E2E_ADMIN_USER")
    password = _require_env("E2E_ADMIN_PASS")
    base_url = f"http://{host}:{port}"

    client = FaultAdminClient(base_url, username, password)
    try:
        yield client
    finally:
        client.close()


# ---------------------------------------------------------------------------
# fault_workspace (session, AC3) — authenticated CLI workspace on fault server
# ---------------------------------------------------------------------------


def _init_git_workspace(workspace: Path, seed_path: Path) -> None:
    """Clone seed_path into workspace so `cidx init --remote` succeeds.

    Without a valid git HEAD (non-unborn branch), cidx init --remote fails
    with BranchMatchingError.  Mirrors the pattern in cli_remote/conftest.py.
    """
    cwd = str(workspace)
    subprocess.run(
        ["git", "clone", str(seed_path), "."],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )

    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
    )
    if not branch_check.stdout.strip():
        remote_refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_SUBPROCESS_TIMEOUT,
        )
        branches = [
            b.strip()
            for b in remote_refs.stdout.splitlines()
            if b.strip() and not b.strip().endswith("/HEAD")
        ]
        if not branches:
            raise RuntimeError(
                f"git clone of {seed_path} left workspace in detached HEAD "
                "with no remote tracking branches."
            )
        local_name = branches[0].split("/", 1)[-1]
        subprocess.run(
            ["git", "checkout", "-b", local_name, "--track", branches[0]],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=GIT_SUBPROCESS_TIMEOUT,
        )

    subprocess.run(
        ["git", "config", "user.name", "CIDX Phase5 E2E"],
        cwd=cwd,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "e2e-phase5@cidx.test"],
        cwd=cwd,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    with open(workspace / ".gitignore", "a", encoding="utf-8") as fh:
        fh.write("\n.code-indexer/\n")


@pytest.fixture(scope="session")
def fault_workspace(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[Path, None, None]:
    """Yield a temp dir initialised with `cidx init --remote` against the fault server.

    The workspace is a git clone of the markupsafe seed repo so that
    cidx init --remote succeeds (branch matching requires a non-unborn HEAD).

    Teardown removes the temp directory.  ignore_errors=True is intentional:
    test cleanup must not fail the test suite if the OS holds file handles
    (e.g., the cidx daemon socket or sqlite journal files on Linux).
    """
    host = _require_env("E2E_FAULT_SERVER_HOST")
    port = _require_env("E2E_FAULT_SERVER_PORT")
    admin_user = _require_env("E2E_ADMIN_USER")
    admin_pass = _require_env("E2E_ADMIN_PASS")
    seed_cache_dir = Path(_require_env("E2E_SEED_CACHE_DIR"))
    fault_server_url = f"http://{host}:{port}"

    workspace = tmp_path_factory.mktemp("fault_workspace", numbered=False)
    try:
        _init_git_workspace(workspace, seed_path=seed_cache_dir / "markupsafe")

        init_result = run_cidx(
            "init",
            "--remote",
            fault_server_url,
            "--username",
            admin_user,
            "--password",
            admin_pass,
            cwd=str(workspace),
            env=_build_cli_env(),
        )
        assert init_result.returncode == 0, (
            f"cidx init --remote against fault server failed "
            f"(rc={init_result.returncode}):\n"
            f"stdout: {init_result.stdout}\nstderr: {init_result.stderr}"
        )

        yield workspace
    finally:
        # ignore_errors=True: test cleanup must not fail the suite if the OS
        # holds open file handles (e.g., cidx daemon socket on Linux).
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# indexed_golden_repo (session, AC2) — markupsafe indexed on fault server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def indexed_golden_repo(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    fault_workspace: Path,
) -> str:
    """Register + index markupsafe on the fault server; activate in fault_workspace.

    Returns the alias string ("markupsafe").

    Timeout respects E2E_FAULT_GOLDEN_REPO_JOB_TIMEOUT (default 900 s) because
    dual-provider indexing (VoyageAI + Cohere) plus the Claude-CLI meta-description
    hook (variable-latency) push total job time well past the single-provider baseline.
    """
    seed_cache_dir = Path(_require_env("E2E_SEED_CACHE_DIR"))
    repo_path = str(seed_cache_dir / "markupsafe")
    alias = "markupsafe"
    job_timeout = float(
        _optional_env(
            "E2E_FAULT_GOLDEN_REPO_JOB_TIMEOUT",
            str(DEFAULT_GOLDEN_REPO_TIMEOUT_SECONDS),
        )
    )

    register_resp = fault_admin_client.post(
        "/api/admin/golden-repos",
        json={"repo_url": repo_path, "alias": alias},
    )
    register_resp.raise_for_status()
    job_id: str = register_resp.json()["job_id"]

    job_status = wait_for_job(
        fault_http_client,
        job_id,
        token=fault_admin_client.token,
        timeout=job_timeout,
        poll_interval=JOB_POLL_INTERVAL_SECONDS,
    )
    assert job_status["status"] == "completed", (
        f"Golden repo registration job did not complete successfully:\n{job_status}"
    )

    activate_result = run_cidx(
        "repos",
        "activate",
        alias,
        cwd=str(fault_workspace),
        env=_build_cli_env(),
    )
    assert activate_result.returncode == 0, (
        f"cidx repos activate failed (rc={activate_result.returncode}):\n"
        f"stdout: {activate_result.stdout}\nstderr: {activate_result.stderr}"
    )

    return alias


# ---------------------------------------------------------------------------
# clear_all_faults (autouse, function, AC5) — per-test state reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_all_faults(fault_admin_client: FaultAdminClient) -> Generator[None, None, None]:
    """Re-login and reset all fault injection state before each test.

    Called BEFORE the test body runs (setup phase of the fixture) so that
    every test observes a clean baseline with no profiles, history, or sinbin state.

    Re-login defeats the 10-minute JWT TTL documented in CLAUDE.md.
    On reset failure the error surfaces immediately — no masking.

    Phase 5 must run single-worker (no pytest-xdist) because POST /reset
    is global: parallel workers would interfere with each other via this fixture.

    Full health state isolation (Bug #902 root-cause fix):
    Three resets are required in sequence:

    1. POST /admin/fault-injection/reset — clears fault profiles and history.

    2. POST /admin/provider-health/clear-sinbin — clears sinbin cooldown timers so
       is_sinbinned() returns False.  This was the original (incomplete) fix.

    3. POST /admin/provider-health/reset-state — wipes ALL rolling health state:
       _metrics (error_rate=1.0), _consecutive_failures, _sinbin_failure_deque,
       _last_known_status, and stops active recovery probes.

       This is the Bug #902 root-cause fix.  The pre-skip gate in
       semantic_query_manager checks BOTH is_sinbinned() AND
       _compute_status().status == "down".  After sinbin-only clear, _metrics
       retained error_rate=1.0 causing _compute_status() to still return "down",
       so providers were pre-skipped even though sinbin timers were gone.
    """
    fault_admin_client.re_login()
    reset_resp = fault_admin_client.post("/admin/fault-injection/reset")
    if reset_resp.status_code != FAULT_RESET_OK:
        raise RuntimeError(
            f"clear_all_faults: POST /admin/fault-injection/reset failed "
            f"(status={reset_resp.status_code}): {reset_resp.text}"
        )
    sinbin_resp = fault_admin_client.post(
        "/admin/provider-health/clear-sinbin", json={}
    )
    if sinbin_resp.status_code != FAULT_RESET_OK:
        raise RuntimeError(
            f"clear_all_faults: POST /admin/provider-health/clear-sinbin failed "
            f"(status={sinbin_resp.status_code}): {sinbin_resp.text}"
        )
    state_resp = fault_admin_client.post("/admin/provider-health/reset-state")
    if state_resp.status_code != FAULT_RESET_OK:
        raise RuntimeError(
            f"clear_all_faults: POST /admin/provider-health/reset-state failed "
            f"(status={state_resp.status_code}): {state_resp.text}"
        )
    yield


# ---------------------------------------------------------------------------
# _mcp_search — shared MCP search_code helper for Phase 5 resiliency tests
# ---------------------------------------------------------------------------


def _mcp_search(
    fault_admin_client: "FaultAdminClient",
    query_text: str,
    repository_alias: str,
    query_strategy: str = "parallel",
    limit: int = 10,
    **extra_args: object,
) -> dict:
    """POST MCP tools/call search_code and return the unwrapped result body.

    Sends a JSON-RPC 2.0 request to /mcp with the given arguments and
    query_strategy (default "parallel" — exercises RRF coalescing across
    all configured providers so one dead provider still returns surviving
    results).

    Uses fault_admin_client.post() which carries pre-built Authorization
    headers — no manual Bearer assembly here.

    Returns the handler result dict (i.e. envelope["result"] from the
    JSON-RPC response, which is {"success": True/False, "results": {...}}).

    Raises:
        ValueError: On invalid arguments (non-str inputs, empty strings,
                    non-int or non-positive limit).
        httpx.HTTPStatusError: If the HTTP transport returns a non-2xx status.
        AssertionError: If the JSON-RPC envelope contains a protocol-level error.

    Does NOT raise if result["success"] is False — callers inspect the body to
    assert per-AC behavior (e.g. empty results on full failure for AC3).
    """
    if not isinstance(query_text, str) or not query_text:
        raise ValueError(f"query_text must be a non-empty str, got {query_text!r}")
    if not isinstance(repository_alias, str) or not repository_alias:
        raise ValueError(
            f"repository_alias must be a non-empty str, got {repository_alias!r}"
        )
    if not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")

    arguments: dict = {
        "query_text": query_text,
        "repository_alias": repository_alias,
        "query_strategy": query_strategy,
        "limit": limit,
        **extra_args,
    }
    response = fault_admin_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_code",
                "arguments": arguments,
            },
        },
    )
    response.raise_for_status()
    envelope = response.json()
    assert "error" not in envelope, (
        f"_mcp_search: JSON-RPC protocol error for "
        f"query_text={query_text!r}, repository_alias={repository_alias!r}: "
        f"{envelope['error']}"
    )
    return envelope["result"]
