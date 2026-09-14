"""Phase 3 -- Regression: a second create_app() must not corrupt shared
auth globals for the rest of the pytest process.

Root cause (found diagnosing e2e-automation.sh --phase 3, 2026-09-06):
create_app() -> app_wiring.create_fastapi_app() wires several services as
MODULE-LEVEL globals on code_indexer.server.auth.dependencies (jwt_manager,
user_manager, oauth_manager, mcp_credential_manager, server_config,
api_key_manager) -- correct for production (exactly one create_app() per
process), but a hazard in this shared Phase 3 pytest process where some
tests legitimately build a SECOND, throwaway create_app() instance
(test_20_telemetry_metrics_wiring_1586.py's TestLifespanRealStartupWiring,
test_21_otel_live_collector_1676.py).

Without conftest.py's ``_restore_auth_dependencies_globals`` autouse guard,
that second call permanently overwrites these globals: the shared session
``test_client`` app keeps minting JWTs with its OWN jwt_manager
(closure-bound into the login route at app-build time), but every later
request validates that JWT against the POISONED global jwt_manager (wrong
secret_key) -- 401 "Authentication required" / "Invalid token" for the
rest of the session, unfixable by re-login or token refresh, because
minting and validation are now permanently split across two different
jwt_manager instances. Observed live: 14 real failures plus 21 tests
masked as "HTTP 401 (4xx acceptable)" skips in a single Phase 3 run, all
occurring strictly after test_20's throwaway create_app() call.

This module reproduces the exact hazard (a second create_app() call)
inside one test function, then proves -- via a REAL front-door MCP call
using the shared session's own admin token, per the Server E2E
front-door-only mandate -- that the guard fixture restored the shared
session's auth globals before the next test could observe the poisoned
state. Fast and deterministic: no need to run the full 10-16 minute
e2e-automation.sh Phase 3 suite to verify this specific mechanism.

A second, distinct variant of the hazard is also covered here: test_21's
own ``telemetry_app_client`` fixture poisons the globals during its OWN
SETUP (a module/session-scoped fixture), not inside its test's body. A
naive live-snapshot guard taking its snapshot at FUNCTION-scoped setup
time would already observe the poisoned state, because pytest sets up
wider-than-function-scoped fixtures needed by a test BEFORE narrower ones
-- confirmed live: fixing only the "poison inside a test body" case left
test_21_otel_live_collector_1676.py's own subcheck with
"1 passed, 1 error" (its teardown-time admin_logs_query call still 401'd).
conftest.py's ``_golden_auth_dependencies_snapshot`` fixes this by
capturing the correct baseline ONCE, immediately after ``test_client``
exists and before any test's fixture graph (including a module-scoped
throwaway-app fixture) can run.

Execution-order note: this suite runs single-threaded, sequential, with no
randomization plugin configured (pytest header lists no pytest-randomly/
xdist) -- tests within one module execute top-to-bottom in definition
order, so each "proof" test below reliably runs immediately after its
corresponding "poisoning" test.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as auth_dependencies
from tests.e2e.server.conftest import (
    AdminTokenProvider,
    preserve_root_logging_handlers,
)
from tests.e2e.server.mcp_helpers import call_mcp_tool

_HTTP_OK = 200


@contextmanager
def _isolated_server_data_dir(data_dir: Path) -> Iterator[None]:
    """Temporarily point CIDX_SERVER_DATA_DIR at an isolated directory.

    A throwaway create_app() against the SAME data dir as the shared
    session's test_client would share DatabaseConnectionManager's
    singleton-per-path SQLite connection, so the throwaway app's
    TestClient.__exit__ shutdown would close the connection the rest of
    the E2E session still needs -- discovered by
    test_20_telemetry_metrics_wiring_1586.py, unrelated to the auth-global
    hazard this module tests, but a prerequisite to isolate here too.
    """
    previous_data_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CIDX_SERVER_DATA_DIR"] = str(data_dir)
    try:
        yield
    finally:
        if previous_data_dir is None:
            os.environ.pop("CIDX_SERVER_DATA_DIR", None)
        else:
            os.environ["CIDX_SERVER_DATA_DIR"] = previous_data_dir


def test_second_create_app_poisons_globals_within_its_own_scope(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Sanity-check the hazard itself still exists: a throwaway create_app()
    call DOES replace the shared jwt_manager global while its own
    TestClient is open. If this assertion ever fails, the module-level
    global wiring this regression test (and the conftest.py guard) protect
    against has been removed -- both can then be retired.
    """
    from code_indexer.server.app import create_app

    original_jwt_manager = auth_dependencies.jwt_manager
    assert original_jwt_manager is not None

    with _isolated_server_data_dir(tmp_path / "isolated-data-dir"):
        throwaway_app = create_app()
        with preserve_root_logging_handlers():
            with TestClient(throwaway_app, raise_server_exceptions=False):
                assert auth_dependencies.jwt_manager is not original_jwt_manager, (
                    "create_app() no longer replaces the shared jwt_manager "
                    "global -- the hazard this regression test guards against "
                    "no longer exists."
                )


