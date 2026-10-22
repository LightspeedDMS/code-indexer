"""
Shared pytest fixtures for Phase 6 PostgreSQL parity E2E tests.

Session-scoped fixtures:
  pg_server_url          -- base URL of the Phase 6 PG-backed uvicorn
  pg_http_client         -- httpx.Client bound to pg_server_url
  pg_admin_token         -- JWT from the PG-backed server
  pg_registered_repo     -- marks the markupsafe golden repo on the PG server
  pg_activated_repo      -- activates the repo on the PG server

Log-audit gate fixtures (Story #1122 / Phase 6):
  log_watermark_phase6   -- max log id at phase start
  _phase6_log_audit_gate -- autouse session fixture; fails if new ERROR/WARNING found

Note: the log-audit gate reads from logs.db (ALWAYS SQLite, regardless of
storage_mode) per the story DoD correction -- the PG backend is used for
CIDX data, not for the operational log store.
"""

from __future__ import annotations

import os
from typing import Any, Iterator, Optional, Tuple

import httpx
import pytest

from tests.e2e.helpers import (
    require_postgres,
    require_voyage_key,
    rest_call,
    wait_for_job,
    wait_for_repo_activation,
)
from tests.e2e.server.conftest import AdminTokenProvider


# ---------------------------------------------------------------------------
# PG-server URL and HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_server_url() -> str:
    """Return the base URL of the Phase 6 PG-backed uvicorn subprocess.

    The harness starts a live uvicorn against a PostgreSQL config and sets
    E2E_PG_SERVER_HOST / E2E_PG_SERVER_PORT before invoking pytest.
    """
    require_postgres()
    host = os.environ.get("E2E_PG_SERVER_HOST", "127.0.0.1")
    port = os.environ.get("E2E_PG_SERVER_PORT", "8901")
    return f"http://{host}:{port}"


@pytest.fixture(scope="session")
def pg_http_client(pg_server_url: str) -> Iterator[httpx.Client]:
    """Yield a session-scoped httpx.Client bound to the PG-backed server."""
    with httpx.Client(base_url=pg_server_url) as client:
        yield client


@pytest.fixture(scope="session")
def pg_admin_token_provider(pg_server_url: str) -> AdminTokenProvider:
    """Session-scoped auto-refreshing admin token for the PG-backed server.

    Reads credentials from the same E2E_ADMIN_USER / E2E_ADMIN_PASS env
    vars used by all other phases. Fixtures that run at the END of the
    phase (the log-audit teardown) call ``get_token()`` there, so a phase
    longer than the token lifetime never presents an expired token.
    """
    admin_user = os.environ.get("E2E_ADMIN_USER", "")
    admin_pass = os.environ.get("E2E_ADMIN_PASS", "")
    if not admin_user or not admin_pass:
        pytest.skip("E2E_ADMIN_USER / E2E_ADMIN_PASS not set")

    def _relogin() -> Tuple[str, Optional[str]]:
        resp = httpx.post(
            f"{pg_server_url}/auth/login",
            json={"username": admin_user, "password": admin_pass},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        return str(body["access_token"]), body.get("refresh_token")

    initial_access, initial_refresh = _relogin()
    return AdminTokenProvider(
        login_fn=_relogin,
        initial_access_token=initial_access,
        initial_refresh_token=initial_refresh,
    )


@pytest.fixture(scope="session")
def pg_admin_token(pg_admin_token_provider: AdminTokenProvider) -> str:
    """An admin JWT for session-scoped setup fixtures (used at setup time)."""
    return pg_admin_token_provider.get_token()


# ---------------------------------------------------------------------------
# Golden repo registration on the PG server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_registered_repo(
    pg_http_client: httpx.Client,
    pg_admin_token: str,
) -> str:
    """Register the markupsafe golden repo on the PG-backed server.

    Returns the alias string ("markupsafe").

    Requires E2E_SEED_CACHE_DIR to locate the markupsafe seed clone and
    VOYAGE_API_KEY for the indexing step.
    """
    require_voyage_key()
    seed_cache_dir = os.environ.get("E2E_SEED_CACHE_DIR", "")
    if not seed_cache_dir:
        pytest.skip("E2E_SEED_CACHE_DIR not set")

    import pathlib

    repo_path = str(pathlib.Path(seed_cache_dir) / "markupsafe")
    alias = "markupsafe"

    response = rest_call(
        pg_http_client,
        "POST",
        "/api/admin/golden-repos",
        token=pg_admin_token,
        json={"repo_url": repo_path, "alias": alias},
    )
    response.raise_for_status()

    body: dict[str, Any] = response.json()
    job_id: str = body["job_id"]

    job_timeout = float(os.environ.get("E2E_GOLDEN_REPO_JOB_TIMEOUT", "300.0"))
    job_status = wait_for_job(
        pg_http_client,
        job_id,
        token=pg_admin_token,
        timeout=job_timeout,
        poll_interval=2.0,
    )
    assert job_status["status"] == "completed", (
        f"PG golden repo registration job did not complete successfully:\n{job_status}"
    )
    return alias


