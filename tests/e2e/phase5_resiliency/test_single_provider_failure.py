"""
AC1: VoyageAI dead — Cohere delivers.
AC2: Cohere dead — VoyageAI delivers (symmetry).

Both ACs are exercised by a single parametrized test that installs a kill
profile (error_rate=1.0, error_codes=[503]) on one provider and asserts that
MCP search_code with query_strategy="parallel" returns results from the
surviving provider.

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy runs both providers concurrently and
fuses results via RRF coalescing — so killing one provider still returns the
surviving provider's results.  The CLI path (/api/query/multi) always uses
primary_only and has no failover; driving through MCP is the only way to
exercise the resiliency behavior promised by epic #485.

Target hostnames (VOYAGE_TARGET, COHERE_TARGET) are fault-transport protocol
constants — they match the httpx transport-layer interception targets and are
not environment-specific configuration values.

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
The markupsafe repo contains only .py files, so checking for ".py" in a
file_path is a structural check appropriate for the indexed golden repo.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

See:
  https://github.com/LightspeedDMS/code-indexer/issues/485 (epic design)
  https://github.com/LightspeedDMS/code-indexer/issues/866 (AC1/AC2)
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _mcp_search

# ---------------------------------------------------------------------------
# Fault-transport protocol constants.
# These are the exact hostnames the fault harness intercepts at the httpx
# transport layer. They are not environment-specific configuration.
# ---------------------------------------------------------------------------
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies or helpers.
# ---------------------------------------------------------------------------
KILL_ERROR_RATE: float = 1.0  # 100% interception rate for kill profiles
KILL_ERROR_CODE: int = 503  # HTTP status the fault harness injects
HTTP_OK: int = 200  # Expected status for successful profile GET
HTTP_CREATED: int = 201  # Accepted status for profile PUT (create)
SEARCH_LIMIT: int = 10  # Default result limit for MCP search calls
SERVER_ERROR_THRESHOLD: int = 500  # GET /health must return below this
RESULT_PREVIEW_COUNT: int = 5  # Number of result items to include in diagnostics


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
    ), f"Kill profile for {target!r} not persisted correctly: {get_resp.text}"


def _assert_results_have_py_file(
    result_body: dict, repo_alias: str, surviving_label: str
) -> None:
    """Assert the MCP result body contains at least one result with a .py file_path.

    result_body is the handler result dict (envelope["result"]):
      {"success": True, "results": {"results": [...], ...}}
    Each result item has a "file_path" key. The markupsafe repo contains only
    Python files, so any valid result must have at least one ".py" path.
    surviving_label names the provider expected to have delivered results.
    """
    assert result_body.get("success") is True, (
        f"MCP search_code returned success=False for repo '{repo_alias}' — "
        f"expected {surviving_label} to deliver results: {result_body}"
    )
    results_wrapper = result_body.get("results", {})
    items = results_wrapper.get("results", [])
    assert items, (
        f"MCP search_code returned 0 results for repo '{repo_alias}' — "
        f"{surviving_label} must deliver results under parallel strategy. "
        f"result_body: {result_body}"
    )
    has_py = any(".py" in (item.get("file_path") or "") for item in items)
    assert has_py, (
        f"MCP search_code results for '{repo_alias}' have no .py file_path "
        f"(expected {surviving_label} to deliver .py results). "
        f"First {RESULT_PREVIEW_COUNT} items: {items[:RESULT_PREVIEW_COUNT]}"
    )


@pytest.mark.parametrize(
    "killed_target, surviving_label",
    [
        pytest.param(VOYAGE_TARGET, "Cohere", id="AC1-voyage-dead-cohere-delivers"),
        pytest.param(COHERE_TARGET, "VoyageAI", id="AC2-cohere-dead-voyage-delivers"),
    ],
)
def test_single_provider_dead_surviving_delivers(
    killed_target: str,
    surviving_label: str,
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """Parametrized AC1+AC2: killing one provider must not prevent results.

    Drives queries through MCP search_code with query_strategy="parallel" so
    that RRF coalescing uses both providers concurrently — one dead provider
    still returns the surviving provider's results.

    Assertions:
      1. Kill profile CRUD round-trip (PUT + GET verified).
      2. MCP search_code returns HTTP 200 with no JSON-RPC error.
      3. result["success"] is True — surviving_label provider delivered.
      4. At least one result item has a .py file_path.
      5. GET /health returns < SERVER_ERROR_THRESHOLD (server survives).
    """
    _install_kill_profile(fault_admin_client, killed_target)

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

    _assert_results_have_py_file(result_body, repo_alias, surviving_label)

    # Server must remain alive after a kill profile is installed.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code} after installing kill profile "
        f"for {killed_target!r}; server must survive fault profile installation."
    )
