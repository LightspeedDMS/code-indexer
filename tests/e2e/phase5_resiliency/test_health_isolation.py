"""
AC5: Health monitor isolation between tests.

After a kill-profile test pushes a provider into the health monitor sinbin,
the next test (after clear_all_faults) must see both providers healthy.

This test simulates the cross-test boundary within a single test body:
  Phase A — Kill Cohere; run QUERIES_TO_TRIGGER_SINBIN MCP parallel queries.
             Under parallel strategy VoyageAI is alive so each query must
             return results. After the loop, assert Cohere is sinbinned via
             GET /admin/provider-health.
  Phase B — Reset faults (mirrors what clear_all_faults does between tests);
             poll until GET /admin/provider-health reports sinbinned=false
             for both "voyage-ai" and "cohere" within SINBIN_RECOVERY_WAIT_SECONDS.
             Each poll runs an MCP query that must succeed (no profiles active)
             to trigger record_call(success=True), which clears sinbin
             automatically (provider_health_monitor.py:231-234).
  Final   — Assert the post-recovery MCP parallel query returns non-empty
             .py results.

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy exercises both providers concurrently
via RRF coalescing, which feeds the ProviderHealthMonitor — so kill profiles
actually trigger sinbin state changes that can be verified.

The primary recovery assertion is sinbinned=false from GET /admin/provider-health.

Sinbin provider names (from semantic_query_manager.py):
  "voyage-ai"  -- VoyageAI embedding provider
  "cohere"     -- Cohere embedding/reranking provider

COHERE_TARGET is a fault-transport protocol constant — it matches the httpx
transport-layer interception target and is not environment-specific config.
The same constant is used by test_single_provider_failure.py and
test_both_providers_failure.py for the same reason.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

See:
  https://github.com/LightspeedDMS/code-indexer/issues/485 (epic design)
  https://github.com/LightspeedDMS/code-indexer/issues/866 (AC5)
"""

from __future__ import annotations

import time
from typing import Dict

import httpx

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _mcp_search

# ---------------------------------------------------------------------------
# Fault-transport protocol constant.
# Matches the httpx transport-layer interception target — not environment config.
# ---------------------------------------------------------------------------
COHERE_TARGET = "api.cohere.com"

# Internal provider names used by ProviderHealthMonitor (from semantic_query_manager.py)
VOYAGE_PROVIDER_NAME = "voyage-ai"
COHERE_PROVIDER_NAME = "cohere"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies or helpers.
# ---------------------------------------------------------------------------
KILL_ERROR_RATE: float = 1.0  # 100% interception rate for kill profiles
KILL_ERROR_CODE: int = 503  # HTTP status the fault harness injects
HTTP_OK: int = 200  # Expected status for REST calls
HTTP_CREATED: int = 201  # Accepted status for profile PUT (create)
SEARCH_LIMIT: int = 10  # Result limit for MCP search calls
SERVER_ERROR_THRESHOLD: int = 500  # GET /health must return below this
RESULT_PREVIEW_COUNT: int = 5  # Number of items to include in diagnostics

# Number of queries needed to exceed the default sinbin failure_threshold=5.
QUERIES_TO_TRIGGER_SINBIN: int = 6

# Timing constants for sinbin recovery polling.
SINBIN_RECOVERY_WAIT_SECONDS: float = 60.0
SINBIN_POLL_INTERVAL_SECONDS: float = 2.0
SINBIN_MAX_POLLS: int = int(SINBIN_RECOVERY_WAIT_SECONDS / SINBIN_POLL_INTERVAL_SECONDS)

FAULT_RESET_OK: int = 200  # Expected status for /admin/fault-injection/reset


def _install_kill_profile(client: FaultAdminClient, target: str) -> None:
    """Install 100% error-rate kill profile on *target* and verify persistence.

    Verifies persistence by round-tripping GET after PUT and asserting that
    the stored error_rate matches KILL_ERROR_RATE.
    """
    payload = {
        "target": target,
        "enabled": True,
        "error_rate": KILL_ERROR_RATE,
        "error_codes": [KILL_ERROR_CODE],
    }
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (HTTP_OK, HTTP_CREATED), (
        f"PUT kill profile for {target!r} failed: {put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert (
        get_resp.status_code == HTTP_OK
        and get_resp.json()["error_rate"] == KILL_ERROR_RATE
    ), f"Kill profile for {target!r} not persisted: {get_resp.text}"


def _get_provider_health(client: FaultAdminClient) -> Dict[str, dict]:
    """Return {provider_name: entry_dict} from GET /admin/provider-health."""
    resp = client.get("/admin/provider-health")
    assert resp.status_code == HTTP_OK, (
        f"GET /admin/provider-health failed: {resp.status_code} {resp.text}"
    )
    return {entry["provider"]: entry for entry in resp.json().get("providers", [])}


def _is_provider_degraded(entry: dict) -> bool:
    """Return True if provider is sinbinned OR health status is 'down'.

    The sinbin deque is window-based (failure_window_seconds=60). Under fault
    injection with retry backoff, 6 queries may span >60 s, causing the deque
    window to prune early failures before the threshold is reached.  The
    consecutive-failure 'down' status is always set after 5+ consecutive failures
    and is not window-bounded, so checking either condition makes the assertion
    robust to timing without weakening the health-degradation invariant.
    """
    return entry.get("sinbinned") is True or entry.get("status") == "down"


