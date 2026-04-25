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
  "❌ No results found"  (Rich markup stripped by subprocess capture → "No results found")
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
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

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
# before the first letter, so "❌ No results found" → "no results found".
_LEADING_NOISE_RE = re.compile(
    r"^[\s☀-⛿✀-➿︀-﻿\U0001F000-\U0001FFFF❌✅⚠️\W]*"
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


def _assert_history_has(client: FaultAdminClient, target: str) -> None:
    """Assert fault history contains at least one event for *target*."""
    resp = client.get("/admin/fault-injection/history")
    assert resp.status_code == 200
    found = {e["target"] for e in resp.json().get("history", [])}
    assert target in found, (
        f"Expected history events for {target!r}, got targets: {found!r}"
    )


def _assert_graceful_empty_stdout(stdout: str) -> None:
    """Assert stdout is empty or exactly a 'No results found' message.

    Accepted:
      - empty or whitespace-only stdout
      - stdout that normalises exactly to "no results found" or "no results found."
        (handles emoji prefix like "❌ No results found" from cli.py:926)

    Rejected: any other content — provider error text must not surface to client.
    The check is an exact match on the normalised single-line content, so a
    message that merely contains "no results found" alongside other text is rejected.
    """
    stripped = stdout.strip()
    if not stripped:
        return  # empty stdout is accepted

    # Normalise and exact-match against accepted messages
    normalised = _normalise_for_comparison(stripped)
    if normalised in _ACCEPTED_EMPTY_NORMALISED:
        return

    raise AssertionError(
        f"cidx query stdout with both providers dead must be empty or exactly "
        f"'No results found'. Got (normalised): {normalised!r}\n"
        f"Raw stdout: {stripped[:300]!r}"
    )


def test_both_providers_dead_graceful_empty(
    fault_admin_client: FaultAdminClient,
    fault_http_client: httpx.Client,
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC3: With both providers killed, cidx query degrades gracefully.

    Assertions:
      - exit code 0 (graceful — no crash or unhandled error)
      - stdout is empty or exactly "No results found" (no provider errors surfaced)
      - GET /health returns < 500 (server still alive after both providers fail)
      - fault history has events for both api.voyageai.com and api.cohere.com
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

    # Graceful degradation: exit 0, not a crash
    assert result.returncode == 0, (
        f"cidx query must exit 0 when both providers are dead (graceful degradation). "
        f"Got exit {result.returncode}. stderr:\n{result.stderr}"
    )

    # stdout must be empty or exactly a "No results found" message
    _assert_graceful_empty_stdout(result.stdout)

    # Server must still be alive — GET /health must not return 5xx
    health_resp = fault_http_client.get("/health")
    assert health_resp.status_code < 500, (
        f"GET /health returned {health_resp.status_code} after both providers failed; "
        f"server must survive provider failures."
    )

    # Kill profiles must have fired — empty history would be a vacuous pass
    _assert_history_has(fault_admin_client, VOYAGE_TARGET)
    _assert_history_has(fault_admin_client, COHERE_TARGET)
