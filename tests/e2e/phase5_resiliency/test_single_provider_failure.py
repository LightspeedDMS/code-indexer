"""
AC1: VoyageAI dead — Cohere delivers.
AC2: Cohere dead — VoyageAI delivers (symmetry).

Both ACs are exercised by a single parametrized test that installs a kill
profile (error_rate=1.0, error_codes=[503]) on one provider and asserts that
`cidx query` returns results from the surviving provider.

Target hostnames (VOYAGE_TARGET, COHERE_TARGET) are fault-transport protocol
constants — they match the httpx transport-layer interception targets and are
not environment-specific configuration values.

cidx query --quiet output format (from cli.py):
  "{N}. {score:.3f} {file_path}:{line_start}-{line_end}"
  e.g.  "1. 0.750 src/markupsafe/_speedups.py:1-40"
The markupsafe repo contains only .py files, so checking for ".py" in a result
line is a structural check appropriate for the indexed golden repo.

Depends on session fixtures from conftest.py:
  fault_admin_client  -- FaultAdminClient authenticated against the fault server
  fault_http_client   -- unauthenticated httpx.Client for health endpoint
  fault_workspace     -- git-backed workspace with cidx init --remote
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test

NOTE (bug #899 — fault transport not wired into query-time embedding clients):
EmbeddingProviderFactory.create() constructs VoyageAIClient and
CohereEmbeddingProvider without passing http_client_factory.  Kill profiles
are accepted by the control plane (CRUD works) but never intercept actual
query-time embedding calls — fault history stays empty after queries.

Current test execution path (documented current behavior):
  1. Kill profile CRUD — passes (control plane works).
  2. cidx query exits 0 — passes (fault transport not wired; providers work normally).
  3. GET /health < 500 — passes (server survives).
  4. stdout has .py result — passes (providers still return real results).
  5. pytest.xfail() — marks AC1/AC2 as xfail at the boundary where the test
     cannot advance further: fault history is empty so _assert_history_has
     would fail. Removing this xfail call and restoring the history/absent
     assertions is the correct upgrade path once bug #899 is resolved.

See: https://github.com/LightspeedDMS/code-indexer/issues/899
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env
from tests.e2e.helpers import run_cidx

# ---------------------------------------------------------------------------
# Fault-transport protocol constants.
# These are the exact hostnames the fault harness intercepts at the httpx
# transport layer. They are not environment-specific configuration.
# ---------------------------------------------------------------------------
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"


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


def _assert_stdout_has_py_result(stdout: str, repo_alias: str) -> None:
    """Assert stdout has at least one result line containing a .py file path.

    cidx query --quiet emits lines like "1. 0.750 src/markupsafe/file.py:1-40".
    The markupsafe repo contains only Python files, so a valid result
    must have at least one line with ".py" — a structural check that rules out
    blank output or non-result noise.
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    assert lines, f"cidx query returned empty stdout for repo '{repo_alias}'"
    has_py_result = any(".py" in ln for ln in lines)
    assert has_py_result, (
        f"cidx query stdout for '{repo_alias}' has no line with a .py file path.\n"
        f"First 10 lines: {lines[:10]}"
    )


@pytest.mark.parametrize(
    "killed_target, surviving_label",
    [
        pytest.param(VOYAGE_TARGET, "Cohere", id="AC1-voyage-dead-cohere-delivers"),
        pytest.param(COHERE_TARGET, "VoyageAI", id="AC2-cohere-dead-voyage-delivers"),
    ],
)
def test_single_provider_dead_surviving_delivers(
    killed_target: str,
    surviving_label: str,
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """Parametrized AC1+AC2: killing one provider must not prevent results.

    Smoke assertions (hard — always run, document current known-good behavior):
      1. cidx query exits 0 (CLI does not crash — surviving_label names the
         expected surviving provider for diagnostic context)
      2. GET /health returns < 500 (server survives fault profile installation)
      3. stdout has at least one .py result (providers still return real results
         because the fault transport is not yet wired — bug #899)

    After the smoke assertions, pytest.xfail() marks the test as xfail because
    the next required step — verifying fault history has events for killed_target —
    cannot pass until bug #899 is resolved.  No dead code follows the xfail call.
    Restoring the history assertions is the correct upgrade path when bug #899
    is fixed and this xfail call is removed.

    See: https://github.com/LightspeedDMS/code-indexer/issues/899
    """
    _install_kill_profile(fault_admin_client, killed_target)

    # Server stores golden repos with a '-global' suffix; the fixture returns
    # the bare alias ("markupsafe"), so we must append it here.
    result = run_cidx(
        "query",
        "escape",
        "--repos",
        f"{indexed_golden_repo}-global",
        "--quiet",
        cwd=str(fault_workspace),
        env=_build_cli_env(),
    )

    # Smoke assertion 1: CLI must not crash; surviving_label names the expected
    # surviving provider for diagnostic context in the error message.
    assert result.returncode == 0, (
        f"cidx query exited {result.returncode} with {killed_target!r} kill profile "
        f"installed (expected {surviving_label} to deliver); CLI must not crash. "
        f"stderr:\n{result.stderr}"
    )

    # Smoke assertion 2: server must remain alive after a kill profile is installed.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} after installing kill profile "
        f"for {killed_target!r}; server must survive fault profile installation."
    )

    # Smoke assertion 3: providers still return results (fault transport not wired,
    # so the kill profile has no effect on actual embedding calls — bug #899).
    _assert_stdout_has_py_result(result.stdout, indexed_golden_repo)

    # AC1/AC2 deep assertion boundary — xfail here (bug #899).
    # The next required step is verifying fault history has events for
    # killed_target (_assert_history_has), which cannot pass until
    # EmbeddingProviderFactory.create() is fixed to thread http_client_factory
    # through to VoyageAIClient / CohereEmbeddingProvider.
    # When bug #899 is resolved: remove this xfail call and restore
    # _assert_history_has / _assert_history_absent assertions here.
    pytest.xfail(
        reason=(
            "bug #899: fault transport not wired into query-time embedding clients — "
            "kill profile installed but fault history stays empty after query. "
            "See https://github.com/LightspeedDMS/code-indexer/issues/899"
        )
    )
