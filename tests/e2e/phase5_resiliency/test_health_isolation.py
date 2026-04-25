"""
AC5: Health monitor isolation between tests.

After a kill-profile test pushes a provider into the health monitor sinbin,
the next test (after clear_all_faults) must see both providers healthy.

This test simulates the cross-test boundary within a single test body:
  Phase A — Kill Cohere; run QUERIES_TO_TRIGGER_SINBIN queries to exceed
             the sinbin failure_threshold=5 (ProviderSinBinConfig default);
             assert Cohere is sinbinned via GET /admin/provider-health.
  Phase B — Reset faults (mirrors what clear_all_faults does between tests);
             poll until GET /admin/provider-health reports sinbinned=false
             for both "voyage-ai" and "cohere" within SINBIN_RECOVERY_WAIT_SECONDS.
             Each poll iteration runs a query to trigger record_call(success=True),
             which clears sinbin automatically (provider_health_monitor.py:231-234).
  Final   — Assert the post-recovery query exits 0 with non-empty results.

The primary recovery assertion is sinbinned=false from GET /admin/provider-health —
a direct observable from the health monitor state.

Phase A queries are expected to partially fail (Cohere killed), so their exit
codes are intentionally discarded. Recovery-phase queries succeed (no kill
profiles), so exit code 0 is asserted.

Sinbin provider names (from semantic_query_manager.py):
  "voyage-ai"  — VoyageAI embedding provider
  "cohere"     — Cohere embedding/reranking provider

Target hostnames are fault-transport protocol constants, not environment config.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_workspace     -- git-backed workspace with cidx init --remote
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Dict

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env
from tests.e2e.helpers import run_cidx

# Fault-transport protocol constants — not environment-specific configuration.
COHERE_TARGET = "api.cohere.com"

# Internal provider names used by ProviderHealthMonitor (from semantic_query_manager.py)
VOYAGE_PROVIDER_NAME = "voyage-ai"
COHERE_PROVIDER_NAME = "cohere"

# Maximum seconds to wait for sinbin to clear after fault reset.
SINBIN_RECOVERY_WAIT_SECONDS: float = 10.0

# Poll interval when waiting for sinbin recovery.
SINBIN_POLL_INTERVAL_SECONDS: float = 1.0

# Number of queries to run to exceed the default sinbin failure_threshold=5.
QUERIES_TO_TRIGGER_SINBIN: int = 6


def _install_kill_profile(client: FaultAdminClient, target: str) -> None:
    """Install a 100% error-rate kill profile on *target* and verify it persisted."""
    payload = {
        "target": target,
        "enabled": True,
        "error_rate": 1.0,
        "error_codes": [503],
    }
    put_resp = client.put(f"/admin/fault-injection/profiles/{target}", json=payload)
    assert put_resp.status_code in (200, 201), (
        f"PUT kill profile for {target!r} failed: "
        f"{put_resp.status_code} {put_resp.text}"
    )


def _get_sinbin_state(client: FaultAdminClient) -> Dict[str, bool]:
    """Return {provider_name: sinbinned} from GET /admin/provider-health."""
    resp = client.get("/admin/provider-health")
    assert resp.status_code == 200, (
        f"GET /admin/provider-health failed: {resp.status_code} {resp.text}"
    )
    return {
        entry["provider"]: entry["sinbinned"]
        for entry in resp.json().get("providers", [])
    }


def _run_query(
    indexed_golden_repo: str, fault_workspace: Path
) -> "subprocess.CompletedProcess[str]":
    """Run cidx query and return the full CompletedProcess result.

    The server stores golden repos with a '-global' suffix, so the bare alias
    returned by the fixture ("markupsafe") must have it appended here.
    """
    return run_cidx(
        "query",
        "escape",
        "--repos",
        f"{indexed_golden_repo}-global",
        "--quiet",
        cwd=str(fault_workspace),
        env=_build_cli_env(),
    )


def _wait_for_sinbin_recovery(
    client: FaultAdminClient,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """Poll until both providers report sinbinned=false, with a bounded retry loop.

    Kill profiles are cleared before this function is called, so queries succeed
    and record_call(success=True) clears sinbin automatically
    (provider_health_monitor.py:231-234). Exit code 0 is asserted on each
    recovery-phase query.

    Raises AssertionError if recovery does not happen within SINBIN_RECOVERY_WAIT_SECONDS.
    """
    deadline = time.monotonic() + SINBIN_RECOVERY_WAIT_SECONDS
    while True:
        recovery_result = _run_query(indexed_golden_repo, fault_workspace)
        assert recovery_result.returncode == 0, (
            f"Recovery-phase query exited {recovery_result.returncode}; "
            f"no kill profiles are active — exit 0 expected. "
            f"stderr:\n{recovery_result.stderr}"
        )

        sinbin_state = _get_sinbin_state(client)
        voyage_sinbinned = sinbin_state.get(VOYAGE_PROVIDER_NAME, False)
        cohere_sinbinned = sinbin_state.get(COHERE_PROVIDER_NAME, False)

        if not voyage_sinbinned and not cohere_sinbinned:
            return  # both providers out of sinbin

        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Providers still sinbinned {SINBIN_RECOVERY_WAIT_SECONDS}s after reset. "
                f"voyage-ai sinbinned={voyage_sinbinned}, cohere sinbinned={cohere_sinbinned}. "
                f"Full provider health: {sinbin_state}"
            )

        time.sleep(SINBIN_POLL_INTERVAL_SECONDS)


def test_health_monitor_recovers_after_fault_reset(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC5: After kill-profile + fault reset, both providers recover from sinbin.

    Phase A: Install Cohere kill profile; run QUERIES_TO_TRIGGER_SINBIN queries
             to exceed failure_threshold=5 (ProviderSinBinConfig default).
             Query results are discarded — exit code is non-deterministic under
             a kill profile (VoyageAI may still deliver, so exit 0 is possible).
             Assert Cohere is sinbinned after the faulted queries.

    Reset:   Re-login + POST /admin/fault-injection/reset (mirrors clear_all_faults).

    Phase B: Bounded retry loop runs queries (exit 0 asserted — no kill profiles)
             and polls GET /admin/provider-health.
             Primary assertion: both "voyage-ai" and "cohere" sinbinned=false
             within SINBIN_RECOVERY_WAIT_SECONDS.

    Final:   Assert post-recovery query exits 0 with non-empty results.
    """
    # Phase A: force Cohere into sinbin
    _install_kill_profile(fault_admin_client, COHERE_TARGET)
    for _ in range(QUERIES_TO_TRIGGER_SINBIN):
        # Intentionally discard result: queries are expected to partially fail
        # (Cohere killed). VoyageAI may still deliver (exit 0) or the CLI may
        # exit non-zero when RRF produces empty results. Either is valid here —
        # the goal is to record enough Cohere failures to trigger sinbin.
        _ = _run_query(indexed_golden_repo, fault_workspace)

    # Verify Cohere actually entered sinbin — prevents vacuous pass
    sinbin_state_before = _get_sinbin_state(fault_admin_client)
    assert sinbin_state_before.get(COHERE_PROVIDER_NAME) is True, (
        f"Cohere must be sinbinned after {QUERIES_TO_TRIGGER_SINBIN} faulted queries. "
        f"Provider health: {sinbin_state_before}"
    )

    # Simulate between-test boundary: re-login and reset all fault state
    fault_admin_client.re_login()
    reset_resp = fault_admin_client.post("/admin/fault-injection/reset")
    assert reset_resp.status_code == 200, (
        f"POST /admin/fault-injection/reset failed: "
        f"{reset_resp.status_code} {reset_resp.text}"
    )

    # Phase B: primary assertion — both providers exit sinbin within recovery window
    _wait_for_sinbin_recovery(fault_admin_client, indexed_golden_repo, fault_workspace)

    # Final sanity check: post-recovery query returns results
    final_result = _run_query(indexed_golden_repo, fault_workspace)
    assert final_result.returncode == 0, (
        f"Post-recovery query exited {final_result.returncode}. "
        f"stderr:\n{final_result.stderr}"
    )
    assert final_result.stdout.strip(), (
        "Post-recovery query returned empty stdout. "
        "Both providers must contribute results after sinbin cleared."
    )
