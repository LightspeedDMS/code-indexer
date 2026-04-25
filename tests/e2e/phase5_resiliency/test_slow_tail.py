"""
AC3: Slow-tail Bernoulli distribution at E2E scale.

Scenario: slow_tail_rate=0.5 on VoyageAI; 20 queries run; approximately 50% of
intercepted events are slow_tail outcomes (tolerance band: 3 <= count <= 17).

Target hostnames (VOYAGE_TARGET) are fault-transport protocol constants — they
match the httpx transport-layer interception targets and are not
environment-specific configuration values.

Current state (bug #899 — fault transport not wired into query-time embedding clients):
EmbeddingProviderFactory.create() constructs VoyageAIClient and
CohereEmbeddingProvider without passing http_client_factory.  Slow-tail profiles
are accepted by the control plane (CRUD works) but never intercept actual
query-time embedding calls — fault history stays empty after queries.

Smoke assertions (always hard):
  - Profile CRUD round-trip (PUT + GET).
  - All QUERY_COUNT cidx queries exit 0.
  - GET /health < 500.
  - GET /admin/fault-injection/history returns 200 (endpoint reachable — a
    non-200 is a real regression, not a known bug #899 condition).

xfail boundary (bug #899): triggered only when event_count == 0 (i.e. history
empty after queries). When bug #899 is fixed and events appear, the distribution
band assertion runs as live code. This means the test self-upgrades without code
change when the transport wiring is corrected.

See: https://github.com/LightspeedDMS/code-indexer/issues/899
"""

from __future__ import annotations

from pathlib import Path
from typing import List, cast

import httpx
import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env
from tests.e2e.helpers import run_cidx

# Fault-transport protocol constants — these match the httpx transport-layer
# interception targets and are not environment-specific configuration values.
VOYAGE_TARGET = "api.voyageai.com"

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
    assert put_resp.status_code in (200, 201), (
        f"PUT slow-tail profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == 200, (
        f"GET profile for {target!r} after PUT failed: {get_resp.status_code}"
    )
    stored_rate = get_resp.json().get("slow_tail_rate")
    assert stored_rate == SLOW_TAIL_RATE, (
        f"slow_tail_rate not persisted for {target!r}: "
        f"expected {SLOW_TAIL_RATE}, got {stored_rate!r}"
    )


def _run_queries_and_assert_success(
    count: int, repo_alias_arg: str, workspace: Path
) -> None:
    """Run *count* cidx queries and assert each exits 0."""
    env = _build_cli_env()
    for i in range(count):
        result = run_cidx(
            "query", "escape",
            "--repos", repo_alias_arg,
            "--quiet",
            cwd=str(workspace),
            env=env,
        )
        assert result.returncode == 0, (
            f"cidx query #{i + 1}/{count} must exit 0 under slow-tail profile "
            f"on {VOYAGE_TARGET!r}. Got exit {result.returncode}. "
            f"stderr:\n{result.stderr}"
        )


def _assert_server_healthy(client: httpx.Client, context: str) -> None:
    """Assert GET /health returns < 500."""
    health_resp = client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} {context}; "
        f"server must remain alive."
    )


def _fetch_history(client: FaultAdminClient) -> List[dict]:
    """Fetch /admin/fault-injection/history; assert 200 (hard — not an xfail condition)."""
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == 200, (
        f"GET /admin/fault-injection/history returned {resp.status_code}; "
        f"the history endpoint must be reachable. "
        f"This is a real regression, not a known bug #899 condition. "
        f"Response: {resp.text}"
    )
    return resp.json()  # type: ignore[no-any-return]


def _assert_slow_tail_distribution(events: List[dict], target: str) -> None:
    """Assert slow_tail events for *target* fall within the expected Bernoulli band."""
    slow_tail_events = [
        e for e in events
        if e.get("target") == target and "slow_tail" in e.get("fault_type", "")
    ]
    assert SLOW_TAIL_MIN_EVENTS <= len(slow_tail_events) <= SLOW_TAIL_MAX_EVENTS, (
        f"slow_tail event count {len(slow_tail_events)} for {target!r} outside "
        f"expected band [{SLOW_TAIL_MIN_EVENTS}, {SLOW_TAIL_MAX_EVENTS}] over "
        f"{QUERY_COUNT} queries. Distribution appears degenerate (all or none). "
        f"Total events in history: {len(events)}."
    )


def test_slow_tail_bernoulli_distribution(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC3: slow_tail_rate=0.5 must produce a non-degenerate distribution over 20 queries.

    xfail triggered only when history is empty (bug #899 condition). When the
    transport is wired and events appear, the distribution band assertion runs.

    See: https://github.com/LightspeedDMS/code-indexer/issues/899
    """
    _install_slow_tail_profile(fault_admin_client, VOYAGE_TARGET)
    repo_alias_arg = f"{indexed_golden_repo}-global"
    _run_queries_and_assert_success(QUERY_COUNT, repo_alias_arg, fault_workspace)
    _assert_server_healthy(
        fault_http_client,
        f"after {QUERY_COUNT} queries under slow-tail profile on {VOYAGE_TARGET!r}",
    )

    events = _fetch_history(fault_admin_client)
    if not events:
        # xfail only for the known bug #899 condition: history empty after queries.
        # When bug #899 is fixed and events appear, execution continues past this
        # branch and the distribution assertion below runs automatically.
        pytest.xfail(
            reason=(
                f"bug #899: fault transport not wired — slow_tail profile "
                f"(rate={SLOW_TAIL_RATE}) on {VOYAGE_TARGET!r} produced 0 history "
                f"events after {QUERY_COUNT} queries. "
                f"See https://github.com/LightspeedDMS/code-indexer/issues/899"
            )
        )

    # Reached only when bug #899 is fixed and the transport is wired.
    _assert_slow_tail_distribution(events, VOYAGE_TARGET)
