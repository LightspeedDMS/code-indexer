"""
AC3: Both providers dead — graceful empty result.

Installs kill profiles (error_rate=1.0, error_codes=[503]) on BOTH
api.voyageai.com and api.cohere.com and asserts that MCP search_code with
query_strategy="parallel" degrades gracefully:
  - MCP call returns HTTP 200 (no transport failure)
  - No JSON-RPC protocol error in the envelope
  - result["success"] is True — server handled the failure gracefully
  - result["results"]["results"] is empty (both providers failed, no results)
  - GET /health returns < SERVER_ERROR_THRESHOLD (server still alive)

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy runs both providers concurrently via
RRF coalescing — when both are dead the coalescing produces an empty result
set rather than surfacing a provider error.  The CLI path (/api/query/multi)
always uses primary_only which propagates the error; MCP parallel is the
path that delivers graceful degradation per epic #485.

Target hostnames are fault-transport protocol constants, not environment config.

MCP result shape (from search.py _search_global_repo):
  envelope: {"jsonrpc": "2.0", "result": <handler_result>, "id": 1}
  handler_result: {
    "success": True,
    "results": {
      "results": [],          # empty when both providers fail gracefully
      "total_results": 0,
      "query_metadata": {...}
    }
  }

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

See:
  https://github.com/LightspeedDMS/code-indexer/issues/485 (epic design)
  https://github.com/LightspeedDMS/code-indexer/issues/866 (AC3)
"""

from __future__ import annotations

import httpx

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _mcp_search

# Fault-transport protocol constants — not environment-specific configuration.
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies or helpers.
# ---------------------------------------------------------------------------
KILL_ERROR_RATE: float = 1.0  # 100% interception rate for kill profiles
KILL_ERROR_CODE: int = 503  # HTTP status the fault harness injects
HTTP_OK: int = 200  # Expected status for successful profile GET
HTTP_CREATED: int = 201  # Accepted status for profile PUT (create)
SEARCH_LIMIT: int = 10  # Result limit for MCP search calls
SERVER_ERROR_THRESHOLD: int = 500  # GET /health must return below this


def _install_kill_profile(client: FaultAdminClient, target: str) -> None:
    """Install a 100% error-rate kill profile on *target* and verify it persisted."""
    payload = {
        "target": target,
        "enabled": True,
        "error_rate": KILL_ERROR_RATE,
        "error_codes": [KILL_ERROR_CODE],
    }
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (HTTP_OK, HTTP_CREATED), (
        f"PUT kill profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert (
        get_resp.status_code == HTTP_OK
        and get_resp.json()["error_rate"] == KILL_ERROR_RATE
    ), f"Kill profile for {target!r} not persisted: {get_resp.text}"


def test_both_providers_dead_graceful_empty(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """AC3: With both providers killed, MCP parallel query degrades gracefully.

    Drives the query through MCP search_code with query_strategy="parallel"
    so RRF coalescing handles both providers failing — producing empty results
    instead of surfacing a provider error to the client.

    Assertions:
      1. Both kill profile CRUDs succeed (PUT + GET verified for each).
      2. MCP search_code returns HTTP 200 with no JSON-RPC protocol error.
      3. result["success"] is True — server handled dual failure gracefully.
      4. result["results"]["results"] is empty (both providers failed).
      5. GET /health returns < SERVER_ERROR_THRESHOLD (server alive).
    """
    _install_kill_profile(fault_admin_client, VOYAGE_TARGET)
    _install_kill_profile(fault_admin_client, COHERE_TARGET)

    # Server stores golden repos with a '-global' suffix; the fixture returns
    # the bare alias ("markupsafe"), so we must append it here.
    repo_alias = f"{indexed_golden_repo}-global"
    result_body = _mcp_search(
        fault_admin_client,
        query_text="escape",
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=SEARCH_LIMIT,
    )

    # AC3: parallel strategy must not surface a provider error to the client.
    # With both providers killed, the coalescer produces an empty result set.
    assert result_body.get("success") is True, (
        f"MCP search_code returned success=False with both providers killed — "
        f"expected graceful empty degradation, not a provider error. "
        f"result_body: {result_body}"
    )
    results_wrapper = result_body.get("results", {})
    items = results_wrapper.get("results", [])
    assert items == [], (
        f"MCP search_code returned non-empty results with both providers killed: "
        f"{items}. Expected empty list under graceful degradation."
    )

    # Server must remain alive after both kill profiles are installed.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code} after both kill profiles "
        f"installed; server must survive fault profile installation."
    )