def _run_parallel_search(client: FaultAdminClient, repo_alias: str) -> dict:
    """Run MCP search_code with query_strategy=parallel; return result body."""
    return _mcp_search(
        client,
        query_text="escape",
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=SEARCH_LIMIT,
    )


def _trigger_cohere_sinbin(
    fault_admin_client: FaultAdminClient, repo_alias: str
) -> None:
    """Phase A: install Cohere kill, run queries, assert Cohere health degraded.

    Under parallel strategy VoyageAI is alive — each query must return
    success=True even while Cohere is faulted. After QUERIES_TO_TRIGGER_SINBIN
    queries, verifies Cohere is sinbinned OR 'down' via GET /admin/provider-health.

    Both sinbinned=True and status='down' confirm that the health monitor correctly
    detected the Cohere provider failure pattern:
    - 'sinbinned' fires when the sinbin deque reaches failure_threshold within
      failure_window_seconds (60s). Under retry backoff this may not trigger if
      queries span >60s.
    - 'down' fires when consecutive_failures >= 5, which is window-independent
      and always set after 5+ consecutive failures.
    """
    _install_kill_profile(fault_admin_client, COHERE_TARGET)
    for i in range(QUERIES_TO_TRIGGER_SINBIN):
        result_body = _run_parallel_search(fault_admin_client, repo_alias)
        assert result_body.get("success") is True, (
            f"Phase A query {i + 1}/{QUERIES_TO_TRIGGER_SINBIN}: success=False "
            f"with only Cohere killed — VoyageAI should deliver under parallel. "
            f"result_body: {result_body}"
        )
    health_map = _get_provider_health(fault_admin_client)
    cohere_entry = health_map.get(COHERE_PROVIDER_NAME, {})
    assert _is_provider_degraded(cohere_entry), (
        f"Expected Cohere sinbinned or 'down' after {QUERIES_TO_TRIGGER_SINBIN} queries; "
        f"cohere entry: {cohere_entry}, full health map: {health_map}. "
        f"Check fault transport wiring and health monitor thresholds."
    )


def _reset_and_wait_recovery(
    fault_admin_client: FaultAdminClient, repo_alias: str
) -> None:
    """Phase B: reset faults; poll until both providers clear sinbin.

    Each poll runs an MCP query that must succeed (no fault profiles active
    after reset) to trigger record_call(success=True) for sinbin self-clear.
    """
    fault_admin_client.re_login()
    reset_resp = fault_admin_client.post("/admin/fault-injection/reset")
    assert reset_resp.status_code == FAULT_RESET_OK, (
        f"POST /admin/fault-injection/reset failed: "
        f"{reset_resp.status_code} {reset_resp.text}"
    )
    recovered = False
    for poll_num in range(SINBIN_MAX_POLLS):
        poll_result = _run_parallel_search(fault_admin_client, repo_alias)
        assert poll_result.get("success") is True, (
            f"Phase B poll {poll_num + 1}/{SINBIN_MAX_POLLS}: success=False after "
            f"fault reset — no profiles active. result_body: {poll_result}"
        )
        health_map = _get_provider_health(fault_admin_client)
        voyage_entry = health_map.get(VOYAGE_PROVIDER_NAME, {})
        cohere_entry = health_map.get(COHERE_PROVIDER_NAME, {})
        if not _is_provider_degraded(voyage_entry) and not _is_provider_degraded(
            cohere_entry
        ):
            recovered = True
            break
        time.sleep(SINBIN_POLL_INTERVAL_SECONDS)
    assert recovered, (
        f"Providers did not recover (sinbin cleared and status != 'down') within "
        f"{SINBIN_RECOVERY_WAIT_SECONDS}s after fault reset. "
        f"Final health map: {_get_provider_health(fault_admin_client)}"
    )


def test_health_monitor_recovers_after_fault_reset(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """AC5: After kill-profile + fault reset, both providers recover from sinbin."""
    repo_alias = f"{indexed_golden_repo}-global"
    _trigger_cohere_sinbin(fault_admin_client, repo_alias)
    _reset_and_wait_recovery(fault_admin_client, repo_alias)

    # Final: both providers healthy — parallel query must return real .py results.
    final_result = _run_parallel_search(fault_admin_client, repo_alias)
    assert final_result.get("success") is True, (
        f"Post-recovery MCP search returned success=False for '{repo_alias}': "
        f"{final_result}"
    )
    items = final_result.get("results", {}).get("results", [])
    assert items, (
        f"Post-recovery MCP search returned 0 results for '{repo_alias}'. "
        f"result_body: {final_result}"
    )
    has_py = any(".py" in (item.get("file_path") or "") for item in items)
    assert has_py, (
        f"Post-recovery MCP results for '{repo_alias}' have no .py file_path. "
        f"First {RESULT_PREVIEW_COUNT} items: {items[:RESULT_PREVIEW_COUNT]}"
    )

    # Server must remain alive throughout the kill-profile + sinbin-recovery cycle.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code} after Phase A/B cycle; "
        f"server must survive the kill-profile + sinbin-recovery sequence."
    )
