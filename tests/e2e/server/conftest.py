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

import json
import logging
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import pytest
from _pytest.monkeypatch import MonkeyPatch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.services.auto_watch_manager import auto_watch_manager
from tests.e2e.helpers import _auth_headers, require_voyage_key
from tests.e2e.server.shared_app_bindings import (
    _app_wiring_bindings,
    _assert_auth_stores_in_shared_data_dir,
    _assert_bindings_are_shared_apps_own,
    _assert_shared_workers_live,
    _assert_solo_mode_wiring,
    _assert_write_mode_dirs_are_shared_apps,
    _restore_app_wiring,
)

logger = logging.getLogger(__name__)

# Environment variable names that carry admin credentials.
# e2e-automation.sh sets these for all four phases before invoking pytest.
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"


@contextmanager
def isolated_server_data_dir(
    data_dir: Path, config: Optional[Dict[str, Any]] = None
) -> Iterator[None]:
    """Point CIDX_SERVER_DATA_DIR at *data_dir* for a throwaway create_app().

    Isolation: an app on the SAME data dir as the shared session's
    test_client would share DatabaseConnectionManager's singleton-per-path
    SQLite connection, and its shutdown would close the one the session
    still needs.  Bootstrap: the throwaway lifespan reads THIS dir's
    config.json (lifespan.py:658), which only a ConfigService built for this
    dir would otherwise write -- the process-wide singleton may already
    exist for another dir, and then startup logs APP-GENERAL-008.  So
    config.json is written first: *config*, or a minimal one.  The env var
    is unguarded by a lock: this suite runs single-threaded and sequential.

    The ConfigService singleton is bound to *data_dir* explicitly: the
    shared session app keeps serving during the test, and any of its
    requests or threads calling get_config_service() after the per-test
    reset would otherwise bind it to the SESSION dir, so the throwaway app
    would silently read the session's configuration.  The previously bound
    singleton is restored on exit.
    """
    from code_indexer.server.services import config_service as config_module

    previous_data_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
    previous_service = config_module._config_service
    data_dir.mkdir(parents=True, exist_ok=True)
    bootstrap = config if config is not None else {"server_dir": str(data_dir)}
    (data_dir / "config.json").write_text(json.dumps(bootstrap))
    os.environ["CIDX_SERVER_DATA_DIR"] = str(data_dir)
    try:
        config_module.set_config_service(config_module.ConfigService(str(data_dir)))
        yield
    finally:
        if previous_service is None:
            config_module.reset_config_service()
        else:
            config_module.set_config_service(previous_service)
        if previous_data_dir is None:
            os.environ.pop("CIDX_SERVER_DATA_DIR", None)
        else:
            os.environ["CIDX_SERVER_DATA_DIR"] = previous_data_dir


def stop_in_process_stall_watchdog(app: FastAPI) -> None:
    """Stop the worker-stall watchdog (Story S12) an in-process app started.

    The watchdog is one per uvicorn WORKER process: it re-arms the
    process-global faulthandler timer every second and reports when no
    Python thread of that worker ran for 3 s. This phase runs the shared
    app, any app a test builds, the TestClient and the test code in ONE
    interpreter, so here it would report the harness's own GIL use or the
    machine starving the pytest process, and several in-process apps would
    re-arm and cancel the same process-global timer. Call it right after a
    long-lived in-process app's lifespan has started. Real workers stay
    covered: Phase 4 runs a live uvicorn server with the watchdog on.
    """
    watchdog = getattr(app.state, "stall_watchdog", None)
    if watchdog is None:
        return
    app.state.stall_watchdog = None
    watchdog.stop()


