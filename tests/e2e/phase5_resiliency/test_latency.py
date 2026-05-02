"""
AC2 (#867): Latency injection completes within budget.

Scenario: A latency profile is installed (latency_rate=1.0,
latency_ms_range=[200,400]); MCP search_code with query_strategy="parallel"
exits within LATENCY_QUERY_BUDGET_SECONDS; history shows at least one latency
event for the target.

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy routes both providers through the
wired http_client_factory, which intercepts the latency profile.  Driving
through MCP (rather than the CLI subprocess) ensures the latency transport is
actually exercised on the query-time embedding calls.

Target hostname (VOYAGE_TARGET) is a fault-transport protocol constant —
it matches the httpx transport-layer interception target and is not an
environment-specific configuration value.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

See:
  https://github.com/LightspeedDMS/code-indexer/issues/485 (epic design)
  https://github.com/LightspeedDMS/code-indexer/issues/867 (AC2)
"""

from __future__ import annotations

import time
from typing import List, cast

import httpx

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _mcp_search

# Fault-transport protocol constant — not environment-specific configuration.
VOYAGE_TARGET = "api.voyageai.com"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies or helpers.
# ---------------------------------------------------------------------------
HTTP_OK: int = 200  # Expected status for REST calls
HTTP_CREATED: int = 201  # Accepted status for profile PUT (create)
SERVER_ERROR_THRESHOLD: int = 500  # GET /health must return below this
SEARCH_LIMIT: int = 10  # Result limit for MCP search calls

# AC2 hard budget: query with latency injection must return within this many seconds.
LATENCY_QUERY_BUDGET_SECONDS: float = 10.0

# Latency profile values matching the AC2 specification.
LATENCY_RATE: float = 1.0
LATENCY_MS_MIN: int = 200
LATENCY_MS_MAX: int = 400


def _install_latency_profile(client: FaultAdminClient, target: str) -> None:
    """PUT latency profile on *target* and verify round-trip via GET."""
    payload = {
        "target": target,
        "enabled": True,
        "latency_rate": LATENCY_RATE,
        "latency_ms_range": [LATENCY_MS_MIN, LATENCY_MS_MAX],
    }
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (HTTP_OK, HTTP_CREATED), (
        f"PUT latency profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == HTTP_OK, (
        f"GET profile for {target!r} after PUT failed: {get_resp.status_code}"
    )
    stored_rate = get_resp.json().get("latency_rate")
    assert stored_rate == LATENCY_RATE, (
        f"latency_rate not persisted for {target!r}: "
        f"expected {LATENCY_RATE}, got {stored_rate!r}"
    )


def _fetch_history_for_target(client: FaultAdminClient, target: str) -> List[dict]:
    """Fetch history and return events matching *target*.

    Hard-asserts history endpoint returns 200 — a non-200 is a real regression
    and must not be masked.
    """
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == HTTP_OK, (
        f"GET /admin/fault-injection/history returned {resp.status_code}; "
        f"the history endpoint must be reachable. Response: {resp.text}"
    )
    all_events = cast(List[dict], resp.json()["history"])
    return [e for e in all_events if e.get("target") == target]


def _assert_latency_events(target_events: List[dict], target: str) -> None:
    """Assert *target_events* contains at least one latency event."""
    assert target_events, (
        f"Expected at least one history event for {target!r}; got 0. "
        f"The fault transport must be wired for latency interception."
    )
    assert all("latency" in e.get("fault_type", "") for e in target_events), (
        f"Not all events for {target!r} have fault_type indicating latency: "
        f"{target_events}"
    )


def _assert_server_healthy(client: httpx.Client, context: str) -> None:
    """Assert GET /health returns below SERVER_ERROR_THRESHOLD."""
    health_resp = client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code} {context}; "
        f"server must remain alive."
    )


def test_latency_injection_completes_within_budget(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """AC2: Latency profile on VoyageAI must not prevent MCP query from completing.

    Asserts:
      1. Latency profile CRUD round-trip (PUT + GET verified).
      2. MCP parallel query completes within LATENCY_QUERY_BUDGET_SECONDS.
      3. result["success"] is True.
      4. GET /health returns < SERVER_ERROR_THRESHOLD.
      5. Fault history contains at least one latency event for VOYAGE_TARGET.
    """
    _install_latency_profile(fault_admin_client, VOYAGE_TARGET)

    repo_alias = f"{indexed_golden_repo}-global"
    start = time.monotonic()
    result_body = _mcp_search(
        fault_admin_client,
        query_text="escape",
        repository_alias=repo_alias,
        query_strategy="parallel",
        limit=SEARCH_LIMIT,
    )
    elapsed = time.monotonic() - start

    assert result_body.get("success") is True, (
        f"MCP search_code must return success=True under latency profile on "
        f"{VOYAGE_TARGET!r}. result_body: {result_body}"
    )
    assert elapsed < LATENCY_QUERY_BUDGET_SECONDS, (
        f"MCP parallel query with latency profile on {VOYAGE_TARGET!r} took "
        f"{elapsed:.2f}s (budget: {LATENCY_QUERY_BUDGET_SECONDS}s). "
        f"Configured latency range [{LATENCY_MS_MIN}, {LATENCY_MS_MAX}] ms "
        f"must not block the client beyond the budget."
    )

    _assert_server_healthy(
        fault_http_client,
        f"after latency profile on {VOYAGE_TARGET!r}",
    )

    target_events = _fetch_history_for_target(fault_admin_client, VOYAGE_TARGET)
    _assert_latency_events(target_events, VOYAGE_TARGET)
