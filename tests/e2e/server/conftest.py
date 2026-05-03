"""Phase 3 fixtures: FastAPI TestClient against in-process CIDX server.

These fixtures spin up a real CIDX server in-process using FastAPI's TestClient.
No subprocess, no port binding — faster than Phase 4.

Admin credentials are read from E2E_ADMIN_USER / E2E_ADMIN_PASS environment
variables, which e2e-automation.sh sets for every phase before invoking pytest.
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
    """
    from code_indexer.server.app import create_app

    fresh_app = create_app()
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
