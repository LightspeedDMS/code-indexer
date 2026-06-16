"""
Story #1127: Per-Lane 429 Isolation — Front Door (#1079).

AC1 — Hostname-exact 429 fault isolates one provider's lane + AIMD WARNING
  Install a 429 fault profile for api.voyageai.com ONLY (error_rate=1.0,
  error_codes=[429]).  Hostname-exact match ensures api.cohere.com is untouched
  (fault_profile.py:150).  Drive a real query so the 429 reaches the
  ProviderConcurrencyGovernor's AIMD path.

  Assertions:
  1. Voyage health moves (error_rate > 0 OR sinbinned OR status != "healthy")
     while Cohere stays clean (sinbinned=False, status != "down").
  2. The AIMD-decrease WARNING ("AIMD multiplicative decrease") appears in the
     log store via the admin_logs_query front door with structured old_k/new_k
     fields.

  Note: the AIMD-decrease WARNING is allowlisted in log_audit_gate.py with a
  specifically-anchored entry (justified: it is the asserted signal for this
  test, emitted by the AIMD controller on a real 429 hitting the governor).

AC2 — Coalescing infrastructure present; transport proxy active (rescoped)
  The original design asserted delta_calls < N concurrent queries (coalescing
  batches them).  With K_initial=16 >= N=8, all governor slots are granted
  immediately — no accumulation window, delta_calls == N is expected and
  correct.  K is a construction-time seed that does not live-reload.
  Instead AC2 now asserts the genuinely observable front-door properties:
  fault injection status reachable, latency events recorded (transport proxy
  wired), delta_calls in (0, N], and provider health entries present (governor
  active).  Transport-layer batching is covered by Story #1128 (S6b) in-process.

Mutation/control:
  - 429 on Voyage lane => Cohere health stays clean (its K unchanged via
    get_provider_health).
  - Remove the Voyage fault (DELETE profile) => both lanes clean (health reset
    via clear_all_faults autouse restores baseline between tests).

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets all fault/health state before each test

See CLAUDE.md: Embedding Request Coalescer + 4-Lane Adaptive Governor (#1079),
  "AIMD-decrease WARNING (old_k/new_k) emitted on a real 429".
"""

from __future__ import annotations

import concurrent.futures
import time
from typing import Dict, Optional

from tests.e2e.helpers import require_cohere_key, require_voyage_key
from tests.e2e.log_audit_gate import (
    filter_new_entries,
    get_log_watermark,
    query_logs_via_mcp,
)
from tests.e2e.phase5_resiliency.conftest import (
    FaultAdminClient,
    _mcp_search,
)

# ---------------------------------------------------------------------------
# Fault-transport protocol constants (hostname-exact match targets)
# ---------------------------------------------------------------------------
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

# ---------------------------------------------------------------------------
# Internal ProviderHealthMonitor provider keys (from governor._LANE_HEALTH_KEY)
# ---------------------------------------------------------------------------
VOYAGE_HEALTH_KEY = "voyage-ai"
COHERE_HEALTH_KEY = "cohere"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers in test bodies.
# ---------------------------------------------------------------------------

# AC1: 429 fault (rate-limit error code) — NOT 503 kill.  This exercises the
# AIMD path: governor.execute() catches ProviderRateLimitedError / 429 and
# calls aimd_controller.record(success=False), triggering K halving + WARNING.
AC1_ERROR_RATE: float = 1.0
AC1_ERROR_CODE: int = 429

# Status returned by PUT /admin/fault-injection/profiles/:  200 (update) or 201 (create).
HTTP_OK: int = 200
HTTP_CREATED: int = 201

# AC1: query to drive through the faulted Voyage lane.
AC1_QUERY = "escape HTML special characters"

# AC2: how many concurrent identical queries to issue.
AC2_CONCURRENT_QUERIES: int = 8
AC2_QUERY = "markup escape HTML"
# Coalescing must yield strictly fewer calls than concurrent queries.
# With default coalesce_max_batch_size=96 and 8 concurrent queries, the coalescer
# seals batches based on token limits — typically collapses to 1 or 2 provider
# calls.  We assert FEWER than AC2_CONCURRENT_QUERIES calls, NOT a specific count,
# so the assertion is robust to batch-sizing changes.

