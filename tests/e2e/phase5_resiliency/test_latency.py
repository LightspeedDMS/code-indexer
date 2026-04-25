"""
AC2: Latency injection completes within budget.

Scenario: A latency profile is installed (latency_rate=1.0, latency_ms_range=[200,400]);
the CLI exits 0 within a 10-second budget; history shows at least one latency event.

Target hostnames (VOYAGE_TARGET) are fault-transport protocol constants — they
match the httpx transport-layer interception targets and are not
environment-specific configuration values.

Current state (bug #899 — fault transport not wired into query-time embedding clients):
EmbeddingProviderFactory.create() constructs VoyageAIClient and
CohereEmbeddingProvider without passing http_client_factory.  Latency profiles
are accepted by the control plane (CRUD works) but never intercept actual
query-time embedding calls — no latency is injected and fault history stays empty.

Smoke assertions (always hard):
  - Latency profile CRUD round-trip (PUT + GET).
  - cidx query exits 0 within LATENCY_QUERY_BUDGET_SECONDS (no hang).
  - GET /health < 500.
  - GET /admin/fault-injection/history returns 200 (endpoint reachable — a
    non-200 is a real regression, not a known bug #899 condition).

xfail boundary (bug #899): triggered only when no history events exist for the
target (i.e. latency transport not intercepting calls). When bug #899 is fixed
and events appear, the fault_type assertion runs as live code — the test
self-upgrades without a code change.

The budget check (smoke assertion 2) is retained as a meaningful guard:
once the transport IS wired, a hang rather than a bounded delay will fail
loudly here rather than at a vague test-runner timeout.

See: https://github.com/LightspeedDMS/code-indexer/issues/899
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import List, cast

import httpx
import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env

# Fault-transport protocol constants — these match the httpx transport-layer
# interception targets and are not environment-specific configuration values.
VOYAGE_TARGET = "api.voyageai.com"

# AC2 hard budget: query with latency injection must return within this many seconds.
LATENCY_QUERY_BUDGET_SECONDS: float = 10.0

# Subprocess hard cap: slightly above the budget so TimeoutExpired fires before
# the test runner's own timeout, giving a clear failure message.
LATENCY_SUBPROCESS_CAP_SECONDS: float = LATENCY_QUERY_BUDGET_SECONDS + 5.0

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
    assert put_resp.status_code in (200, 201), (
        f"PUT latency profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == 200, (
        f"GET profile for {target!r} after PUT failed: {get_resp.status_code}"
    )
    stored_rate = get_resp.json().get("latency_rate")
    assert stored_rate == LATENCY_RATE, (
        f"latency_rate not persisted for {target!r}: "
        f"expected {LATENCY_RATE}, got {stored_rate!r}"
    )


def _run_query_within_budget(
    repo_alias_arg: str, workspace: Path
) -> "subprocess.CompletedProcess[str]":
    """Run cidx query and assert it completes within LATENCY_QUERY_BUDGET_SECONDS."""
    env = _build_cli_env()
    cmd = [
        "python3", "-m", "code_indexer.cli",
        "query", "escape",
        "--repos", repo_alias_arg,
        "--quiet",
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(workspace),
            env=env,
            timeout=LATENCY_SUBPROCESS_CAP_SECONDS,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        raise AssertionError(
            f"cidx query with latency profile on {VOYAGE_TARGET!r} did not terminate "
            f"within {LATENCY_SUBPROCESS_CAP_SECONDS:.0f}s (elapsed: {elapsed:.1f}s). "
            f"Latency transport must not hang the client; "
            f"configured range [{LATENCY_MS_MIN}, {LATENCY_MS_MAX}] ms."
        )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, (
        f"cidx query must exit 0 under latency profile on {VOYAGE_TARGET!r}. "
        f"Got exit {proc.returncode}. stderr:\n{proc.stderr}"
    )
    assert elapsed < LATENCY_QUERY_BUDGET_SECONDS, (
        f"cidx query with latency profile on {VOYAGE_TARGET!r} took {elapsed:.1f}s "
        f"(AC2 budget: {LATENCY_QUERY_BUDGET_SECONDS}s). "
        f"Configured latency range [{LATENCY_MS_MIN}, {LATENCY_MS_MAX}] ms must not "
        f"block the client beyond the budget."
    )
    return proc


def _assert_server_healthy(client: httpx.Client, context: str) -> None:
    """Assert GET /health returns < 500."""
    health_resp = client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} {context}; "
        f"server must remain alive."
    )


def _fetch_history_for_target(
    client: FaultAdminClient, target: str
) -> List[dict]:
    """Fetch history and return events matching *target*.

    Hard-asserts history endpoint returns 200 — a non-200 is a real regression,
    not a known bug #899 condition, and must not be masked by the xfail path.
    """
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == 200, (
        f"GET /admin/fault-injection/history returned {resp.status_code}; "
        f"the history endpoint must be reachable. "
        f"This is a real regression, not a known bug #899 condition. "
        f"Response: {resp.text}"
    )
    all_events = cast(List[dict], resp.json())
    return [e for e in all_events if e.get("target") == target]


def _assert_latency_events(target_events: List[dict], target: str) -> None:
    """Assert *target_events* contains at least one latency event with correct fault_type."""
    assert target_events, (
        f"Expected at least one history event for {target!r}; got 0."
    )
    assert all("latency" in e.get("fault_type", "") for e in target_events), (
        f"Not all events for {target!r} have fault_type indicating latency: "
        f"{target_events}"
    )


def test_latency_injection_completes_within_budget(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC2: Latency profile on VoyageAI must not prevent query from completing.

    xfail triggered only when no history events exist for the target (bug #899).
    When the transport is wired and events appear, the fault_type assertion runs.

    See: https://github.com/LightspeedDMS/code-indexer/issues/899
    """
    _install_latency_profile(fault_admin_client, VOYAGE_TARGET)
    repo_alias_arg = f"{indexed_golden_repo}-global"
    _run_query_within_budget(repo_alias_arg, fault_workspace)
    _assert_server_healthy(
        fault_http_client,
        f"after latency profile on {VOYAGE_TARGET!r}",
    )

    target_events = _fetch_history_for_target(fault_admin_client, VOYAGE_TARGET)
    if not target_events:
        # xfail only for the known bug #899 condition: no events for this target.
        # When bug #899 is fixed and events appear, execution continues past this
        # branch and the latency assertion below runs automatically.
        pytest.xfail(
            reason=(
                f"bug #899: fault transport not wired — latency profile "
                f"(rate={LATENCY_RATE}) on {VOYAGE_TARGET!r} produced 0 history "
                f"events after query. "
                f"See https://github.com/LightspeedDMS/code-indexer/issues/899"
            )
        )

    # Reached only when bug #899 is fixed and the transport is wired.
    _assert_latency_events(target_events, VOYAGE_TARGET)
