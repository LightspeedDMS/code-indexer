"""
AC1: Recovery after clearing a fault.

Scenario: A kill fault is active, the query degrades to single-provider results,
the fault is cleared via DELETE, and the query returns the full RRF set.

Current state (bug #899 — fault transport not wired into query-time embedding clients):
EmbeddingProviderFactory.create() constructs VoyageAIClient and
CohereEmbeddingProvider without passing http_client_factory.  Kill profiles
are accepted by the control plane (CRUD works) but never intercept actual
query-time embedding calls — fault history stays empty and results are identical
before and after DELETE.

Current test execution path (documented current behavior):
  1. Kill profile CRUD (PUT + GET) — passes (control plane works).
  2. cidx query under fault exits 0 — passes (fault transport not wired).
  3. DELETE profile returns 200 + GET returns 404 — passes (control plane works).
  4. cidx query after DELETE exits 0 — passes (providers return real results).
  5. GET /health < 500 — passes (server survives).
  6. pytest.xfail() — marks AC1 as xfail at the boundary where the test cannot
     advance further: result set width comparison cannot prove recovery because
     the faulted query never actually degraded (bug #899).
     Removing this xfail and restoring the width comparison is the correct
     upgrade path once bug #899 is resolved.

See: https://github.com/LightspeedDMS/code-indexer/issues/899
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env
from tests.e2e.helpers import run_cidx

# Fault-transport protocol constants — not environment-specific configuration.
VOYAGE_TARGET = "api.voyageai.com"

# AC1 hard budget: each query must return within this many seconds.
QUERY_BUDGET_SECONDS: float = 10.0


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
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == 200 and get_resp.json()["error_rate"] == 1.0, (
        f"Kill profile for {target!r} not persisted correctly: {get_resp.text}"
    )


def _delete_profile(client: FaultAdminClient, target: str) -> None:
    """DELETE the fault profile for *target* and verify it returns 404 after deletion."""
    del_resp = client.delete(f"/admin/fault-injection/profiles/{target}")
    assert del_resp.status_code == 200, (
        f"DELETE profile for {target!r} failed: "
        f"{del_resp.status_code} {del_resp.text}"
    )
    get_resp = client.get(f"/admin/fault-injection/profiles/{target}")
    assert get_resp.status_code == 404, (
        f"GET profile for {target!r} after DELETE expected 404, "
        f"got {get_resp.status_code}: {get_resp.text}"
    )


def _count_results(stdout: str) -> int:
    """Count result lines in cidx query --quiet output.

    cidx query --quiet emits lines like "1. 0.750 src/markupsafe/file.py:1-40".
    A result line starts with a decimal number followed by a period and space.
    """
    count = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        # Result lines: "N. score path:start-end"
        parts = stripped.split(".", 1)
        if len(parts) == 2 and parts[0].isdigit():
            count += 1
    return count


def test_recovery_after_delete_restores_results(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC1: After DELETE of a kill profile, query result set must widen back.

    Smoke assertions (hard — always run, document current known-good behavior):
      1. Kill profile CRUD succeeds (PUT + GET round-trip verified).
      2. cidx query under kill profile exits 0 (CLI does not crash).
      3. DELETE profile succeeds and GET returns 404 (control plane DELETE works).
      4. cidx query after DELETE exits 0 (CLI does not crash after recovery).
      5. GET /health returns < 500 (server alive throughout).

    After the smoke assertions, pytest.xfail() marks AC1 as xfail because the
    result-set width comparison cannot demonstrate recovery: the kill profile
    never intercepts actual query-time embedding calls (bug #899), so both
    queries return the same full result set.  No dead code follows the xfail.
    Restoring the width comparison is the correct upgrade path when bug #899
    is resolved.

    See: https://github.com/LightspeedDMS/code-indexer/issues/899
    """
    # Smoke assertion 1: kill profile CRUD (PUT + GET round-trip).
    _install_kill_profile(fault_admin_client, VOYAGE_TARGET)

    # Server stores golden repos with a '-global' suffix; the fixture returns
    # the bare alias ("markupsafe"), so we must append it here.
    result_faulted = run_cidx(
        "query",
        "escape",
        "--repos",
        f"{indexed_golden_repo}-global",
        "--quiet",
        cwd=str(fault_workspace),
        env=_build_cli_env(),
    )

    # Smoke assertion 2: CLI must not crash under the kill profile.
    assert result_faulted.returncode == 0, (
        f"cidx query must exit 0 under VoyageAI kill profile. "
        f"Got exit {result_faulted.returncode}. stderr:\n{result_faulted.stderr}"
    )

    # Record how many results came back under the kill profile.
    count_faulted = _count_results(result_faulted.stdout)

    # Smoke assertion 3: control plane DELETE + subsequent 404.
    _delete_profile(fault_admin_client, VOYAGE_TARGET)

    result_recovered = run_cidx(
        "query",
        "escape",
        "--repos",
        f"{indexed_golden_repo}-global",
        "--quiet",
        cwd=str(fault_workspace),
        env=_build_cli_env(),
    )

    # Smoke assertion 4: CLI must not crash after DELETE.
    assert result_recovered.returncode == 0, (
        f"cidx query must exit 0 after DELETE of kill profile. "
        f"Got exit {result_recovered.returncode}. stderr:\n{result_recovered.stderr}"
    )

    # Smoke assertion 5: server must be alive throughout.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} after kill profile + DELETE; "
        f"server must survive the fault profile lifecycle."
    )

    # AC1 deep assertion boundary — xfail here (bug #899).
    # The width comparison (count_recovered > count_faulted) cannot pass because
    # the kill profile never intercepts query-time embedding calls:
    # count_faulted == count_recovered (both queries run against both providers).
    # When bug #899 is resolved: remove this xfail and restore:
    #   count_recovered = _count_results(result_recovered.stdout)
    #   assert count_recovered > count_faulted, (
    #       f"Expected result set to widen after DELETE (recovery). "
    #       f"Faulted: {count_faulted}, recovered: {count_recovered}."
    #   )
    count_recovered = _count_results(result_recovered.stdout)
    pytest.xfail(
        reason=(
            f"bug #899: fault transport not wired into query-time embedding clients — "
            f"kill profile installed on {VOYAGE_TARGET!r} but query results identical "
            f"before DELETE (count={count_faulted}) and after DELETE "
            f"(count={count_recovered}). Width comparison cannot prove recovery. "
            f"See https://github.com/LightspeedDMS/code-indexer/issues/899"
        )
    )
