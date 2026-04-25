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

The primary recovery assertion is sinbinned=false from GET /admin/provider-health.

Sinbin provider names (from semantic_query_manager.py):
  "voyage-ai"  -- VoyageAI embedding provider
  "cohere"     -- Cohere embedding/reranking provider

Target hostnames are fault-transport protocol constants, not environment config.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_workspace     -- git-backed workspace with cidx init --remote
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

NOTE (bug #899 — fault transport not wired into query-time embedding clients):
EmbeddingProviderFactory.create() constructs VoyageAIClient and
CohereEmbeddingProvider without passing http_client_factory.  Kill profiles
are accepted by the control plane (CRUD works) but never intercept actual
query-time embedding calls — Cohere never receives faulted calls so the
health monitor sinbin is never triggered regardless of how many queries run.

Current test execution path (documented current behavior):
  1. Cohere kill profile CRUD — passes (control plane works).
  2. QUERIES_TO_TRIGGER_SINBIN query loop — runs without error (providers work
     normally because fault transport is not wired).
  3. _get_sinbin_state() — passes (GET /admin/provider-health API call works).
  4. pytest.xfail() — marks AC5 as xfail immediately before the sinbin
     assertion: sinbin_state.get(COHERE_PROVIDER_NAME) is never True because
     Cohere never receives faulted calls. Phase B and final assertions are not
     retained as dead code after the xfail call.
     Removing this xfail and restoring the full Phase A/B/Final flow is the
     correct upgrade path once bug #899 is resolved.

See: https://github.com/LightspeedDMS/code-indexer/issues/899
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env
from tests.e2e.helpers import run_cidx

# Fault-transport protocol constants — not environment-specific configuration.
COHERE_TARGET = "api.cohere.com"

# Internal provider names used by ProviderHealthMonitor (from semantic_query_manager.py)
VOYAGE_PROVIDER_NAME = "voyage-ai"
COHERE_PROVIDER_NAME = "cohere"

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


def test_health_monitor_recovers_after_fault_reset(
    fault_admin_client: FaultAdminClient,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC5: After kill-profile + fault reset, both providers recover from sinbin.

    Current behavior (bug #899 — fault transport not wired):
      Phase A runs: kill profile CRUD succeeds, QUERIES_TO_TRIGGER_SINBIN
      queries complete without error, GET /admin/provider-health returns 200.
      However the sinbin assertion cannot pass: Cohere never receives faulted
      calls because the fault transport is not wired into query-time embedding
      clients, so the health monitor sinbin is never triggered.

    pytest.xfail() is placed between the _get_sinbin_state() call (which works)
    and the assertion on its value (which cannot pass). Phase B and the final
    recovery assertions are not retained as dead code after the xfail call.
    Restoring the full Phase A/B/Final flow is the upgrade path for bug #899.

    See: https://github.com/LightspeedDMS/code-indexer/issues/899
    """
    # Phase A: attempt to force Cohere into sinbin.
    # Kill profile CRUD passes; queries complete without error because the
    # fault transport is not wired — Cohere calls go through normally (bug #899).
    _install_kill_profile(fault_admin_client, COHERE_TARGET)
    for _ in range(QUERIES_TO_TRIGGER_SINBIN):
        run_cidx(
            "query",
            "escape",
            "--repos",
            f"{indexed_golden_repo}-global",
            "--quiet",
            cwd=str(fault_workspace),
            env=_build_cli_env(),
        )

    # Read provider health state — GET /admin/provider-health works (API is live).
    sinbin_state_before = _get_sinbin_state(fault_admin_client)

    # AC5 deep assertion boundary — xfail immediately before the sinbin check.
    # sinbin_state_before.get(COHERE_PROVIDER_NAME) is never True because Cohere
    # never receives faulted calls via the unwired fault transport.
    # When bug #899 is resolved: remove this xfail and restore:
    #   assert sinbin_state_before.get(COHERE_PROVIDER_NAME) is True, ...
    #   fault_admin_client.re_login()
    #   reset_resp = fault_admin_client.post("/admin/fault-injection/reset")
    #   assert reset_resp.status_code == 200, ...
    #   _wait_for_sinbin_recovery(...)
    #   final_result = _run_query(...)
    #   assert final_result.returncode == 0 and final_result.stdout.strip()
    pytest.xfail(
        reason=(
            f"bug #899: fault transport not wired into query-time embedding clients — "
            f"Cohere kill profile installed but sinbin never triggers "
            f"(cohere sinbinned={sinbin_state_before.get(COHERE_PROVIDER_NAME)!r}). "
            f"See https://github.com/LightspeedDMS/code-indexer/issues/899"
        )
    )