@pytest.fixture(scope="session")
def pg_activated_repo(
    pg_http_client: httpx.Client,
    pg_admin_token: str,
    pg_registered_repo: str,
) -> str:
    """Activate the markupsafe golden repo on the PG-backed server.

    Uses the REST API directly (no CLI needed; tests the server front door).
    The correct endpoint is POST /api/repos/activate with body
    {"golden_repo_alias": alias} -- returns 202 (async job) or 200 (idempotent).
    Returns the alias string ("markupsafe").
    """
    alias = pg_registered_repo

    # POST /api/repos/activate -- the real activation endpoint
    response = rest_call(
        pg_http_client,
        "POST",
        "/api/repos/activate",
        token=pg_admin_token,
        json={"golden_repo_alias": alias},
    )
    # 202 = new async job; 200 = duplicate/already-active (idempotent)
    assert response.status_code in (200, 202), (
        f"Activation returned unexpected status {response.status_code}: {response.text[:300]}"
    )

    body: dict[str, Any] = response.json()
    job_id: str | None = body.get("job_id") or None

    # If a job was started, wait for it to reach a terminal state
    if job_id:
        job_timeout = float(os.environ.get("E2E_GOLDEN_REPO_JOB_TIMEOUT", "300.0"))
        job_status = wait_for_job(
            pg_http_client,
            job_id,
            token=pg_admin_token,
            timeout=job_timeout,
            poll_interval=2.0,
        )
        assert job_status["status"] == "completed", (
            f"PG activation job did not complete successfully:\n{job_status}"
        )

    # Poll GET /api/repos/{alias} until the repo is visible as activated
    wait_for_repo_activation(
        pg_http_client,
        alias=alias,
        token=pg_admin_token,
        timeout=90.0,
    )
    return alias


# ---------------------------------------------------------------------------
# Log-audit gate (Phase 6)
# Log store is ALWAYS logs.db (SQLite) regardless of storage_mode=postgres.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def log_watermark_phase6(
    pg_http_client: httpx.Client,
    pg_admin_token: str,
) -> int:
    """Record the maximum log id BEFORE Phase 6 tests run (watermark).

    Uses poll_until_stable_count to wait for the live PG-backed server's
    async log writer to drain before recording the watermark.
    """
    from tests.e2e.log_audit_gate import (
        get_log_watermark,
        poll_until_stable_count,
        query_logs_via_mcp,
    )

    poll_until_stable_count(
        count_fn=lambda: len(query_logs_via_mcp(pg_http_client, pg_admin_token)),
        max_attempts=10,
        sleep_seconds=0.3,
    )
    return get_log_watermark(pg_http_client, pg_admin_token)


@pytest.fixture(scope="session", autouse=True)
def _phase6_log_audit_gate(
    pg_http_client: httpx.Client,
    pg_admin_token_provider: AdminTokenProvider,
    log_watermark_phase6: int,
    pytestconfig: pytest.Config,
) -> Iterator[None]:
    """Autouse session fixture: run the log-audit gate at Phase 6 teardown.

    Operational log store is ALWAYS SQLite logs.db -- not the PG backend.
    poll_until_stable_count drains the async writer before auditing.  The
    result is recorded for ``pytest_sessionfinish`` below. The token is
    fetched at teardown: the phase may outlive a token issued at setup.
    """
    from tests.e2e.log_audit_claims import record_gate_result
    from tests.e2e.log_audit_gate import (
        poll_until_stable_count,
        query_logs_via_mcp,
        run_log_audit_gate,
    )

    yield  # Tests run here

    # --- Teardown: audit phase logs ---
    poll_until_stable_count(
        count_fn=lambda: len(
            query_logs_via_mcp(pg_http_client, pg_admin_token_provider.get_token())
        ),
        max_attempts=10,
        sleep_seconds=0.3,
    )
    result = run_log_audit_gate(
        pg_http_client,
        pg_admin_token_provider.get_token(),
        watermark_id=log_watermark_phase6,
        phase_name="Phase 6 (PostgreSQL Parity)",
    )
    record_gate_result(pytestconfig, result)
    if not result.passed:
        raise AssertionError(result.failure_message())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A failed Phase 6 log audit makes the session exit nonzero."""
    from tests.e2e.log_audit_claims import apply_gate_exit_status

    apply_gate_exit_status(session)