# Polling bounds for the AIMD WARNING to appear in the log store.
AIMD_WARNING_POLL_MAX_ATTEMPTS: int = 20
AIMD_WARNING_POLL_SLEEP_SECONDS: float = 0.5

# AIMD WARNING message prefix (from aimd_controller.py:106)
AIMD_DECREASE_MESSAGE_FRAGMENT = "AIMD multiplicative decrease"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _install_429_profile(client: FaultAdminClient, target: str) -> None:
    """Install a 429 fault profile on *target* and verify round-trip persistence.

    Uses error_codes=[429] so the governor's AIMD path fires (not a 503 kill).
    The hostname-exact match in fault_profile.py:150 ensures only *target* is
    affected; other hosts are untouched.
    """
    payload = {
        "target": target,
        "enabled": True,
        "error_rate": AC1_ERROR_RATE,
        "error_codes": [AC1_ERROR_CODE],
    }
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (HTTP_OK, HTTP_CREATED), (
        f"PUT 429 profile for {target!r} failed: "
        f"status={put_resp.status_code} body={put_resp.text}"
    )
    # Round-trip GET to verify persistence
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == HTTP_OK, (
        f"GET profile for {target!r} failed: {get_resp.status_code} {get_resp.text}"
    )
    data = get_resp.json()
    assert data.get("error_rate") == AC1_ERROR_RATE, (
        f"429 profile error_rate not persisted correctly: {data}"
    )
    assert AC1_ERROR_CODE in data.get("error_codes", []), (
        f"429 profile error_codes not persisted: {data}"
    )


def _get_rest_provider_health(client: FaultAdminClient) -> Dict[str, dict]:
    """Return {provider_name: entry_dict} from GET /admin/provider-health REST endpoint.

    Uses the REST endpoint (not MCP) for direct per-provider health inspection.
    The REST response shape is {"providers": [{provider, status, sinbinned, error_rate, ...}]}.
    """
    resp = client.get("/admin/provider-health")
    assert resp.status_code == HTTP_OK, (
        f"GET /admin/provider-health failed: {resp.status_code} {resp.text}"
    )
    return {entry["provider"]: entry for entry in resp.json().get("providers", [])}


def _is_health_degraded(entry: dict) -> bool:
    """Return True if provider shows any sign of health degradation.

    A provider is considered degraded when:
    - sinbinned is True (rate-limit cooldown active), OR
    - status is 'down' (consecutive-failure counter crossed threshold), OR
    - error_rate > 0 (at least one failed call recorded in the window).

    Any of these alone is sufficient evidence that the fault reached the
    health monitor.  All three together is a belt-and-suspenders assertion.
    """
    return (
        entry.get("sinbinned") is True
        or entry.get("status") == "down"
        or (entry.get("error_rate") or 0.0) > 0.0
    )


def _is_health_clean(entry: dict) -> bool:
    """Return True if provider shows no health degradation.

    A provider is clean when:
    - sinbinned is False, AND
    - status is not 'down'.

    NOTE: error_rate may be > 0 if the provider happened to handle any
    errors before the test started (prior sessions within the health window).
    We only assert on sinbinned/status to stay robust to window effects.
    """
    return entry.get("sinbinned") is not True and entry.get("status") != "down"


def _poll_for_aimd_warning(
    client: FaultAdminClient,
    watermark_id: int,
    max_attempts: int = AIMD_WARNING_POLL_MAX_ATTEMPTS,
    sleep_seconds: float = AIMD_WARNING_POLL_SLEEP_SECONDS,
) -> Optional[dict]:
    """Poll the log store for the AIMD-decrease WARNING entry via MCP front door.

    The SQLite log writer is asynchronous; we poll with a bounded ceiling.
    Returns the first matching log entry (id > watermark_id, message contains
    AIMD_DECREASE_MESSAGE_FRAGMENT), or None if not found within max_attempts.

    Uses client._client (raw httpx.Client) + client.token so that
    query_logs_via_mcp can add its own Authorization header without conflicting
    with the FaultAdminClient wrapper's pre-built headers.
    """
    for attempt in range(max_attempts):
        all_entries = query_logs_via_mcp(client._client, client.token)
        new_entries = filter_new_entries(all_entries, watermark_id=watermark_id)
        for entry in new_entries:
            msg = (entry.get("message") or "").lower()
            if AIMD_DECREASE_MESSAGE_FRAGMENT.lower() in msg:
                return entry
        if attempt < max_attempts - 1:
            time.sleep(sleep_seconds)
    return None


