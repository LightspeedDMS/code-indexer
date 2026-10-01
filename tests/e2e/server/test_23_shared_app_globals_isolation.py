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
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as auth_dependencies
from tests.e2e.server.conftest import (
    AdminTokenProvider,
    isolated_server_data_dir,
    preserve_root_logging_handlers,
    wait_for_terminal_job,
)
from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

_HTTP_OK = 200
_XRAY_JOB_TIMEOUT_S = 120.0
_JOB_POLL_S = 0.5
# Governor lane of a VoyageAI query embedding (governed_call._get_embedding_budget).
_VOYAGE_EMBED_LANE = "voyage:embed"
# Passes the server's password strength policy (as test_12's _STRONG_PASSWORD).
_ELEV_PASSWORD = "ElevIsoAb1!xyz"


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

    with isolated_server_data_dir(tmp_path / "isolated-data-dir"):
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


def _audit_rows(
    client: TestClient, headers: dict, **filters: str
) -> List[Dict[str, Any]]:
    resp = call_mcp_tool(client, "query_audit_logs", {"limit": 100, **filters}, headers)
    assert resp.status_code == _HTTP_OK, resp.text[:300]
    result = parse_mcp_result(resp.json())
    assert result.get("success") is True, result
    rows: List[Dict[str, Any]] = result["entries"]
    return rows


