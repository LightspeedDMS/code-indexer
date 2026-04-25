"""
AC1 (#867): Recovery after clearing a fault.

Scenario: A VoyageAI kill fault is active; MCP parallel query returns only
the surviving Cohere results (narrower set). The fault is cleared via DELETE.
MCP parallel query returns the full RRF-coalesced set from both providers
(wider set). Asserts count_recovered > count_faulted.

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy runs both providers concurrently via
RRF coalescing — killing VoyageAI narrows the result set to Cohere-only;
after DELETE the full dual-provider set is restored.  The CLI path
(/api/query/multi) always uses primary_only and surfaces the provider error
rather than degrading gracefully; MCP parallel is the correct path for
demonstrating recovery semantics per epic #485.

Target hostnames (VOYAGE_TARGET) are fault-transport protocol constants —
they match the httpx transport-layer interception targets and are not
environment-specific configuration values.

MCP result shape (from search.py _search_global_repo):
  envelope: {"jsonrpc": "2.0", "result": <handler_result>, "id": 1}
  handler_result: {
    "success": True,
    "results": {
      "results": [{"file_path": "...", "score": ..., ...}, ...],
      "total_results": N,
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
  https://github.com/LightspeedDMS/code-indexer/issues/867 (AC1)
"""

from __future__ import annotations

import httpx

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _mcp_search

# Fault-transport protocol constant — not environment-specific configuration.
VOYAGE_TARGET = "api.voyageai.com"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies or helpers.
# ---------------------------------------------------------------------------
KILL_ERROR_RATE: float = 1.0          # 100% interception rate for kill profiles
KILL_ERROR_CODE: int = 503            # HTTP status the fault harness injects
HTTP_OK: int = 200                    # Expected status for successful GET/DELETE
HTTP_CREATED: int = 201               # Accepted status for profile PUT (create)
HTTP_NOT_FOUND: int = 404             # Expected status for profile GET after DELETE
SEARCH_LIMIT: int = 10                # Result limit for MCP search calls
SERVER_ERROR_THRESHOLD: int = 500     # GET /health must return below this


def _install_kill_profile(client: FaultAdminClient, target: str) -> None:
    """Install 100% error-rate kill profile on *target* and verify persistence."""
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
    assert get_resp.status_code == HTTP_OK and get_resp.json()["error_rate"] == KILL_ERROR_RATE, (
        f"Kill profile for {target!r} not persisted correctly: {get_resp.text}"
    )


def _delete_profile(client: FaultAdminClient, target: str) -> None:
    """DELETE the fault profile for *target* and verify it returns 404 after."""
    del_resp = client.delete(f"/admin/fault-injection/profiles/{target}")
    assert del_resp.status_code == HTTP_OK, (
        f"DELETE profile for {target!r} failed: "
        f"{del_resp.status_code} {del_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == HTTP_NOT_FOUND, (
        f"GET profile for {target!r} after DELETE expected {HTTP_NOT_FOUND}, "
        f"got {get_resp.status_code}: {get_resp.text}"
    )


def _count_results(result_body: dict) -> int:
    """Count result items in MCP search result body.

    result_body is envelope["result"]: {"success": True, "results": {"results": [...]}}
    Returns 0 if success is False or results list is absent.
    """
    if not result_body.get("success"):
        return 0
    return len(result_body.get("results", {}).get("results", []))


def test_recovery_after_delete_restores_results(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """AC1: After DELETE of a kill profile, MCP parallel result set must widen back.

    Assertions:
      1. Kill profile CRUD round-trip (PUT + GET verified).
      2. MCP parallel query under kill profile returns success=True with a
         narrower result set (Cohere-only, VoyageAI blocked).
      3. DELETE profile succeeds; GET returns 404.
      4. MCP parallel query after DELETE returns a wider result set
         (count_recovered > count_faulted — both providers contributing again).
      5. GET /health returns < SERVER_ERROR_THRESHOLD (server alive throughout).
    """
    # Assertion 1: kill profile CRUD.
    _install_kill_profile(fault_admin_client, VOYAGE_TARGET)

    # Server stores golden repos with a '-global' suffix; fixture returns bare alias.
    repo_alias = f"{indexed_golden_repo}-global"

    # Assertion 2: faulted query — VoyageAI blocked, Cohere delivers.
    result_faulted = _mcp_search(
        fault_admin_client,
        query_text="escape",
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=SEARCH_LIMIT,
    )
    assert result_faulted.get("success") is True, (
        f"MCP search must return success=True under VoyageAI kill profile "
        f"(Cohere still delivers under parallel strategy). "
        f"result_body: {result_faulted}"
    )
    count_faulted = _count_results(result_faulted)

    # Assertion 3: control plane DELETE + subsequent 404.
    _delete_profile(fault_admin_client, VOYAGE_TARGET)

    # Assertion 4: recovered query — both providers contributing.
    result_recovered = _mcp_search(
        fault_admin_client,
        query_text="escape",
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=SEARCH_LIMIT,
    )
    assert result_recovered.get("success") is True, (
        f"MCP search must return success=True after DELETE of kill profile. "
        f"result_body: {result_recovered}"
    )
    count_recovered = _count_results(result_recovered)
    assert count_recovered > count_faulted, (
        f"Expected result set to widen after DELETE (recovery). "
        f"Faulted (Cohere-only): {count_faulted}, "
        f"recovered (both providers): {count_recovered}."
    )

    # Assertion 5: server must be alive throughout.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code} after kill profile + DELETE; "
        f"server must survive the fault profile lifecycle."
    )