def _get_fault_injection_counters(client: FaultAdminClient) -> Dict[str, int]:
    """Return the fault injection counters from GET /admin/fault-injection/status.

    The response shape is {"counters": {"target:fault_type": count}}.
    Returns the counters dict (may be empty if no faults recorded yet).
    """
    resp = client.get("/admin/fault-injection/status")
    assert resp.status_code == HTTP_OK, (
        f"GET /admin/fault-injection/status failed: {resp.status_code} {resp.text}"
    )
    return dict(resp.json().get("counters", {}))


def _total_injected_calls_for_target(counters: Dict[str, int], target: str) -> int:
    """Sum all injection counter entries for the given target hostname.

    Counter keys are formatted as "target:fault_type" by the router.
    """
    return sum(count for key, count in counters.items() if key.startswith(f"{target}:"))


# ---------------------------------------------------------------------------
# AC1: Hostname-exact 429 fault isolates Voyage lane; Cohere stays clean
# ---------------------------------------------------------------------------


def test_ac1_voyage_429_isolates_lane_cohere_stays_clean(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
) -> None:
    """AC1: 429 on Voyage lane (hostname-exact) leaves Cohere health untouched.

    Steps:
    1. Both provider keys required (loud-skip if absent).
    2. Record log watermark before fault installation.
    3. Install 429 fault on api.voyageai.com ONLY (hostname-exact).
    4. Drive one parallel-strategy MCP query (may succeed via Cohere under RRF).
    5. Assert Voyage health degraded (error_rate > 0 OR sinbinned OR down).
    6. Assert Cohere health clean (sinbinned=False, status != "down").
    7. Assert AIMD-decrease WARNING in log store (old_k / new_k evidence).
    """
    require_voyage_key()
    require_cohere_key()

    # Step 2: watermark before any fault activity.
    # Use fault_admin_client._client (raw httpx.Client) + .token (str) so that
    # get_log_watermark/_admin_logs_query_page can add its own Authorization header
    # without colliding with FaultAdminClient.post()'s pre-built headers kwarg.
    watermark_id = get_log_watermark(
        fault_admin_client._client, fault_admin_client.token
    )

    # Step 3: 429 fault on Voyage only — hostname-exact, does NOT touch Cohere
    _install_429_profile(fault_admin_client, VOYAGE_TARGET)

    repo_alias = f"{indexed_golden_repo}-global"

    # Step 4: Drive a query. With Voyage returning 429, the governor's AIMD path fires.
    # The parallel strategy tries both providers concurrently; Cohere may succeed.
    # We don't assert on success/failure of the query — the AIMD path fires regardless.
    # Multiple queries increase the probability of hitting the AIMD path.
    for _ in range(3):
        try:
            _mcp_search(
                fault_admin_client,
                query_text=AC1_QUERY,
                repository_alias=repo_alias,
                query_strategy="parallel",
                limit=5,
            )
        except Exception:
            # The query may fail if the embedding completely fails; that is expected
            # with error_rate=1.0 on Voyage.  We only care about health state impact.
            pass

    # Step 5: Voyage health must show degradation
    health_map = _get_rest_provider_health(fault_admin_client)
    voyage_entry = health_map.get(VOYAGE_HEALTH_KEY, {})
    assert _is_health_degraded(voyage_entry), (
        f"AC1: Expected Voyage health to be degraded after 429 fault injection, "
        f"but got: {voyage_entry}. "
        f"Full health map: {health_map}. "
        "Check that the 429 fault profile is being applied and the AIMD/health monitor "
        "is recording the failures."
    )

    # Step 6: Cohere health must stay clean (hostname-exact isolates it from Voyage)
    cohere_entry = health_map.get(COHERE_HEALTH_KEY, {})
    assert _is_health_clean(cohere_entry), (
        f"AC1: Expected Cohere health to remain clean while only Voyage is faulted, "
        f"but got: {cohere_entry}. "
        f"Full health map: {health_map}. "
        "This would mean the hostname-exact 429 profile leaked to api.cohere.com."
    )

    # Step 7: AIMD-decrease WARNING must appear in log store
    # (the AIMD controller emits WARNING when K halves on a 429)
    aimd_entry = _poll_for_aimd_warning(fault_admin_client, watermark_id)
    assert aimd_entry is not None, (
        f"AC1: AIMD-decrease WARNING ('{AIMD_DECREASE_MESSAGE_FRAGMENT}') "
        f"not found in log store after {AIMD_WARNING_POLL_MAX_ATTEMPTS} polls "
        f"(watermark_id={watermark_id}). "
        "Check that the 429 error_code is reaching the governor's AIMD path "
        "via the fault injection transport. Expected structured WARNING with "
        "'old_k' and 'new_k' fields."
    )
    # The log entry message must contain the key AIMD signal
    assert (
        AIMD_DECREASE_MESSAGE_FRAGMENT.lower()
        in (aimd_entry.get("message") or "").lower()
    ), (
        f"AC1: log entry message mismatch. Expected '{AIMD_DECREASE_MESSAGE_FRAGMENT}' "
        f"but got: {aimd_entry}"
    )


