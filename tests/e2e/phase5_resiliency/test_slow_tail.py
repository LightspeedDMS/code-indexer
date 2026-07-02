"""
AC3 (#867): Slow-tail Bernoulli distribution at E2E scale.

Scenario: slow_tail_rate=0.5 on VoyageAI; QUERY_COUNT MCP parallel queries
run; approximately 50% of intercepted events are slow_tail outcomes
(tolerance band: SLOW_TAIL_MIN_EVENTS <= count <= SLOW_TAIL_MAX_EVENTS).

Test approach: MCP tools/call search_code with query_strategy="parallel"
(not cidx CLI).  The parallel strategy routes both providers through the
wired http_client_factory, which intercepts the slow_tail profile.  Driving
through MCP (rather than the CLI subprocess) ensures the fault transport is
actually exercised on the query-time embedding calls.

Target hostname (VOYAGE_TARGET) is a fault-transport protocol constant —
it matches the httpx transport-layer interception target and is not an
environment-specific configuration value.

Assertions (all hard — no xfail):
  - Profile CRUD round-trip (PUT + GET verified).
  - All QUERY_COUNT MCP parallel queries return success=True.
  - GET /health < SERVER_ERROR_THRESHOLD.
  - GET /admin/fault-injection/history returns 200.
  - slow_tail event count for VOYAGE_TARGET falls within
    [SLOW_TAIL_MIN_EVENTS, SLOW_TAIL_MAX_EVENTS] over QUERY_COUNT queries.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

See:
  https://github.com/LightspeedDMS/code-indexer/issues/485 (epic design)
  https://github.com/LightspeedDMS/code-indexer/issues/867 (AC3)
"""

from __future__ import annotations

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

# AC3 specification values.
SLOW_TAIL_RATE: float = 0.5
QUERY_COUNT: int = 20

# Assertion band for slow_tail event count over QUERY_COUNT queries.
# Band [3, 17] avoids flakes while still detecting a degenerate sampler.
SLOW_TAIL_MIN_EVENTS: int = 3
SLOW_TAIL_MAX_EVENTS: int = 17


def _install_slow_tail_profile(client: FaultAdminClient, target: str) -> None:
    """PUT slow-tail profile on *target* and verify round-trip via GET."""
    payload = {"target": target, "enabled": True, "slow_tail_rate": SLOW_TAIL_RATE}
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (HTTP_OK, HTTP_CREATED), (
        f"PUT slow-tail profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == HTTP_OK, (
        f"GET profile for {target!r} after PUT failed: {get_resp.status_code}"
    )
    stored_rate = get_resp.json().get("slow_tail_rate")
    assert stored_rate == SLOW_TAIL_RATE, (
        f"slow_tail_rate not persisted for {target!r}: "
        f"expected {SLOW_TAIL_RATE}, got {stored_rate!r}"
    )


def _run_queries_and_assert_success(
    count: int, repo_alias: str, client: FaultAdminClient
) -> None:
    """Run *count* MCP parallel queries and assert each returns success=True."""
    for i in range(count):
        result_body = _mcp_search(
            client,
            query_text="escape",
            repository_alias=repo_alias,
            query_strategy="parallel",
            limit=SEARCH_LIMIT,
        )
        assert result_body.get("success") is True, (
            f"MCP query #{i + 1}/{count} must return success=True under slow-tail "
            f"profile on {VOYAGE_TARGET!r}. result_body: {result_body}"
        )


def _fetch_history(client: FaultAdminClient) -> List[dict]:
    """Fetch /admin/fault-injection/history; assert 200 (hard)."""
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == HTTP_OK, (
        f"GET /admin/fault-injection/history returned {resp.status_code}; "
        f"the history endpoint must be reachable. Response: {resp.text}"
    )
    return cast(List[dict], resp.json()["history"])


def _assert_slow_tail_distribution(events: List[dict], target: str) -> None:
    """Assert slow_tail events for *target* fall within the expected Bernoulli band."""
    slow_tail_events = [
        e
        for e in events
        if e.get("target") == target and "slow_tail" in e.get("fault_type", "")
    ]
    assert slow_tail_events, (
        f"Expected at least one slow_tail history event for {target!r}; got 0. "
        f"The fault transport must be wired for slow_tail interception."
    )
    assert SLOW_TAIL_MIN_EVENTS <= len(slow_tail_events) <= SLOW_TAIL_MAX_EVENTS, (
        f"slow_tail event count {len(slow_tail_events)} for {target!r} outside "
        f"expected band [{SLOW_TAIL_MIN_EVENTS}, {SLOW_TAIL_MAX_EVENTS}] over "
        f"{QUERY_COUNT} queries. Distribution appears degenerate (all or none). "
        f"Total events in history: {len(events)}."
    )


def _assert_server_healthy(client: httpx.Client, context: str) -> None:
    """Assert GET /health returns below SERVER_ERROR_THRESHOLD."""
    health_resp = client.get("/health")
    assert health_resp.status_code < SERVER_ERROR_THRESHOLD, (
        f"GET /health returned {health_resp.status_code} {context}; "
        f"server must remain alive."
    )


def test_slow_tail_bernoulli_distribution(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
) -> None:
    """AC3: slow_tail_rate=0.5 must produce a non-degenerate distribution over 20 queries.

    All assertions are hard — no xfail guards. The fault transport must be
    wired for slow_tail interception; if history is empty the test fails.
    """
    _install_slow_tail_profile(fault_admin_client, VOYAGE_TARGET)
    repo_alias = f"{indexed_golden_repo}-global"
    _run_queries_and_assert_success(QUERY_COUNT, repo_alias, fault_admin_client)
    _assert_server_healthy(
        fault_http_client,
        f"after {QUERY_COUNT} queries under slow-tail profile on {VOYAGE_TARGET!r}",
    )
    events = _fetch_history(fault_admin_client)
    _assert_slow_tail_distribution(events, VOYAGE_TARGET)
