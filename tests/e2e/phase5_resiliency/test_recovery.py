"""
AC1 (#867): Recovery after clearing a fault.

Scenario: A VoyageAI kill fault is active; MCP parallel query returns only
the surviving Cohere results — VoyageAI must not appear in contributing_providers.
The fault is cleared via DELETE. MCP parallel query returns results where
VoyageAI is present in contributing_providers, confirming both providers are
active again.

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy runs both providers concurrently via
RRF coalescing — killing VoyageAI removes it from the contributing_providers
field of fused results; after DELETE, VoyageAI re-appears in contributing_providers.
The CLI path (/api/query/multi) always uses primary_only and surfaces the provider
error rather than degrading gracefully; MCP parallel is the correct path for
demonstrating recovery semantics per epic #485.

Note on count-based assertions: both providers embed the same chunks from the
same small repo, so the dual-provider RRF result count equals the single-provider
count. Provider composition (contributing_providers) is the correct recovery
signal — it proves VoyageAI is actually contributing, not just that results exist.

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
SEARCH_LIMIT: int = 50                # Result limit — large enough that dual-provider RRF can widen past single-provider cap
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


def _get_results(result_body: dict) -> list:
    """Extract result items from MCP search result body.

    result_body is envelope["result"]: {"success": True, "results": {"results": [...]}}
    Returns empty list if success is False or results list is absent.
    """
    if not result_body.get("success"):
        return []
    return result_body.get("results", {}).get("results", [])


def _providers_in_results(items: list) -> set:
    """Collect all provider names from contributing_providers across all result items.

    Each item may have a 'contributing_providers' list (set by RRF fusion when
    both providers are alive) or a 'source_provider' string (when only one
    provider contributed, e.g. faulted state).  Both fields are checked.
    """
    providers: set = set()
    for item in items:
        cp = item.get("contributing_providers")
        if isinstance(cp, list):
            providers.update(cp)
        sp = item.get("source_provider")
        if sp and isinstance(sp, str):
            providers.add(sp)
    return providers


VOYAGE_PROVIDER = "voyage-ai"
RESULT_PREVIEW_COUNT: int = 3  # items shown in assertion messages
HEALTH_RESET_OK: int = 200     # expected status for POST /admin/provider-health/reset-state


def _reset_provider_health(client: FaultAdminClient) -> None:
    """POST /admin/provider-health/reset-state to wipe accumulated failure state.

    Required after deleting a kill profile so that the parallel dispatch no longer
    skips providers whose 'down' status was accumulated during the fault phase.
    Without this, VoyageAI remains skipped by dispatch even after the fault
    profile is gone because consecutive_failures >= 5 still marks it 'down'.
    """
    resp = client.post("/admin/provider-health/reset-state")
    assert resp.status_code == HEALTH_RESET_OK, (
        f"POST /admin/provider-health/reset-state failed: "
        f"{resp.status_code} {resp.text}"
    )


def _search_and_extract_providers(
    client: FaultAdminClient,
    repo_alias: str,
) -> tuple:
    """Run MCP parallel search and return (items, providers_set).

    Items is the list of result dicts; providers_set is the union of all
    source_provider and contributing_providers values across all items.
    """
    result_body = _mcp_search(
        client,
        query_text="escape",
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=SEARCH_LIMIT,
    )
    assert result_body.get("success") is True, (
        f"_search_and_extract_providers: success=False. body: {result_body}"
    )
    items = _get_results(result_body)
    return items, _providers_in_results(items)


def _assert_provider_contribution(
    providers: set, items: list, provider_name: str, expected_present: bool
) -> None:
    """Assert provider_name is present or absent in providers.

    expected_present=True: provider must appear (recovery confirmed).
    expected_present=False: provider must not appear (fault isolation confirmed).
    """
    if expected_present:
        assert provider_name in providers, (
            f"{provider_name} must appear in contributing_providers after recovery. "
            f"providers: {providers}. "
            f"First {RESULT_PREVIEW_COUNT} items: {items[:RESULT_PREVIEW_COUNT]}"
        )
    else:
        assert provider_name not in providers, (
            f"{provider_name} must NOT appear in contributing_providers under kill profile. "
            f"providers: {providers}. "
            f"First {RESULT_PREVIEW_COUNT} items: {items[:RESULT_PREVIEW_COUNT]}"
        )


def test_recovery_after_delete_restores_results(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """AC1: After DELETE of kill profile, VoyageAI must contribute to results again.

    Assertions:
      1. Kill profile CRUD round-trip (PUT + GET verified).
      2. Faulted query: VoyageAI absent from all contributing_providers.
      3. DELETE profile succeeds; GET returns 404.
      4. Recovered query: VoyageAI present in contributing_providers.
      5. GET /health < SERVER_ERROR_THRESHOLD (server survives throughout).
    """
    repo_alias = f"{indexed_golden_repo}-global"

    # Assertion 1: install kill profile and verify persistence.
    _install_kill_profile(fault_admin_client, VOYAGE_TARGET)

    # Assertion 2: faulted query — VoyageAI blocked, must not contribute.
    faulted_items, faulted_providers = _search_and_extract_providers(
        fault_admin_client, repo_alias
    )
    assert faulted_items, "Faulted query must return at least one result via Cohere."
    _assert_provider_contribution(
        faulted_providers, faulted_items, VOYAGE_PROVIDER, expected_present=False
    )

    # Assertion 3: delete profile and verify it is gone.
    _delete_profile(fault_admin_client, VOYAGE_TARGET)
    # Reset accumulated "down" health state so dispatch no longer skips VoyageAI.
    _reset_provider_health(fault_admin_client)

    # Assertion 4: recovered query — VoyageAI must contribute again.
    recovered_items, recovered_providers = _search_and_extract_providers(
        fault_admin_client, repo_alias
    )
    assert recovered_items, "Recovered query must return at least one result."
    _assert_provider_contribution(
        recovered_providers, recovered_items, VOYAGE_PROVIDER, expected_present=True
    )

    # Assertion 5: server must be alive throughout.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code}; "
        "server must survive the kill-profile + DELETE cycle."
    )