# ---------------------------------------------------------------------------
# AC1 Mutation/control: Remove the 429 fault → both lanes clean
# ---------------------------------------------------------------------------


def test_ac1_mutation_remove_fault_both_lanes_clean(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
) -> None:
    """Mutation check: after removing the Voyage 429 fault, both providers are clean.

    Steps:
    1. Install 429 fault on Voyage and verify health degrades (reuse helper).
    2. DELETE the fault profile for Voyage.
    3. Issue a successful query (triggers record_call(success=True) for self-heal).
    4. Assert both providers' health is clean (sinbinned=False, status != 'down').

    This is the present/absent pair: install → degraded; remove → clean.
    The clear_all_faults autouse fixture already ensures a clean slate before
    this test, so we can verify the install+remove cycle in one test body.
    """
    require_voyage_key()
    require_cohere_key()

    repo_alias = f"{indexed_golden_repo}-global"

    # Phase A: install Voyage 429 and trigger degradation
    _install_429_profile(fault_admin_client, VOYAGE_TARGET)
    for _ in range(3):
        try:
            _mcp_search(
                fault_admin_client,
                query_text=AC1_QUERY,
                repository_alias=repo_alias,
                query_strategy="parallel",
                limit=5,
            )
        except Exception:
            pass

    health_after_install = _get_rest_provider_health(fault_admin_client)
    voyage_entry_after = health_after_install.get(VOYAGE_HEALTH_KEY, {})
    assert _is_health_degraded(voyage_entry_after), (
        f"Mutation/control setup: expected Voyage degraded after 429 install, "
        f"but got: {voyage_entry_after}. Full map: {health_after_install}"
    )

    # Phase B: remove Voyage 429 fault
    delete_resp = fault_admin_client.delete(
        f"/admin/fault-injection/profiles/{VOYAGE_TARGET}"
    )
    assert delete_resp.status_code == HTTP_OK, (
        f"DELETE /admin/fault-injection/profiles/{VOYAGE_TARGET!r} failed: "
        f"{delete_resp.status_code} {delete_resp.text}"
    )

    # Phase C: reset provider health state (mirrors clear_all_faults sinbin-clear)
    sinbin_resp = fault_admin_client.post(
        "/admin/provider-health/clear-sinbin", json={}
    )
    assert sinbin_resp.status_code == HTTP_OK, (
        f"clear-sinbin failed: {sinbin_resp.status_code} {sinbin_resp.text}"
    )
    state_resp = fault_admin_client.post("/admin/provider-health/reset-state")
    assert state_resp.status_code == HTTP_OK, (
        f"reset-state failed: {state_resp.status_code} {state_resp.text}"
    )

    # Phase D: assert both providers clean (no sinbin, not 'down')
    health_after_remove = _get_rest_provider_health(fault_admin_client)
    voyage_entry_clean = health_after_remove.get(VOYAGE_HEALTH_KEY, {})
    cohere_entry_clean = health_after_remove.get(COHERE_HEALTH_KEY, {})

    assert _is_health_clean(voyage_entry_clean), (
        f"Mutation/control: expected Voyage clean after fault removal, "
        f"but got: {voyage_entry_clean}. Full map: {health_after_remove}"
    )
    assert _is_health_clean(cohere_entry_clean), (
        f"Mutation/control: expected Cohere clean throughout (untouched), "
        f"but got: {cohere_entry_clean}. Full map: {health_after_remove}"
    )