@contextmanager
def preserve_root_logging_handlers() -> Iterator[None]:
    """Restore shared logging after a throwaway in-process app shuts down."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
        for handler in original_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)


# ---------------------------------------------------------------------------
# Guard: a second create_app() must not corrupt shared auth globals
# ---------------------------------------------------------------------------

# create_app() -> app_wiring.create_fastapi_app() wires these services as
# MODULE-LEVEL globals on auth.dependencies -- correct for production
# (exactly one create_app() per process) but a hazard here: a test that
# legitimately builds a SECOND, throwaway create_app() (e.g.
# test_20_telemetry_metrics_wiring_1586.py, test_21_otel_live_collector_1676.py)
# permanently overwrites them, splitting JWT minting (still the shared
# session app's own jwt_manager) from JWT validation (now the throwaway
# app's jwt_manager/secret_key) for the rest of the process -- 401s for
# every later test, unfixable by re-login/refresh. See
# test_23_shared_app_globals_isolation.py for the full root-cause narrative
# and regression proof. This suite runs single-threaded/sequential (no
# xdist/parallel plugin), so snapshot/restore needs no lock.
_GUARDED_AUTH_DEPENDENCY_ATTRS = (
    "jwt_manager",
    "user_manager",
    "oauth_manager",
    "mcp_credential_manager",
    "server_config",
    "api_key_manager",
)


@pytest.fixture(scope="session", autouse=True)
def _golden_auth_dependencies_snapshot(test_client: TestClient) -> dict:
    """Capture the CORRECT auth.dependencies values exactly once, right
    after the shared session app exists -- before any test body or any
    wider-than-function-scoped fixture (e.g. test_21's module-scoped
    ``telemetry_app_client``, which poisons these globals during its OWN
    setup) gets a chance to run. Depending on ``test_client`` guarantees
    this fixture's setup happens after the shared app's create_app() call.
    A per-test live snapshot is NOT enough: pytest sets up wider-scoped
    fixtures needed by a test BEFORE narrower ones, so a plain
    function-scoped snapshot taken for test_21's single test would already
    observe the poisoned state.
    """
    from code_indexer.server import app as _app_module
    from code_indexer.server.auth import dependencies as _auth_dependencies

    golden = {
        attr: getattr(_auth_dependencies, attr, None)
        for attr in _GUARDED_AUTH_DEPENDENCY_ATTRS
    }
    # Record what the shared create_app() wired; never repair it.
    unset = [attr for attr, value in golden.items() if value is None]
    assert not unset, f"shared create_app() left auth.dependencies unset: {unset}"
    for attr in ("jwt_manager", "user_manager"):
        assert golden[attr] is vars(_app_module).get(attr), (
            f"auth.dependencies.{attr} is not the shared create_app()'s own"
        )
    return golden


def _restore_auth_dependencies(golden: dict) -> None:
    from code_indexer.server.auth import dependencies as _auth_dependencies

    for attr, value in golden.items():
        setattr(_auth_dependencies, attr, value)


@pytest.fixture(autouse=True)
def _restore_auth_dependencies_globals(
    _golden_auth_dependencies_snapshot: dict,
) -> Iterator[None]:
    """Restore auth.dependencies to the golden baseline after each test.

    Function-scoped so it fires after EVERY test (undoing a poisoning
    create_app() called directly inside a test body, e.g. test_20), and it
    restores to the FIXED baseline above (never a live re-snapshot) so it
    also fixes a poisoning that happened during a wider-scoped fixture's
    setup (e.g. test_21), which a live snapshot taken at this fixture's own
    setup time would have missed.  After-only by design: a wider-scoped
    throwaway app (test_21's own client) keeps its auth wiring for its test.
    """
    yield
    _restore_auth_dependencies(_golden_auth_dependencies_snapshot)


# ---------------------------------------------------------------------------
# Guard: a second app must not strip the shared app's process-wide wiring
# (bindings and validators: tests/e2e/server/shared_app_bindings.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _golden_app_wiring_snapshot(
    test_client: TestClient, test_client_data_dir: Path
) -> dict:
    """The shared session app's process-wide wiring, recorded once.

    Validated against the shared app itself BEFORE any restore can run: the
    snapshot records what the shared startup did and never repairs it.
    """
    shared_app = test_client.app
    assert isinstance(shared_app, FastAPI), type(shared_app)
    bindings = _app_wiring_bindings(shared_app)
    values = {name: read() for name, (read, _write) in bindings.items()}
    _assert_bindings_are_shared_apps_own(values, shared_app)
    _assert_auth_stores_in_shared_data_dir(values, shared_app, test_client_data_dir)
    _assert_write_mode_dirs_are_shared_apps(values, shared_app)
    _assert_solo_mode_wiring(values)
    _assert_shared_workers_live(values)
    return {"bindings": bindings, "values": values}


@pytest.fixture(autouse=True)
def _restore_app_wiring_globals(
    _golden_app_wiring_snapshot: dict,
) -> Iterator[None]:
    """Restore the shared app's process-wide wiring before AND after each test.

    After: undoes a throwaway app built inside a test body (test_20,
    test_23, test_24) and a wider-scoped throwaway-app fixture's startup.
    Before: such a fixture is torn down at the end of its module, AFTER its
    last test's function-scoped teardown, so its shutdown unbind would
    otherwise reach the next module's first test.
    """
    _restore_app_wiring(_golden_app_wiring_snapshot)
    yield
    _restore_app_wiring(_golden_app_wiring_snapshot)


@pytest.fixture(scope="module", autouse=True)
def _restore_app_wiring_for_module(_golden_app_wiring_snapshot: dict) -> None:
    """Restore at each module's setup, ahead of its own module fixtures.

    The previous module's module-scoped throwaway app is torn down at that
    module's end, after every function-scoped restore; without this, the
    next module's module-scoped fixtures (e.g. test_26's web_client login)
    would run against the unbound audit sink its shutdown left behind.
    Autouse fixtures are set up before other fixtures of the same scope.
    """
    _restore_app_wiring(_golden_app_wiring_snapshot)


@pytest.fixture
def restore_shared_app_wiring(
    _golden_auth_dependencies_snapshot: dict,
    _golden_app_wiring_snapshot: dict,
) -> Callable[[], None]:
    """The exact restore the autouse guards run, as a callable: for a test
    that builds and shuts down a throwaway app INSIDE its own body and must
    observe the healed state before those guards next fire."""

    def _restore() -> None:
        _restore_auth_dependencies(_golden_auth_dependencies_snapshot)
        _restore_app_wiring(_golden_app_wiring_snapshot)

    return _restore


# ---------------------------------------------------------------------------
# AdminTokenProvider — automatic JWT refresh on near-expiry
# ---------------------------------------------------------------------------


class AdminTokenProvider:
    """Cache a JWT access token and re-login when it nears expiry.

    Uses the JWT ``exp`` claim (Unix epoch seconds) to decide whether to
    refresh.  No signature verification is performed — we only need the
    timestamp embedded in the token.

    Args:
        login_fn:              Callable that returns ``(access_token, refresh_token)``.
                               Called during construction and as the FALLBACK renewal
                               path whenever the cached token is within
                               ``REFRESH_THRESHOLD_SECONDS`` of expiry and the
                               refresh-token grant is unavailable or fails.
        initial_access_token:  First access token, obtained by the caller before
                               constructing the provider.
        initial_refresh_token: Corresponding refresh token (may be ``None``).
        refresh_fn:            Optional callable that renews the token via the
                               refresh-token grant (e.g. POST /api/auth/refresh)
                               instead of a full username/password re-login.
                               Called with the cached refresh token; must return
                               ``(access_token, refresh_token)`` on success or
                               ``None`` on failure (an exception is also treated
                               as failure). Preferred over ``login_fn`` on
                               near-expiry renewal -- renewing via a refresh
                               token never resubmits credentials, so it cannot
                               trip a credential-based login lockout (Bug #1484).
                               ``login_fn`` is used when ``refresh_fn`` is
                               ``None``, no refresh token is cached, or the
                               grant itself fails.
    """

    REFRESH_THRESHOLD_SECONDS: int = 60

    def __init__(
        self,
        login_fn: Callable[[], Tuple[str, Optional[str]]],
        initial_access_token: str,
        initial_refresh_token: Optional[str],
        refresh_fn: Optional[
            Callable[[str], Optional[Tuple[str, Optional[str]]]]
        ] = None,
    ) -> None:
        self._login_fn = login_fn
        self._access_token = initial_access_token
        self._refresh_token = initial_refresh_token
        self._refresh_fn = refresh_fn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _exp_from_token(token: str) -> float:
        """Decode the ``exp`` claim without verifying the JWT signature.

        Returns the expiry as a Unix epoch float (seconds).

        Raises:
            ValueError: If the token has no ``exp`` claim.
        """
        from jose import jwt as jose_jwt

        claims = jose_jwt.get_unverified_claims(token)
        exp = claims.get("exp")
        if exp is None:
            raise ValueError(f"AdminTokenProvider: JWT has no 'exp' claim: {claims!r}")
        return float(exp)

    def _is_near_expiry(self, token: str) -> bool:
        """Return True when ``now + REFRESH_THRESHOLD_SECONDS >= exp``."""
        exp = self._exp_from_token(token)
        return time.time() + self.REFRESH_THRESHOLD_SECONDS >= exp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """Return a valid access token, renewing on near-expiry.

        Renewal prefers the refresh-token grant (``refresh_fn``) over a full
        ``login_fn`` re-login: a routine renewal must never resubmit
        credentials, since a credential-based login lockout accumulated
        earlier in a long test phase would otherwise 401 the renewal itself
        (Bug #1484). ``login_fn`` is used when ``refresh_fn`` is absent, no
        refresh token is cached, or the grant fails (returns ``None`` or
        raises).

        Thread-safe note: concurrent calls may both refresh; the last write wins.
        This is safe for E2E test usage where a single test drives requests.
        """
        if self._is_near_expiry(self._access_token):
            renewed = None
            if self._refresh_fn is not None and self._refresh_token:
                try:
                    renewed = self._refresh_fn(self._refresh_token)
                except Exception as exc:  # noqa: BLE001 -- deliberate bounded fallback
                    logger.warning(
                        "AdminTokenProvider: refresh-token grant raised %r; "
                        "falling back to full re-login",
                        exc,
                    )
                    renewed = None

            if renewed is not None:
                new_access, new_refresh = renewed
            else:
                new_access, new_refresh = self._login_fn()

            self._access_token = new_access
            self._refresh_token = new_refresh
        return self._access_token

    def get_headers(self) -> dict:
        """Return ``{"Authorization": "Bearer <token>"}`` via the shared helper."""
        return _auth_headers(self.get_token())


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
    # Phase 3 is a REST/MCP functional suite; watch-mode (Phase 2's concern) is
    # disabled to prevent watch-daemon accumulation across the session-scoped server
    # — the root-cause trigger of the ms1139scip golden-repo registration failure.
    auto_watch_manager.auto_watch_enabled = False
    yield d
    if previous is None:
        os.environ.pop("CIDX_SERVER_DATA_DIR", None)
    else:
        os.environ["CIDX_SERVER_DATA_DIR"] = previous


@pytest.fixture(scope="session", autouse=True)
def _disable_registration_lifecycle_hook(
    test_client_data_dir: Path,
) -> Iterator[None]:
    """Disable the inline Claude-CLI lifecycle call during golden-repo registration.

    GoldenRepoManager._register_lifecycle_after_registration runs a blocking
    real ``claude -p`` subprocess on the background-worker thread immediately
    after clone+init+index.  In Phase 3 each register job takes 3-4 minutes
    instead of ~1 minute, saturating the 5-worker pool and causing the 300s
    poll deadline to be exceeded when several repos are registered back-to-back.

    This fixture replaces the method with a fast no-op for the entire e2e
    session.  The lifecycle/description feature is covered by its own dedicated
    tests and does not need to be exercised here.

    Depends on ``test_client_data_dir`` (the root session fixture) so the patch
    is installed BEFORE ``test_client`` creates the app and BEFORE any
    golden-repo registration fires.
    """
    mp = MonkeyPatch()

    def _noop_register_lifecycle(
        self: object, alias: str, submitter_username: str
    ) -> None:
        logger.info(
            "[e2e] registration lifecycle hook disabled for test session"
            " (alias=%r, submitter=%r)",
            alias,
            submitter_username,
        )

    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    mp.setattr(
        GoldenRepoManager,
        "_register_lifecycle_after_registration",
        _noop_register_lifecycle,
    )
    yield
    mp.undo()


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
        stop_in_process_stall_watchdog(fresh_app)
        yield client


@pytest.fixture(scope="session")
def admin_token_provider(test_client: TestClient) -> AdminTokenProvider:
    """Session-scoped AdminTokenProvider backed by the in-process TestClient.

    Performs the initial /auth/login once and caches the result.  All
    subsequent callers (admin_token, auth_headers, log_audit_admin_token)
    delegate here so the token is refreshed automatically if the phase runs
    longer than the JWT TTL (~10 minutes).
    """
    username = _require_env(_ENV_ADMIN_USER)
    password = _require_env(_ENV_ADMIN_PASS)

    def _relogin() -> tuple[str, str | None]:
        resp = test_client.post(
            "/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, (
            f"admin_token_provider re-login failed: {resp.status_code} — {resp.text[:300]}"
        )
        body = resp.json()
        return str(body["access_token"]), body.get("refresh_token")

    def _refresh_via_grant(refresh_token: str) -> tuple[str, str | None] | None:
        """Renew via POST /api/auth/refresh instead of resubmitting credentials.

        Bug #1484: a full username/password re-login late in a long,
        auth-heavy phase can 401 due to account state/rate-limiting
        accumulated across the phase (the login endpoint's own lockout,
        independent of the refresh-token grant). Renewing via the refresh
        token never resubmits credentials, so it cannot trip that lockout.
        Returns None on any failure so the caller falls back to _relogin.
        """
        resp = test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        if resp.status_code != 200:
            logger.warning(
                "admin_token_provider refresh-token grant failed (%s), "
                "falling back to full re-login: %s",
                resp.status_code,
                resp.text[:300],
            )
            return None
        body = resp.json()
        return str(body["access_token"]), body.get("refresh_token")

    initial_access, initial_refresh = _relogin()
    return AdminTokenProvider(
        login_fn=_relogin,
        initial_access_token=initial_access,
        initial_refresh_token=initial_refresh,
        refresh_fn=_refresh_via_grant,
    )


@pytest.fixture(scope="function")
def admin_token(admin_token_provider: AdminTokenProvider) -> str:
    """Return a fresh-enough admin JWT for the current test.

    Function-scoped so each test gets a token that is not near-expiry,
    even in long-running phases.  Delegates to the session-scoped provider
    so no extra login round-trips occur unless the token nears its TTL.
    """
    return admin_token_provider.get_token()


@pytest.fixture(scope="function")
def auth_headers(admin_token_provider: AdminTokenProvider) -> dict:
    """Return authorization headers for the current test.

    Function-scoped: every test receives a fresh-enough token.  Delegates
    to the shared _auth_headers helper via AdminTokenProvider.get_headers().
    """
    return admin_token_provider.get_headers()


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


@pytest.fixture(scope="function")
def log_audit_admin_token(admin_token_provider: AdminTokenProvider) -> str:
    """JWT string for the log-audit gate fixtures (test_log_audit_gate_e2e.py).

    Function-scoped (like ``admin_token``/``auth_headers``) so every test
    gets a not-near-expiry token via ``admin_token_provider.get_token()``.
    A prior session-scoped version cached one token for the WHOLE session,
    resolved once on first use -- stale by the time later tests in
    test_log_audit_gate_e2e.py ran in a long Phase 3 session, since nothing
    ever re-checked it against the near-expiry threshold again.
    """
    return admin_token_provider.get_token()


@pytest.fixture(scope="session")
def log_watermark(
    log_audit_app_client: TestClient,
    admin_token_provider: AdminTokenProvider,
) -> int:
    """Record the maximum log id BEFORE the phase's tests run (watermark).

    Any log entry at or below this id was emitted during server startup,
    not during the phase under test.  The gate diffs against this watermark
    so pre-existing startup messages don't fail the phase.

    Uses admin_token_provider.get_token() at call time to ensure the token
    used for the watermark query is not stale.
    """
    from tests.e2e.log_audit_gate import flush_log_pipeline, get_log_watermark

    # Flush the full logging pipeline (async_logging queue + SQLiteLogHandler's
    # own writer queue) to drain any startup entries before recording the
    # watermark. See flush_log_pipeline()'s docstring for the two-queue race
    # this closes.
    flush_log_pipeline(log_audit_app_client)

    return get_log_watermark(log_audit_app_client, admin_token_provider.get_token())


@pytest.fixture(scope="session", autouse=True)
def _phase3_log_audit_gate(
    log_audit_app_client: TestClient,
    admin_token_provider: AdminTokenProvider,
    log_watermark: int,
    pytestconfig: pytest.Config,
) -> Iterator[None]:
    """Autouse session fixture: run the log-audit gate at Phase 3 teardown.

    The result is also recorded for ``pytest_sessionfinish`` (end of this
    file), which fails the session on a failed audit whatever the last
    test's outcome.

    Yields first (tests run), then at teardown:
      1. Flush the full logging pipeline (async_logging queue listener +
         SQLiteLogHandler's own writer queue) via flush_log_pipeline().
      2. Query admin_logs_query via MCP front door.
      3. Diff against log_watermark to find new entries.
      4. Fail with detailed report if any new non-allowlisted ERROR/WARNING found.

    Calls admin_token_provider.get_token() at teardown time so the audit
    query uses a fresh token even if the phase ran longer than the JWT TTL.
    """
    from tests.e2e.log_audit_gate import flush_log_pipeline, run_log_audit_gate

    yield  # Tests run here

    # --- Teardown: audit phase logs ---
    # Flush the full logging pipeline (async_logging queue + SQLiteLogHandler's
    # own writer queue) to drain buffered entries before auditing. See
    # flush_log_pipeline()'s docstring for the two-queue race this closes.
    flush_log_pipeline(log_audit_app_client)

    # Obtain a fresh-enough token at teardown time (not the session-start token).
    teardown_token = admin_token_provider.get_token()

    result = run_log_audit_gate(
        log_audit_app_client,
        teardown_token,
        watermark_id=log_watermark,
        phase_name="Phase 3 (Server In-Process)",
    )
    from tests.e2e.log_audit_claims import record_gate_result

    record_gate_result(pytestconfig, result)
    if not result.passed:
        # pytest.fail() at session teardown surfaces as a test collection error;
        # raise AssertionError directly so it appears as a clear fixture failure.
        raise AssertionError(result.failure_message())


# ---------------------------------------------------------------------------
# seeded_indexed_client fixture (Story #1138)
# ---------------------------------------------------------------------------

# Alias used for the markupsafe golden repo in this phase.
_MARKUPSAFE_ALIAS: str = "markupsafe"

# Prebuilt SCIP fixture bundled with the test suite.  Contains a Calculator
# class in src/calculator.py.  Seeded into the golden-repo SCIP path so
# scip_definition / scip_references return real data without rustc.
_SCIP_FIXTURE_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "scip"
    / "fixtures"
    / "comprehensive_index.scip.db"
)

# Maximum seconds to wait for register + activate background jobs.
_SEED_JOB_TIMEOUT: float = float(os.environ.get("E2E_GOLDEN_JOB_TIMEOUT", "300"))
_SEED_JOB_POLL_INTERVAL: float = float(os.environ.get("E2E_GOLDEN_JOB_POLL", "0.5"))
_SEED_JOB_TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


def wait_for_terminal_job(
    client: TestClient,
    job_id: str,
    admin_token_provider: "AdminTokenProvider",
    *,
    timeout: float,
    poll_interval: float,
    label: str = "job",
    assert_completed: bool = True,
) -> dict:
    """Poll GET /api/jobs/{job_id} until terminal; return the body.

    Canonical job-poll loop for tests/e2e/server/ (Messi Rule #4:
    consolidates 4 drifted copies -- this file, test_22, test_18, test_21).
    Calls ``admin_token_provider.get_headers()`` fresh every iteration,
    never a frozen snapshot (Bug #1803 -- a frozen snapshot can outlive a
    long job, so every later poll 401s and completion is never observed).
    ``assert_completed=False`` returns the raw body on any terminal status
    instead of asserting "completed" (test_22's contract).

    Bounded loop (Messi Rule #14): terminates on deadline (TimeoutError)
    or terminal state.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout!r}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be positive, got {poll_interval!r}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            f"/api/jobs/{job_id}", headers=admin_token_provider.get_headers()
        )
        assert resp.status_code < 500, (
            f"{label}: job poll returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            status = body.get("status")
            if status in _SEED_JOB_TERMINAL:
                if assert_completed:
                    assert status == "completed", (
                        f"{label}: job {job_id!r} ended with status {status!r}: {body}"
                    )
                return dict(body)
        time.sleep(poll_interval)
    raise TimeoutError(
        f"{label}: job {job_id!r} did not reach a terminal state within {timeout}s"
    )


@pytest.fixture(scope="session")
def seeded_indexed_client(
    test_client: TestClient,
    test_client_data_dir: Path,
    admin_token_provider: AdminTokenProvider,
) -> Iterator[tuple[TestClient, str]]:
    """Register, index, and activate the markupsafe golden repo; yield (client, alias).

    Anti-dual-app invariant:
        Depends on the unified ``test_client`` fixture (never creates a second app).
        ``test_client`` already sets ``_app_module.app = fresh_app`` so admin_logs_query
        reads the same state. Adding a second TestClient / lifespan would share the
        process-global SQLiteLogHandler with a closed/different DB, causing HTTP 500s
        in the log-audit gate (the bug this fixture was designed to avoid).

    Description-refresh mitigation:
        ``description_refresh_enabled`` defaults to ``False`` in
        ``ServerConfig.claude_integration_config`` (config_manager.py line 508).
        No explicit disable step is needed — the scheduler starts but never dispatches
        Claude invocations in the E2E test environment, so no ~300s Claude call fires.

    SCIP seeding:
        The prebuilt ``tests/scip/fixtures/comprehensive_index.scip.db`` is copied into
        ``{data_dir}/golden-repos/{alias}/.code-indexer/scip/index.scip.db`` AFTER the
        golden-repo registration job completes (which creates the clone directory).
        The server's ScipQueryService walks ``{repo_path}/.code-indexer/scip/**/*.scip.db``
        so the seeded file is picked up without any additional wiring.

    Uses ``admin_token_provider`` (session-scoped) instead of the function-scoped
    ``auth_headers`` fixture to avoid a ScopeMismatch error (session fixture cannot
    request a function-scoped fixture).  Headers are obtained via
    ``admin_token_provider.get_headers()`` at each HTTP call point so the token is
    always fresh even for long-running registration jobs.

    Raises:
        pytest.skip.Exception: When VOYAGE_API_KEY / E2E_VOYAGE_API_KEY is absent.
        AssertionError: When registration or activation job fails or returns non-2xx.
        TimeoutError: When a background job exceeds E2E_GOLDEN_JOB_TIMEOUT seconds.
    """
    # Guard 1: require embedding key — loud skip locally, hard-fail in CI.
    require_voyage_key()

    # Guard 2: require markupsafe seed repo to exist on disk.
    seed_cache_dir = Path(
        os.environ.get(
            "E2E_SEED_CACHE_DIR", str(Path.home() / ".tmp" / "cidx-e2e-seed-repos")
        )
    )
    markupsafe_path = seed_cache_dir / "markupsafe"
    if not markupsafe_path.exists():
        pytest.skip(
            f"Markupsafe seed repo not found at {markupsafe_path!r} — "
            "run e2e-automation.sh to pre-seed repos or set E2E_SEED_CACHE_DIR."
        )

    alias = _MARKUPSAFE_ALIAS

    # Step 1: Register the golden repo via REST front door.
    # POST /api/admin/golden-repos accepts JSON {repo_url, alias}.
    # repo_url is the LOCAL path (file:// protocol not required — the server
    # accepts absolute paths to local directories as repo_url for golden repos).
    auth_headers = admin_token_provider.get_headers()
    reg_resp = test_client.post(
        "/api/admin/golden-repos",
        json={"repo_url": str(markupsafe_path), "alias": alias},
        headers=auth_headers,
    )
    assert reg_resp.status_code in (200, 202), (
        f"seeded_indexed_client: register returned HTTP {reg_resp.status_code}: "
        f"{reg_resp.text[:300]}"
    )
    reg_body = reg_resp.json()
    reg_job_id: str = reg_body.get("job_id", "")
    assert reg_job_id, (
        f"seeded_indexed_client: register response missing job_id: {reg_body}"
    )

    # Poll until registration+indexing job completes. Passes the provider
    # itself (Bug #1803) so headers are refreshed on EVERY poll, not just
    # once before the loop starts.
    wait_for_terminal_job(
        test_client,
        reg_job_id,
        admin_token_provider,
        timeout=_SEED_JOB_TIMEOUT,
        poll_interval=_SEED_JOB_POLL_INTERVAL,
        label="register",
    )

    # Step 2: Seed the SCIP fixture BEFORE activation so the SCIP index is present
    # when activation completes and tests run.
    # Clone path formula: {data_dir}/data/golden-repos/{alias}  (lifespan.py:133 injects "data/")
    if _SCIP_FIXTURE_PATH.exists():
        scip_dest_dir = (
            test_client_data_dir
            / "data"
            / "golden-repos"
            / alias
            / ".code-indexer"
            / "scip"
        )
        scip_dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_SCIP_FIXTURE_PATH, scip_dest_dir / "index.scip.db")

    # Step 3: Activate the golden repo.
    # POST /api/repos/activate with JSON {golden_repo_alias}.
    # When user_alias is omitted the server defaults it to golden_repo_alias.
    act_resp = test_client.post(
        "/api/repos/activate",
        json={"golden_repo_alias": alias},
        headers=admin_token_provider.get_headers(),
    )
    assert act_resp.status_code in (200, 202), (
        f"seeded_indexed_client: activate returned HTTP {act_resp.status_code}: "
        f"{act_resp.text[:300]}"
    )
    act_body = act_resp.json()
    act_job_id: str = act_body.get("job_id", "")
    assert act_job_id, (
        f"seeded_indexed_client: activate response missing job_id: {act_body}"
    )

    # Poll until activation job completes (Bug #1803: pass the provider,
    # not a frozen headers snapshot).
    wait_for_terminal_job(
        test_client,
        act_job_id,
        admin_token_provider,
        timeout=_SEED_JOB_TIMEOUT,
        poll_interval=_SEED_JOB_POLL_INTERVAL,
        label="activate",
    )

    # Yield the UNIFIED client (same app, same lifespan, no dual-app bug).
    yield test_client, alias


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A failed Phase 3 log audit makes the session exit nonzero."""
    from tests.e2e.log_audit_claims import apply_gate_exit_status

    apply_gate_exit_status(session)
