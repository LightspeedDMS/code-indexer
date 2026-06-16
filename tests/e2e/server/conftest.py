"""Phase 3 fixtures: FastAPI TestClient against in-process CIDX server.

These fixtures spin up a real CIDX server in-process using FastAPI's TestClient.
No subprocess, no port binding -- faster than Phase 4.

Admin credentials are read from E2E_ADMIN_USER / E2E_ADMIN_PASS environment
variables, which e2e-automation.sh sets for every phase before invoking pytest.

Log-audit gate (Story #1122)
----------------------------
All log-audit gate fixtures are unified onto the single test_client app instance.
test_client sets _app_module.app = fresh_app so admin_logs_query (which reads
app_module.app.state for log_db_path) reads the SAME state the tests drive.

log_audit_app_client  -- Alias for test_client (one app, one lifespan).
log_audit_admin_token -- JWT for the audit client.
log_watermark         -- Session watermark (max log id at phase start).
_phase3_log_audit_gate -- Autouse session fixture: fails the phase on any new
                          non-allowlisted ERROR/WARNING entry.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import _auth_headers

# Environment variable names that carry admin credentials.
# e2e-automation.sh sets these for all four phases before invoking pytest.
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"


def _require_env(name: str) -> str:
    """Return the value of environment variable *name* or raise RuntimeError.

    All required credentials must be supplied via environment variables set
    by e2e-automation.sh.  No hardcoded defaults exist in this file.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Run tests via e2e-automation.sh or export the variable manually."
        )
    return value


@pytest.fixture(scope="session")
def test_client_data_dir(tmp_path_factory) -> Iterator[Path]:
    """Isolated data directory for the TestClient server session.

    Sets CIDX_SERVER_DATA_DIR for the duration of the session and restores
    (or removes) the env var on teardown to avoid leaking mutable process state.
    """
    d = tmp_path_factory.mktemp("cidx_testclient_data")
    previous = os.environ.get("CIDX_SERVER_DATA_DIR")
    os.environ["CIDX_SERVER_DATA_DIR"] = str(d)
    yield d
    if previous is None:
        os.environ.pop("CIDX_SERVER_DATA_DIR", None)
    else:
        os.environ["CIDX_SERVER_DATA_DIR"] = previous


@pytest.fixture(scope="session")
def test_client(test_client_data_dir) -> Iterator[TestClient]:
    """Session-scoped TestClient against an in-process CIDX server.

    Calls create_app() directly so CIDX_SERVER_DATA_DIR is already set before
    service initialisation runs.  The module-level app singleton is created at
    import time; using create_app() gives a fresh app bound to our temp dir.

    After creating the fresh app we REPLACE the module-global app singleton so
    that admin_logs_query (which reads code_indexer.server.app.app.state for
    log_db_path) reads the SAME app instance that the tests drive.  Without
    this, admin_logs_query would read a DIFFERENT state object and return
    'Log database not configured', causing all log-audit gate tests to fail.
    """
    import code_indexer.server.app as _app_module
    from code_indexer.server.app import create_app

    fresh_app = create_app()
    # Point the module-global singleton at the fresh app before entering
    # the TestClient lifespan so admin_logs_query reads the right state.
    _app_module.app = fresh_app
    with TestClient(fresh_app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="session")
def admin_token(test_client: TestClient) -> str:
    """Obtain a JWT once per session using the admin account credentials from env."""
    resp = test_client.post(
        "/auth/login",
        json={
            "username": _require_env(_ENV_ADMIN_USER),
            "password": _require_env(_ENV_ADMIN_PASS),
        },
    )
    assert resp.status_code == 200, (
        f"Admin login failed: {resp.status_code} — {resp.text[:300]}"
    )
    return str(resp.json()["access_token"])


@pytest.fixture(scope="session")
def auth_headers(admin_token: str) -> dict:
    """Return authorization headers for the admin session.

    Delegates to the shared _auth_headers helper so no fixture assembles
    Authorization strings directly.
    """
    return _auth_headers(admin_token)


# ---------------------------------------------------------------------------
# Log-audit gate fixtures (Story #1122)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def log_audit_app_client(test_client: TestClient) -> Iterator[TestClient]:
    """TestClient for the log-audit gate — unified with test_client.

    Previously this fixture opened a SECOND TestClient on the module-level app
    singleton, causing two apps + two lifespans to share one process-global
    SQLiteLogHandler/SQLite connection.  They clobbered each other, and
    admin_logs_query (which reads app_module.app.state) hit a closed/
    uninitialized DB -> sqlite3.ProgrammingError.

    Fix: test_client now sets _app_module.app = fresh_app before entering its
    TestClient context, so admin_logs_query reads the SAME app state that the
    tests drive.  This fixture simply yields test_client -- one app, one
    lifespan, one SQLite connection.
    """
    yield test_client


@pytest.fixture(scope="session")
def log_audit_admin_token(log_audit_app_client: TestClient) -> str:
    """JWT for the log-audit gate admin session (against the singleton client)."""
    resp = log_audit_app_client.post(
        "/auth/login",
        json={
            "username": _require_env(_ENV_ADMIN_USER),
            "password": _require_env(_ENV_ADMIN_PASS),
        },
    )
    assert resp.status_code == 200, (
        f"log_audit_admin_token: login failed {resp.status_code} -- {resp.text[:300]}"
    )
    return str(resp.json()["access_token"])


@pytest.fixture(scope="session")
def log_watermark(log_audit_app_client: TestClient, log_audit_admin_token: str) -> int:
    """Record the maximum log id BEFORE the phase's tests run (watermark).

    Any log entry at or below this id was emitted during server startup,
    not during the phase under test.  The gate diffs against this watermark
    so pre-existing startup messages don't fail the phase.
    """
    from tests.e2e.log_audit_gate import get_log_watermark

    # Flush to drain any startup entries before recording the watermark
    handler = getattr(
        getattr(log_audit_app_client.app, "state", None), "sqlite_log_handler", None
    )
    if handler is not None:
        handler.flush()

    return get_log_watermark(log_audit_app_client, log_audit_admin_token)


@pytest.fixture(scope="session", autouse=True)
def _phase3_log_audit_gate(
    log_audit_app_client: TestClient,
    log_audit_admin_token: str,
    log_watermark: int,
) -> Iterator[None]:
    """Autouse session fixture: run the log-audit gate at Phase 3 teardown.

    Yields first (tests run), then at teardown:
      1. Flush SQLiteLogHandler (deterministic drain, Bug #1078 mitigation).
      2. Query admin_logs_query via MCP front door.
      3. Diff against log_watermark to find new entries.
      4. Fail with detailed report if any new non-allowlisted ERROR/WARNING found.
    """
    from tests.e2e.log_audit_gate import run_log_audit_gate

    yield  # Tests run here

    # --- Teardown: audit phase logs ---
    # Flush the async writer to drain buffered entries (Bug #1078)
    handler = getattr(
        getattr(log_audit_app_client.app, "state", None), "sqlite_log_handler", None
    )
    if handler is not None:
        handler.flush()

    result = run_log_audit_gate(
        log_audit_app_client,
        log_audit_admin_token,
        watermark_id=log_watermark,
        phase_name="Phase 3 (Server In-Process)",
    )
    if not result.passed:
        # pytest.fail() at session teardown surfaces as a test collection error;
        # raise AssertionError directly so it appears as a clear fixture failure.
        raise AssertionError(result.failure_message())