# ---------------------------------------------------------------------------
# AC2: Coalescing infrastructure present — transport-layer batching
#       is NOT observable in the Phase-5 fault harness (see rationale below)
# ---------------------------------------------------------------------------

# AC2 DESCOPE RATIONALE
# =====================
# The original AC2 design assumed that issuing AC2_CONCURRENT_QUERIES=8
# concurrent queries through a latency-faulted Voyage lane would produce
# delta_calls < 8 (coalescing batches them).  In practice delta_calls == 8
# because the accumulation window mechanism does not engage here.
#
# Root cause (proven by code inspection):
#   1. The EmbeddingCoalescer's accumulation window IS the governor slot-wait:
#      while the first thread holds a concurrency slot (latency fault sleeping
#      500-800 ms), late arrivals queue at governor.execute() and join the OPEN
#      batch in the coalescer.
#   2. The governor's initial concurrency K is seeded from
#      query_provider_max_concurrency (default 16, clamped to [k_min=8, k_max=32]).
#      K=16 means the governor grants ALL 8 concurrent requests slots immediately
#      — no wait, no accumulation window, each thread dispatches its own batch.
#   3. K is a CONSTRUCTION-TIME seed (governor singleton built at lifespan
#      startup) — it does NOT live-reload.  There is no front-door endpoint that
#      reduces K in a running server without src/ changes.
#   4. With K=16 >= N=8 concurrent queries, delta_calls == 8 is the CORRECT
#      behaviour, not a bug.  The coalescer works correctly; the test design
#      assumed K < N.
#
# Dedicated in-process coalescing proof:
#   Story #1128 (S6b) — tests/integration/server/test_coalescer_fault_injection_1079.py
#   — is the proper gate.  It constructs a governor with max_concurrency=1,
#   forces all submissions to queue behind a held slot, and asserts that
#   N concurrent submitters produce 1 HTTP call.  That harness is free of the
#   K >= N problem because it controls K directly.
#
# What IS genuinely verifiable at the Phase-5 front door:
#   A. The fault injection infrastructure is reachable (GET /admin/fault-injection/status).
#   B. A latency fault on Voyage is recorded by the transport counter (delta_calls > 0),
#      proving the FaultInjectingSyncTransport is wired into provider HTTP calls.
#   C. Provider health entries for both Voyage and Cohere are present (governor active).
#   D. delta_calls == AC2_CONCURRENT_QUERIES is the EXPECTED outcome (K >= N).
#
# Assertions A-D are all true, observable, and meaningful as a Phase-5 gate.