def _assert_shared_audit_capture_works(test_client: TestClient, headers: dict) -> None:
    """A failed login (bound audit capture) and a group create (router group
    manager) both reach the shared audit store, read back through MCP."""
    correlation_id = str(uuid.uuid4())
    failed = test_client.post(
        "/auth/login",
        json={"username": "e2e-isolation-nobody", "password": "not-the-password"},
        headers={"X-Correlation-ID": correlation_id},
    )
    assert failed.status_code == 401, failed.text[:300]
    failure_rows = [
        row
        for row in _audit_rows(
            test_client,
            headers,
            action_type="authentication_failure",
            user="(unknown)",
        )
        if row["correlation_id"] == correlation_id
    ]
    assert len(failure_rows) == 1, (
        "failed login after a prior test's throwaway create_app() wrote no "
        f"audit row -- the process-wide audit sink was not restored: {failure_rows}"
    )

    created = test_client.post(
        "/api/v1/groups",
        json={"name": f"e2e-isolation-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert created.status_code == 201, created.text[:300]
    group_id = str(created.json()["id"])
    try:
        group_rows = [
            row
            for row in _audit_rows(test_client, headers, action_type="group_create")
            if row["target_id"] == group_id
        ]
        assert len(group_rows) == 1, (
            "group create after a prior test's throwaway create_app() wrote no "
            f"audit row -- the groups router manager was not restored: {group_rows}"
        )
    finally:
        test_client.delete(f"/api/v1/groups/{group_id}", headers=headers)


def _shared_process_values(test_client: TestClient) -> Dict[str, Any]:
    """The shared app's X-Ray and query-path process values, read directly."""
    from code_indexer.server.services.coalescer_registry import (
        get_coalescer_registry,
    )
    from code_indexer.server.services.governed_call import (
        get_query_embedding_cache,
    )

    state = getattr(test_client.app, "state")
    shared = {
        "xray_executor": getattr(state, "xray_executor", None),
        "xray_cell_limiter": getattr(state, "xray_cell_limiter", None),
        "coalescer_registry": get_coalescer_registry(),
        "query_embedding_cache": get_query_embedding_cache(),
    }
    unset = [name for name, value in shared.items() if value is None]
    assert not unset, f"shared app has no {unset} before the poison"
    return shared


def _run_throwaway_app_lifespan(data_dir: Path) -> None:
    """Build, start and fully shut down a throwaway create_app() lifespan.

    Its create_app() and startup rebind the process-wide wiring; its
    shutdown stops and unbinds what it started.  The ConfigService singleton
    it created for the isolated data dir is then reset, exactly as
    tests/conftest.py resets it at every test boundary.
    """
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    with isolated_server_data_dir(data_dir):
        throwaway_app = create_app()
        with preserve_root_logging_handlers():
            with TestClient(throwaway_app, raise_server_exceptions=False):
                pass
    reset_config_service()


def test_shared_startup_snapshot_rejects_a_wrong_binding(
    test_client: TestClient,
    test_client_data_dir: Path,
    _golden_app_wiring_snapshot: dict,
) -> None:
    """The conftest snapshot validates what the shared startup bound and
    fails loudly on a wrong binding, instead of recording it for restore."""
    from concurrent.futures import ThreadPoolExecutor

    from tests.e2e.server.conftest import (
        _assert_auth_stores_in_shared_data_dir,
        _assert_bindings_are_shared_apps_own,
        _assert_shared_workers_live,
        _assert_solo_mode_wiring,
    )

    shared_app = getattr(test_client, "app")
    recorded = dict(_golden_app_wiring_snapshot["values"])
    _assert_bindings_are_shared_apps_own(recorded, shared_app)
    _assert_auth_stores_in_shared_data_dir(recorded, shared_app, test_client_data_dir)
    _assert_solo_mode_wiring(recorded)
    _assert_shared_workers_live(recorded)

    foreign_db = {
        **recorded,
        "token_blacklist_sqlite_path": str(Path("elsewhere") / "cidx_server.db"),
    }
    with pytest.raises(AssertionError, match="token_blacklist_sqlite_path"):
        _assert_auth_stores_in_shared_data_dir(
            foreign_db, shared_app, test_client_data_dir
        )

    foreign_sink = {**recorded, "audit_capture_sink": (object(), None)}
    with pytest.raises(AssertionError, match="audit_capture_sink"):
        _assert_bindings_are_shared_apps_own(foreign_sink, shared_app)
    unset_registry = {**recorded, "coalescer_registry": None}
    with pytest.raises(AssertionError, match="coalescer_registry"):
        _assert_solo_mode_wiring(unset_registry)
    stopped = ThreadPoolExecutor(max_workers=1)
    stopped.shutdown(wait=True)
    with pytest.raises(AssertionError, match="X-Ray executor"):
        _assert_shared_workers_live({**recorded, "xray_executor": stopped})


def test_shared_session_audit_capture_survives_a_throwaway_app_lifespan(
    test_client: TestClient,
    admin_token_provider: AdminTokenProvider,
    restore_shared_app_wiring: Callable[[], None],
    tmp_path: Path,
) -> None:
    """A throwaway app's lifespan shutdown unbinds the process-wide audit
    sink and stops its own AuditLogService -- the one its GroupAccessManager
    (installed as the groups router's module-level manager) writes through.
    After the guards' restore, audit capture reaches the shared store."""
    _run_throwaway_app_lifespan(tmp_path / "throwaway")
    restore_shared_app_wiring()
    _assert_shared_audit_capture_works(test_client, admin_token_provider.get_headers())


def test_shared_session_xray_executor_survives_a_throwaway_app_lifespan(
    seeded_indexed_client: "tuple[TestClient, str]",
    admin_token_provider: AdminTokenProvider,
    restore_shared_app_wiring: Callable[[], None],
    tmp_path: Path,
) -> None:
    """The lifespan mirrors its X-Ray executor and cell limiter onto the
    process-wide app singleton (the shared app) and shuts that executor down
    at exit.  A multi-repo xray_explore submits every per-repo job to that
    executor: it must be the shared app's live one and the jobs complete."""
    client, alias = seeded_indexed_client
    shared = _shared_process_values(client)
    _run_throwaway_app_lifespan(tmp_path / "throwaway")
    restore_shared_app_wiring()
    state = getattr(client.app, "state")
    assert state.xray_executor is shared["xray_executor"], (
        "the X-Ray executor on the shared app is not its own after a "
        "throwaway app lifespan (the throwaway's shut-down one?)"
    )
    assert state.xray_cell_limiter is shared["xray_cell_limiter"]
    resp = call_mcp_tool(
        client,
        "xray_explore",
        {
            "repository_alias": [alias, alias],  # a list: the multi-repo path
            "pattern": r"def\s+escape",
            "search_target": "content",
            "max_debug_nodes": 5,
        },
        admin_token_provider.get_headers(),
    )
    assert resp.status_code == _HTTP_OK, resp.text[:300]
    result = parse_mcp_result(resp.json())
    assert result.get("errors") == [] and len(result.get("job_ids") or []) == 2, (
        "multi-repo xray_explore could not submit to the X-Ray executor after "
        f"a throwaway app lifespan: {result}"
    )
    for job_id in result["job_ids"]:
        wait_for_terminal_job(
            client,
            job_id,
            admin_token_provider,
            timeout=_XRAY_JOB_TIMEOUT_S,
            poll_interval=_JOB_POLL_S,
            label="xray_explore",
        )


def test_shared_session_query_coalescer_survives_a_throwaway_app_lifespan(
    seeded_indexed_client: "tuple[TestClient, str]",
    admin_token_provider: AdminTokenProvider,
    restore_shared_app_wiring: Callable[[], None],
    tmp_path: Path,
) -> None:
    """The lifespan installs the process-wide embedding coalescer registry
    and query-embedding cache and clears both at exit -- after which every
    query silently takes the direct, uncoalesced path.  A real semantic
    query (unique text: a cache miss) must run through the shared app's own
    registry, which then holds a coalescer on the query's provider lane."""
    from code_indexer.server.services.coalescer_registry import (
        get_coalescer_registry,
    )
    from code_indexer.server.services.governed_call import (
        get_query_embedding_cache,
    )

    client, alias = seeded_indexed_client
    shared = _shared_process_values(client)
    _run_throwaway_app_lifespan(tmp_path / "throwaway")
    restore_shared_app_wiring()
    assert get_coalescer_registry() is shared["coalescer_registry"], (
        "the process-wide coalescer registry is not the shared app's after a "
        "throwaway app lifespan -- queries take the uncoalesced direct path"
    )
    assert get_query_embedding_cache() is shared["query_embedding_cache"]
    resp = client.post(
        "/api/query",
        json={
            "query_text": f"escape html markup {uuid.uuid4().hex}",
            "repository_alias": alias,
            "limit": 3,
        },
        headers=admin_token_provider.get_headers(),
    )
    assert resp.status_code == _HTTP_OK, resp.text[:300]
    assert shared["coalescer_registry"].get(_VOYAGE_EMBED_LANE), (
        "the semantic query did not go through the shared coalescer registry"
    )


def _jti_of(token: str) -> str:
    import jwt as pyjwt

    jti = pyjwt.decode(token, options={"verify_signature": False}).get("jti")
    assert jti, "access token carries no jti"
    return str(jti)


def _admin_bearer_token(client: TestClient) -> str:
    resp = client.post(
        "/auth/login",
        json={
            "username": os.environ["E2E_ADMIN_USER"],
            "password": os.environ["E2E_ADMIN_PASS"],
        },
    )
    assert resp.status_code == _HTTP_OK, resp.text[:300]
    return str(resp.json()["access_token"])


def _shared_db_path(test_client: TestClient) -> str:
    """The shared app's own cidx_server.db (app_wiring sets db_path_str)."""
    return str(getattr(test_client.app, "state").db_path_str)


def test_shared_session_logout_revocation_survives_a_throwaway_app_lifespan(
    test_client: TestClient,
    restore_shared_app_wiring: Callable[[], None],
    tmp_path: Path,
) -> None:
    """create_app() repoints the process-wide TokenBlacklist at its own
    cidx_server.db.  A logout must revoke the token in the SHARED app's
    store: rejected on this node (its in-memory set) AND visible to another
    worker reading the shared store (a fresh blacklist on the shared db)."""
    from code_indexer.server.app import TokenBlacklist

    _run_throwaway_app_lifespan(tmp_path / "throwaway")
    restore_shared_app_wiring()
    token = _admin_bearer_token(test_client)
    headers = {"Authorization": f"Bearer {token}"}
    assert test_client.get("/api/keys", headers=headers).status_code == _HTTP_OK
    logout = test_client.get("/admin/logout", headers=headers, follow_redirects=False)
    assert logout.status_code in (302, 303, 307), logout.text[:300]
    rejected = test_client.get("/api/keys", headers=headers)
    assert rejected.status_code == 401, rejected.text[:300]
    other_worker_view = TokenBlacklist()
    other_worker_view.set_sqlite_path(_shared_db_path(test_client))
    assert other_worker_view.contains(_jti_of(token)), (
        "logout revoked the token outside the shared app's store -- another "
        "worker would still accept it"
    )


def test_shared_session_elevation_window_survives_a_throwaway_app_lifespan(
    test_client: TestClient,
    restore_shared_app_wiring: Callable[[], None],
    tmp_path: Path,
) -> None:
    """create_app() repoints the process-wide ElevatedSessionManager at its
    own cidx_server.db.  The Phase 3 elevation flow (enrol TOTP, POST
    /auth/elevate, gated POST /api/admin/users) must succeed AND open the
    window in the SHARED app's store, where another worker would look."""
    import pyotp

    from code_indexer.server.auth.elevated_session_manager import (
        ElevatedSessionManager,
    )
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.web.mfa_routes import get_totp_service

    _run_throwaway_app_lifespan(tmp_path / "throwaway")
    restore_shared_app_wiring()
    username = os.environ["E2E_ADMIN_USER"]
    token = _admin_bearer_token(test_client)  # before enrolment: no MFA step
    headers = {"Authorization": f"Bearer {token}"}
    totp_service = get_totp_service()
    assert totp_service is not None
    config = get_config_service().get_config()
    prior_enforcement = bool(config.elevation_enforcement_enabled)
    config.elevation_enforcement_enabled = True
    new_user = f"e2e-elev-iso-{uuid.uuid4().hex[:8]}"
    try:
        secret = totp_service.generate_secret(username)
        assert totp_service.activate_mfa(username, pyotp.TOTP(secret).now())
        elevate = test_client.post(
            "/auth/elevate",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers=headers,
        )
        assert elevate.status_code == _HTTP_OK, elevate.text[:300]
        assert elevate.json().get("elevated") is True, elevate.json()
        created = test_client.post(
            "/api/admin/users",
            json={
                "username": new_user,
                "password": _ELEV_PASSWORD,
                "role": "normal_user",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text[:300]
        other_worker_view = ElevatedSessionManager(db_path=_shared_db_path(test_client))
        assert other_worker_view.get_status(_jti_of(token)) is not None, (
            "the elevation window was opened outside the shared app's store -- "
            "another worker would demand elevation again"
        )
    finally:
        test_client.delete(f"/api/admin/users/{new_user}", headers=headers)
        totp_service.disable_mfa(username, actor=username)
        config.elevation_enforcement_enabled = prior_enforcement


def test_shared_session_mcp_managers_survive_a_throwaway_app_lifespan(
    test_client: TestClient,
    admin_token_provider: AdminTokenProvider,
    restore_shared_app_wiring: Callable[[], None],
    tmp_path: Path,
) -> None:
    """create_app() also rebinds server.app's module-level managers, which
    MCP handlers read (``app_module.user_manager``) while REST routes keep
    the managers bound at app build.  A key minted through MCP must be the
    one REST lists, i.e. both doors share the session's user store."""
    _run_throwaway_app_lifespan(tmp_path / "throwaway")
    restore_shared_app_wiring()
    headers = admin_token_provider.get_headers()
    created = parse_mcp_result(
        call_mcp_tool(
            test_client, "create_api_key", {"description": "e2e isolation"}, headers
        ).json()
    )
    assert created.get("success") is True, created
    key_id = created["key_id"]
    try:
        listed = test_client.get("/api/keys", headers=headers)
        assert listed.status_code == _HTTP_OK, listed.text[:300]
        listed_ids = {key["key_id"] for key in listed.json()["keys"]}
        assert key_id in listed_ids, (
            "MCP create_api_key wrote to a different user store than REST "
            "reads after a prior test's throwaway create_app() -- server.app "
            "module-level managers were not restored"
        )
    finally:
        call_mcp_tool(test_client, "delete_api_key", {"key_id": key_id}, headers)


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

    with isolated_server_data_dir(tmp_path_factory.mktemp("fixture-setup-poison")):
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
    # The module-scoped throwaway app is still alive here (torn down at
    # module end); its startup rebound the process-wide audit wiring, which
    # conftest.py's _restore_app_wiring_globals must have undone.
    _assert_shared_audit_capture_works(test_client, headers)
