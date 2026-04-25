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
  fault_workspace     -- git-backed workspace with cidx init --remote
  indexed_golden_repo -- "markupsafe" registered + indexed on fault server
  clear_all_faults    -- autouse, resets state before each test
"""

from __future__ import annotations

from pathlib import Path

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
    The markupsafe golden repo contains only Python files, so a valid result
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


def _assert_history_has(client: FaultAdminClient, target: str) -> None:
    """Assert fault history contains at least one event for *target*."""
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == 200
    found = {e["target"] for e in resp.json().get("history", [])}
    assert target in found, (
        f"Expected history events for {target!r}, got targets: {found!r}"
    )


def _assert_history_absent(client: FaultAdminClient, target: str) -> None:
    """Assert fault history contains zero events for *target*."""
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == 200
    found = {e["target"] for e in resp.json().get("history", [])}
    assert target not in found, (
        f"Expected NO history for {target!r}, but found events. targets={found!r}"
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
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """Parametrized AC1+AC2: killing one provider must not prevent results.

    For each (killed_target, surviving_label) pair:
      - Install 100% kill profile on killed_target
      - Run cidx query — must exit 0 and return at least one .py file-path result
      - Verify fault history has events for killed_target (profile actually fired)
      - Verify fault history has no events for the surviving provider
    """
    surviving_target = COHERE_TARGET if killed_target == VOYAGE_TARGET else VOYAGE_TARGET

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

    assert result.returncode == 0, (
        f"cidx query exited {result.returncode} with {killed_target!r} killed; "
        f"{surviving_label} must deliver. stderr:\n{result.stderr}"
    )
    _assert_stdout_has_py_result(result.stdout, indexed_golden_repo)

    # Kill profile must have fired — empty history would be a vacuous pass
    _assert_history_has(fault_admin_client, killed_target)
    # Surviving provider must not have been faulted
    _assert_history_absent(fault_admin_client, surviving_target)
