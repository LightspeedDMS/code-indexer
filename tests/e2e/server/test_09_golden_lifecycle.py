"""Phase 3 — AC9: Golden repo full lifecycle via REST and MCP.

Exercises the complete golden repository lifecycle end-to-end using the
in-process FastAPI TestClient:

  REST path:
    1. register   — POST /api/admin/golden-repos (JSON: repo_url + alias)
    2. wait       — poll GET /api/jobs/{job_id} until terminal
    3. activate   — POST /api/repos/activate (golden_repo_alias)
    4. wait       — poll GET /api/jobs/{job_id} until terminal
    5. query      — POST /api/query (query_text + repository_alias)
    6. refresh    — POST /api/admin/golden-repos/{alias}/refresh
    7. wait       — poll GET /api/jobs/{job_id} until terminal
    8. re-query   — POST /api/query (verify still works after refresh)
    9. deactivate — DELETE /api/repos/{user_alias}
    10. delete    — DELETE /api/admin/golden-repos/{alias}

  MCP path:
    Same lifecycle via add_golden_repo / activate_repository / search_code /
    refresh_golden_repo / deactivate_repository / remove_golden_repo tools.

Endpoint discovery (Story #705 / #711):
  - Correct path:   POST /api/admin/golden-repos
  - Correct body:   JSON with fields ``repo_url`` and ``alias``
  - WRONG path:     /admin/golden-repos/add  (404 — does not exist)
  - WRONG encoding: form-urlencoded (422 — server expects JSON body)
  - WRONG field:    ``url``  (422 — Pydantic expects ``repo_url``)

The MCP tool ``add_golden_repo`` accepts ``url`` in its inputSchema; the
handler maps ``url`` -> ``repo_url`` internally.

When activation is requested without an explicit ``user_alias``, the server
defaults the activated alias to ``golden_repo_alias``.

Environment variables:
  E2E_SEED_CACHE_DIR         base path for seed repos
                             (default: ~/.tmp/cidx-e2e-seed-repos)
  E2E_GOLDEN_JOB_TIMEOUT     max seconds to wait for a job (default: 120)
  E2E_GOLDEN_JOB_POLL        seconds between job polls (default: 0.5)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import call_mcp_tool

# ---------------------------------------------------------------------------
# Configuration resolved from environment variables
# ---------------------------------------------------------------------------
_SEED_CACHE_DIR: Path = Path(
    os.environ.get(
        "E2E_SEED_CACHE_DIR",
        str(Path.home() / ".tmp" / "cidx-e2e-seed-repos"),
    )
)
MARKUPSAFE_PATH: str = str(_SEED_CACHE_DIR / "markupsafe")

_JOB_TIMEOUT: float = float(os.environ.get("E2E_GOLDEN_JOB_TIMEOUT", "120"))
_JOB_POLL_INTERVAL: float = float(os.environ.get("E2E_GOLDEN_JOB_POLL", "0.5"))

_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


# ---------------------------------------------------------------------------
# Shared infrastructure helpers — exactly 3 in this operation
# ---------------------------------------------------------------------------


def _require_seed_repo() -> None:
    """Skip the test when the markupsafe seed repo directory is absent.

    This is the only condition that justifies a pytest.skip().  All runtime
    failures in the lifecycle use pytest.fail() or bare assert so they are
    reported as errors, never silenced.
    """
    if not Path(MARKUPSAFE_PATH).exists():
        pytest.skip(
            f"Seed repo not found at {MARKUPSAFE_PATH!r} — "
            "run e2e-automation.sh to pre-seed repos or set E2E_SEED_CACHE_DIR"
        )


def _wait_for_job(
    client: TestClient,
    job_id: str,
    auth_headers: dict[str, str],
) -> dict[str, Any]:
    """Poll GET /api/jobs/{job_id} until a terminal state is reached.

    Timeouts and poll intervals are resolved from environment variables
    (E2E_GOLDEN_JOB_TIMEOUT, E2E_GOLDEN_JOB_POLL) at module load time.

    Returns the final job status dict.
    Raises TimeoutError if the job does not complete within the timeout.
    The caller is responsible for asserting the returned status value.
    """
    deadline = time.monotonic() + _JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=auth_headers)
        assert resp.status_code < 500, (
            f"Job poll returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
        if resp.status_code == 200:
            body: dict[str, Any] = resp.json()
            if body.get("status") in _TERMINAL_STATES:
                return body
        time.sleep(_JOB_POLL_INTERVAL)
    raise TimeoutError(
        f"Job {job_id!r} did not reach a terminal state within {_JOB_TIMEOUT}s"
    )


def _parse_mcp_tool_result(resp_body: dict[str, Any]) -> dict[str, Any]:
    """Parse the tool result payload from a JSON-RPC 2.0 response body.

    CIDX MCP tools return a list of TextContent blocks under ``result``.
    Each block has a ``text`` field containing the JSON-serialised tool
    output dict.  The first successfully decoded dict is narrowed via an
    isinstance guard before being returned, so the return type is accurate
    without a type-ignore escape.

    Calls pytest.fail() (never returns normally) if no block can be parsed.
    """
    for item in resp_body.get("result", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                decoded = json.loads(item["text"])
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                continue
    pytest.fail(
        f"Could not parse MCP tool result from response: {resp_body!r}"
    )


# ---------------------------------------------------------------------------
# Lifecycle orchestration helpers — exactly 3 in this operation
# ---------------------------------------------------------------------------


def _assert_job_completed(status: dict[str, Any], label: str) -> None:
    """Assert that a job status dict reports the 'completed' terminal state.

    Args:
        status: Dict returned by _wait_for_job.
        label:  Human-readable label for the failure message.
    """
    assert status.get("status") == "completed", (
        f"{label} job ended with status {status.get('status')!r}: {status}"
    )


def _wait_on_job_id(
    source: dict[str, Any],
    client: TestClient,
    auth_headers: dict[str, str],
    label: str,
) -> None:
    """Extract ``job_id`` from *source* and wait for completion if present.

    Many CIDX async endpoints return ``{"job_id": "...", ...}``.  This helper
    extracts the id and delegates to _wait_for_job + _assert_job_completed.
    When job_id is absent or None the call is a no-op (some endpoints are
    synchronous).

    Args:
        source:       Dict that may contain a ``job_id`` key.
        client:       In-process TestClient.
        auth_headers: Authorization header dict.
        label:        Human-readable label forwarded to _assert_job_completed.
    """
    job_id: str | None = source.get("job_id")
    if job_id:
        status = _wait_for_job(client, job_id, auth_headers)
        _assert_job_completed(status, label)


def _run_lifecycle(
    register: Any,
    activate: Any,
    query: Any,
    refresh: Any,
    deactivate: Any,
    delete: Any,
) -> None:
    """Orchestrate the 7-step golden repo lifecycle via transport callables.

    Each argument is a zero-argument callable that executes one lifecycle
    step and raises AssertionError / calls pytest.fail() on failure.
    The lifecycle is:
      1. register  (+ wait internally)
      2. activate  (+ wait internally)
      3. query
      4. refresh   (+ wait internally)
      5. re-query
      6. deactivate
      7. delete

    Separating orchestration from transport eliminates the near-identical
    duplication between the REST and MCP test functions.
    """
    register()
    activate()
    query()
    refresh()
    query()   # re-query: same callable, verifies state survived refresh
    deactivate()
    delete()


def _rest_step(
    client: TestClient,
    method: str,
    path: str,
    auth_headers: dict[str, str],
    label: str,
    ok_statuses: tuple[int, ...] = (200, 202),
    *,
    json_body: dict[str, Any] | None = None,
) -> None:
    """Execute one REST lifecycle step: request → status check → job wait.

    Calls pytest.fail() on unexpected HTTP status codes.
    Waits for any returned job_id to reach a terminal completed state.

    Args:
        client:      TestClient bound to the CIDX app.
        method:      HTTP method string ("POST", "DELETE", …).
        path:        URL path relative to the server root.
        auth_headers: Authorization header dict.
        label:       Human-readable step name for failure messages.
        ok_statuses: Acceptable HTTP status codes (default 200, 202).
        json_body:   Optional JSON body for POST/PUT requests.
    """
    kwargs: dict[str, Any] = {"headers": auth_headers}
    if json_body is not None:
        kwargs["json"] = json_body
    resp = client.request(method, path, **kwargs)
    if resp.status_code not in ok_statuses:
        pytest.fail(
            f"REST {label} failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    if resp.status_code in (200, 202):
        body: dict[str, Any] = resp.json()
        _wait_on_job_id(body, client, auth_headers, label)