def test_shared_session_auth_survives_a_prior_tests_throwaway_create_app(
    test_client: TestClient,
    admin_token_provider: AdminTokenProvider,
) -> None:
    """Runs immediately after the poisoning test above and proves the
    shared session's own auth state was restored: a real, authenticated
    MCP front-door call with the SAME admin_token_provider used throughout
    the rest of Phase 3 must still succeed."""
    headers = admin_token_provider.get_headers()
    resp = call_mcp_tool(test_client, "get_tool_categories", {}, headers)
    assert resp.status_code == _HTTP_OK, (
        f"Shared session admin auth broke after a prior test's throwaway "
        f"create_app() call -- the conftest.py "
        f"_restore_auth_dependencies_globals guard did not restore the "
        f"shared jwt_manager: HTTP {resp.status_code} -- {resp.text[:300]}"
    )


@pytest.fixture(scope="module")
def _fixture_setup_time_throwaway_client(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TestClient]:
    """Reproduce test_21_otel_live_collector_1676.py's telemetry_app_client
    pattern: a fixture WIDER than function scope that calls create_app()
    during its OWN setup (poisoning auth.dependencies for as long as this
    fixture stays alive), never inside a test body. A function-scoped
    snapshot-and-restore guard takes its snapshot AFTER this fixture's
    setup already ran (pytest sets up wider-scoped fixtures a test needs
    before narrower ones), so it would capture the ALREADY-poisoned state.
    """
    from code_indexer.server.app import create_app

    with _isolated_server_data_dir(tmp_path_factory.mktemp("fixture-setup-poison")):
        throwaway_app = create_app()
        with preserve_root_logging_handlers():
            with TestClient(throwaway_app, raise_server_exceptions=False) as client:
                yield client


def test_module_scoped_fixture_setup_poisoning_is_recorded(
    _golden_auth_dependencies_snapshot: dict,
    _fixture_setup_time_throwaway_client: TestClient,
) -> None:
    """Sanity check: the module-scoped fixture's create_app() call DOES
    replace the shared jwt_manager global for the duration of its own
    scope. Compares against ``_golden_auth_dependencies_snapshot`` (the
    correct baseline captured once, early, right after test_client exists)
    since no local "before" reference is available here -- the poisoning
    fixture already ran its setup before this test body starts."""
    golden_jwt_manager = _golden_auth_dependencies_snapshot["jwt_manager"]
    assert auth_dependencies.jwt_manager is not golden_jwt_manager, (
        "create_app() no longer replaces the shared jwt_manager global "
        "during a wider-scoped fixture's setup -- the hazard the next "
        "test guards against no longer exists."
    )


def test_shared_session_auth_survives_a_fixture_setup_time_poisoning(
    test_client: TestClient,
    admin_token_provider: AdminTokenProvider,
) -> None:
    """Runs after the fixture-setup poisoning test above WITHOUT requesting
    ``_fixture_setup_time_throwaway_client`` itself -- since no later test
    in this module needs it, pytest tears that module-scoped fixture down
    right after the prior test's own (function-scoped) teardown phase.
    Proves the shared session's admin auth survives a poisoning that
    happened at FIXTURE SETUP time, not just one from inside a test body.
    """
    headers = admin_token_provider.get_headers()
    resp = call_mcp_tool(test_client, "get_tool_categories", {}, headers)
    assert resp.status_code == _HTTP_OK, (
        f"Shared session admin auth broke after a prior test's "
        f"fixture-setup-time create_app() poisoning -- conftest.py's "
        f"_golden_auth_dependencies_snapshot did not protect against a "
        f"wider-than-function-scoped poisoning fixture: HTTP "
        f"{resp.status_code} -- {resp.text[:300]}"
    )
