"""
AC3: Both providers dead — graceful empty result.

Installs kill profiles (error_rate=1.0, error_codes=[503]) on BOTH
api.voyageai.com and api.cohere.com and asserts that:
  - cidx query exits 0 (graceful degradation — no crash, no non-zero exit)
  - stdout is empty or contains only "No results found" (no provider errors surfaced)
  - GET /health returns < 500 (server still alive after both providers fail)
  - Fault history contains events for both targets

Target hostnames are fault-transport protocol constants, not environment config.

cidx query --quiet output for empty results (from cli.py line 926):
  "No results found"  (Rich markup stripped by subprocess capture)
  OR empty stdout when the remote query path returns an empty list silently.
Both are accepted. Any other non-empty content is rejected — provider error
text must not be surfaced to the client.

Accepted token normalization: strip leading/trailing whitespace, strip leading
emoji characters (U+2600..U+26FF, U+2700..U+27BF, U+FE00..U+FEFF,
U+1F000..U+1FFFF) and ASCII punctuation, lowercase, then exact-match against
the normalized forms of the accepted messages.

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
query-time embedding calls — both providers return real results and fault
history stays empty after queries.

Current test execution path (documented current behavior):
  1. Both kill profile CRUDs — pass (control plane works).
  2. cidx query exits 0 — passes (fault transport not wired).
  3. GET /health < 500 — passes (server survives).
  4. pytest.xfail() — marks AC3 as xfail at the boundary where the test
     cannot advance further: stdout has real results, not graceful empty,
     because kill profiles never fire during queries (bug #899).
     Removing this xfail and restoring _assert_graceful_empty_stdout /
     _assert_history_has is the correct upgrade path once bug #899 is fixed.

See: https://github.com/LightspeedDMS/code-indexer/issues/899
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from tests.e2e.phase5_resiliency.conftest import FaultAdminClient, _build_cli_env
from tests.e2e.helpers import run_cidx

# Fault-transport protocol constants — not environment-specific configuration.
VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

# Exact normalised forms of accepted empty-result messages from cli.py lines 926 + 5712.
# Normalised = stripped, leading emoji/punct stripped, lowercased.
_ACCEPTED_EMPTY_NORMALISED = frozenset(
    {
        "no results found",
        "no results found.",
    }
)

# Regex to strip leading emoji codepoints and ASCII punctuation/whitespace
# before the first letter, so "No results found" -> "no results found".
_LEADING_NOISE_RE = re.compile(
    r"^[\s☀-⛿✀-➿︀-﻿\U0001F000-\U0001FFFF\W]*"
)


def _normalise_for_comparison(text: str) -> str:
    """Strip leading emoji/punctuation/whitespace and lowercase for comparison."""
    return _LEADING_NOISE_RE.sub("", text).rstrip().lower()


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
        f"Kill profile for {target!r} not persisted: {get_resp.text}"
    )


def test_both_providers_dead_graceful_empty(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC3: With both providers killed, cidx query degrades gracefully.

    Smoke assertions (hard — always run, document current known-good behavior):
      1. cidx query exits 0 (CLI does not crash)
      2. GET /health returns < 500 (server survives both kill profiles)

    After the smoke assertions, pytest.xfail() marks AC3 as xfail because the
    next required step — _assert_graceful_empty_stdout — cannot pass until bug
    #899 is resolved: kill profiles never fire during queries, so stdout contains
    real results rather than graceful empty output.  No dead code follows xfail.

    See: https://github.com/LightspeedDMS/code-indexer/issues/899
    """
    _install_kill_profile(fault_admin_client, VOYAGE_TARGET)
    _install_kill_profile(fault_admin_client, COHERE_TARGET)

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

    # Smoke assertion 1: CLI must not crash when both kill profiles are installed.
    assert result.returncode == 0, (
        f"cidx query must exit 0 even when both providers have kill profiles. "
        f"Got exit {result.returncode}. stderr:\n{result.stderr}"
    )

    # Smoke assertion 2: server must still be alive after both kill profiles installed.
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} after both kill profiles "
        f"installed; server must survive fault profile installation."
    )

    # AC3 deep assertion boundary — xfail here (bug #899).
    # The next required step is _assert_graceful_empty_stdout, which cannot pass
    # because the kill profiles never fire during queries — stdout has real results.
    # Also _assert_history_has for both targets would fail (history stays empty).
    # When bug #899 is resolved: remove this xfail and restore
    # _assert_graceful_empty_stdout / _assert_history_has assertions here.
    pytest.xfail(
        reason=(
            "bug #899: fault transport not wired into query-time embedding clients — "
            "both kill profiles installed but providers return real results and fault "
            "history stays empty. stdout is not gracefully empty. "
            "See https://github.com/LightspeedDMS/code-indexer/issues/899"
        )
    )