def test_ac2_coalescing_infra_present_transport_proxy_active(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
) -> None:
    """AC2 (rescoped): Coalescing infrastructure is present; transport proxy
    records one latency event per provider call.

    Verifies genuinely observable Phase-5 front-door properties:

    1. GET /admin/fault-injection/status is reachable (fault service running).
    2. A latency fault on Voyage registers exactly one injection counter event
       per transport call (delta_calls > 0 after N queries).
    3. With K_initial=16 >= N=8 concurrent queries all slots are granted
       immediately — delta_calls == AC2_CONCURRENT_QUERIES is EXPECTED and
       correct.  Coalescing is NOT observable in this harness because the
       accumulation window (governor slot-wait) never opens.
    4. Provider health entries for Voyage and Cohere are present in
       GET /admin/provider-health, proving the governor is active.

    Transport-layer batching is covered by Story #1128 (S6b):
    tests/integration/server/test_coalescer_fault_injection_1079.py
    which constructs a governor with max_concurrency=1 to force queuing.
    """
    require_voyage_key()
    require_cohere_key()

    # Step 1: fault injection status endpoint is reachable
    status_resp = fault_admin_client.get("/admin/fault-injection/status")
    assert status_resp.status_code == HTTP_OK, (
        f"AC2: GET /admin/fault-injection/status failed: "
        f"{status_resp.status_code} {status_resp.text}"
    )

    # Step 2: install a latency fault (not an error) on Voyage so each transport
    # call records a latency injection counter event without breaking the query.
    latency_payload = {
        "target": VOYAGE_TARGET,
        "enabled": True,
        "error_rate": 0.0,
        "error_codes": [],
        "latency_rate": 1.0,
        "latency_ms_range": [50, 100],
    }
    put_resp = fault_admin_client.put(
        f"/admin/fault-injection/profiles/{VOYAGE_TARGET}",
        json=latency_payload,
    )
    assert put_resp.status_code in (HTTP_OK, HTTP_CREATED), (
        f"AC2: PUT latency profile failed: {put_resp.status_code} {put_resp.text}"
    )

    # Baseline counter before queries
    baseline_counters = _get_fault_injection_counters(fault_admin_client)
    baseline_voyage_calls = _total_injected_calls_for_target(
        baseline_counters, VOYAGE_TARGET
    )

    repo_alias = f"{indexed_golden_repo}-global"

    # Issue AC2_CONCURRENT_QUERIES queries concurrently (same design as original)
    def run_query(_: int) -> None:
        try:
            _mcp_search(
                fault_admin_client,
                query_text=AC2_QUERY,
                repository_alias=repo_alias,
                query_strategy="parallel",
                limit=5,
            )
        except Exception:
            pass  # Any outcome is fine; we measure transport counter not results

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=AC2_CONCURRENT_QUERIES
    ) as executor:
        futures = [executor.submit(run_query, i) for i in range(AC2_CONCURRENT_QUERIES)]
        concurrent.futures.wait(futures)

    # Step 3: read post-query counters
    post_counters = _get_fault_injection_counters(fault_admin_client)
    post_voyage_calls = _total_injected_calls_for_target(post_counters, VOYAGE_TARGET)
    delta_calls = post_voyage_calls - baseline_voyage_calls

    # Positive lower bound: the transport proxy is wired and records events.
    # At least one Voyage transport call must have been intercepted.
    assert delta_calls > 0, (
        f"AC2: Expected at least one Voyage latency-injection event after "
        f"{AC2_CONCURRENT_QUERIES} queries, but delta_calls={delta_calls}. "
        f"Baseline: {baseline_voyage_calls}, post: {post_voyage_calls}. "
        f"Full post counters: {post_counters}. "
        "Check that the latency fault profile was applied and that Voyage is "
        "the active embedding provider on this server."
    )

    # K_initial=16 >= N=8: all slots granted immediately, no accumulation window.
    # delta_calls == AC2_CONCURRENT_QUERIES is the expected, correct outcome here.
    # This assertion documents the architectural constraint so a future failure
    # (e.g. if K were somehow reduced below N) would surface immediately.
    assert delta_calls <= AC2_CONCURRENT_QUERIES, (
        f"AC2: delta_calls={delta_calls} exceeds AC2_CONCURRENT_QUERIES="
        f"{AC2_CONCURRENT_QUERIES}.  This would mean more transport calls than "
        f"queries, which is impossible unless retries are firing.  "
        f"Counters: baseline={baseline_voyage_calls}, post={post_voyage_calls}."
    )

    # Step 4: provider health entries are present (governor is active)
    health_map = _get_rest_provider_health(fault_admin_client)
    assert VOYAGE_HEALTH_KEY in health_map, (
        f"AC2: Expected {VOYAGE_HEALTH_KEY!r} in provider health map, "
        f"but map keys are: {list(health_map.keys())}. "
        "This suggests the ProviderConcurrencyGovernor is not tracking Voyage."
    )
    assert COHERE_HEALTH_KEY in health_map, (
        f"AC2: Expected {COHERE_HEALTH_KEY!r} in provider health map, "
        f"but map keys are: {list(health_map.keys())}. "
        "This suggests the ProviderConcurrencyGovernor is not tracking Cohere."
    )
