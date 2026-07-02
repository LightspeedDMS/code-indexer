"""
AC4: Provider timeouts and malformed responses.

Two scenarios from the AC4 specification:

  Scenario A — connect-timeout on api.voyageai.com:
    Profile field: connect_timeout_rate=1.0
    Requirement:   cidx query must return within 15 seconds (bounded budget).
                   A hang is a bug. TimeoutExpired = test failure.

  Scenario B — malformed-response on api.cohere.com:
    Profile fields: malformed_rate=1.0, corruption_modes=["wrong_schema"]
    Requirement:   Server must NOT 500 to the client and must NOT crash.
                   stdout and stderr must not contain "500 Internal Server Error".
                   GET /health must return < 500 after the query.

Target hostnames are fault-transport protocol constants, not environment config.

Pydantic field names verified against FaultProfile in fault_profile.py:
  - connect_timeout_rate  (line 76)
  - malformed_rate        (line 80)
  - corruption_modes      (line 81, allowed: truncate/invalid_utf8/wrong_schema/empty)

Both tests use subprocess.run directly (not run_cidx) to allow explicit timeout
control. CONNECT_TIMEOUT_SUBPROCESS_SECONDS is set slightly above the AC4 budget
so TimeoutExpired fires before any test-runner global timeout.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  fault_workspace     -- git-backed workspace with cidx init --remote
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env

# Fault-transport protocol constants — not environment-specific configuration.
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

# AC4 hard budget: query with connect-timeout must return within this many seconds.
CONNECT_TIMEOUT_QUERY_BUDGET_SECONDS: float = 15.0

# Subprocess hard cap: slightly above the budget so TimeoutExpired fires before
# the test runner's own timeout, giving a clear failure message.
CONNECT_TIMEOUT_SUBPROCESS_SECONDS: float = CONNECT_TIMEOUT_QUERY_BUDGET_SECONDS + 5.0


def _cidx_query_cmd(indexed_golden_repo: str, env: dict) -> list:
    """Return the cidx query command list for the given repo alias.

    The server stores golden repos with a '-global' suffix, so the bare alias
    returned by the fixture ("markupsafe") must have it appended here.
    """
    return [
        "python3",
        "-m",
        "code_indexer.cli",
        "query",
        "escape",
        "--repos",
        f"{indexed_golden_repo}-global",
        "--quiet",
    ]


def _install_profile(client: FaultAdminClient, target: str, payload: dict) -> None:
    """PUT a fault profile for *target* with *payload* and verify it was accepted."""
    payload_with_target = dict(payload, target=target)
    put_resp = client.put(
        f"/admin/fault-injection/profiles/{target}", json=payload_with_target
    )
    assert put_resp.status_code in (200, 201), (
        f"PUT fault profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == 200, (
        f"GET profile for {target!r} after PUT failed: {get_resp.status_code}"
    )


def test_connect_timeout_bounded(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC4 Scenario A: connect-timeout on VoyageAI must resolve within 15 seconds.

    Installs connect_timeout_rate=1.0 on api.voyageai.com.
    Uses subprocess.run with a hard timeout of CONNECT_TIMEOUT_SUBPROCESS_SECONDS.
    If the CLI hangs past the cap, TimeoutExpired is caught and the test fails
    with a clear message. If the CLI exits within the cap but beyond the AC4
    budget, elapsed time assertion fails.
    """
    _install_profile(
        fault_admin_client,
        VOYAGE_TARGET,
        {
            "enabled": True,
            "connect_timeout_rate": 1.0,
        },
    )

    env = _build_cli_env()
    cmd = _cidx_query_cmd(indexed_golden_repo, env)

    start = time.monotonic()
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(fault_workspace),
            env=env,
            timeout=CONNECT_TIMEOUT_SUBPROCESS_SECONDS,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        raise AssertionError(
            f"cidx query with connect_timeout on VoyageAI did not terminate within "
            f"{CONNECT_TIMEOUT_SUBPROCESS_SECONDS:.0f}s (elapsed: {elapsed:.1f}s). "
            f"Provider timeout must not hang the client."
        )

    elapsed = time.monotonic() - start
    assert elapsed < CONNECT_TIMEOUT_QUERY_BUDGET_SECONDS, (
        f"cidx query with connect_timeout on VoyageAI took {elapsed:.1f}s "
        f"(AC4 budget: {CONNECT_TIMEOUT_QUERY_BUDGET_SECONDS}s). "
        f"Provider timeout must not block the client beyond the latency budget."
    )


def test_malformed_response_no_server_crash(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC4 Scenario B: malformed response (wrong_schema) on Cohere must not crash server.

    Installs malformed_rate=1.0 with corruption_modes=["wrong_schema"] on
    api.cohere.com. Assertions after the query:
      - stdout and stderr contain no "500 Internal Server Error" text
        (provider malformed response must not be surfaced as server error to client)
      - GET /health returns < 500 (server alive after malformed responses)
    """
    _install_profile(
        fault_admin_client,
        COHERE_TARGET,
        {
            "enabled": True,
            "malformed_rate": 1.0,
            "corruption_modes": ["wrong_schema"],
        },
    )

    env = _build_cli_env()
    cmd = _cidx_query_cmd(indexed_golden_repo, env)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(fault_workspace),
        env=env,
    )

    # Client must not see a 500 error surfaced from the server
    assert "500 Internal Server Error" not in result.stderr, (
        f"cidx query stderr contained '500 Internal Server Error' after malformed "
        f"Cohere response. Provider errors must not reach the client.\n"
        f"stderr:\n{result.stderr[:500]}"
    )
    assert "500 Internal Server Error" not in result.stdout, (
        f"cidx query stdout contained '500 Internal Server Error' after malformed "
        f"Cohere response. Provider errors must not reach the client.\n"
        f"stdout:\n{result.stdout[:500]}"
    )

    # Server must still be alive after receiving malformed responses
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} after malformed Cohere responses; "
        f"server must not crash on provider malformed responses."
    )
