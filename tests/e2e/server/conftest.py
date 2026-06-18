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
import shutil
import time
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services.auto_watch_manager import auto_watch_manager
from tests.e2e.helpers import _auth_headers, require_voyage_key

# Environment variable names that carry admin credentials.
# e2e-automation.sh sets these for all four phases before invoking pytest.
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"


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
                               Called during construction and whenever the cached
                               token is within ``REFRESH_THRESHOLD_SECONDS`` of expiry.
        initial_access_token:  First access token, obtained by the caller before
                               constructing the provider.
        initial_refresh_token: Corresponding refresh token (may be ``None``).
    """

    REFRESH_THRESHOLD_SECONDS: int = 60

    def __init__(
        self,
        login_fn: Callable[[], Tuple[str, Optional[str]]],
        initial_access_token: str,
        initial_refresh_token: Optional[str],
    ) -> None:
        self._login_fn = login_fn
        self._access_token = initial_access_token
        self._refresh_token = initial_refresh_token

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
        """Return a valid access token, refreshing via ``login_fn`` if near-expiry.

        Thread-safe note: concurrent calls may both refresh; the last write wins.
        This is safe for E2E test usage where a single test drives requests.
        """
        if self._is_near_expiry(self._access_token):
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

    initial_access, initial_refresh = _relogin()
    return AdminTokenProvider(
        login_fn=_relogin,
        initial_access_token=initial_access,
        initial_refresh_token=initial_refresh,
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


@pytest.fixture(scope="session")
def log_audit_admin_token(admin_token_provider: AdminTokenProvider) -> str:
    """JWT string for the log-audit gate fixtures (test_log_audit_gate_e2e.py).

    Returns a plain str from the provider so callers that type-annotate as
    ``str`` work without change.  Within a single test the token is fixed;
    the critical teardown freshness is handled by ``_phase3_log_audit_gate``
    which calls ``admin_token_provider.get_token()`` at teardown time directly.
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
    from tests.e2e.log_audit_gate import get_log_watermark

    # Flush to drain any startup entries before recording the watermark
    handler = getattr(
        getattr(log_audit_app_client.app, "state", None), "sqlite_log_handler", None
    )
    if handler is not None:
        handler.flush()

    return get_log_watermark(log_audit_app_client, admin_token_provider.get_token())


@pytest.fixture(scope="session", autouse=True)
def _phase3_log_audit_gate(
    log_audit_app_client: TestClient,
    admin_token_provider: AdminTokenProvider,
    log_watermark: int,
) -> Iterator[None]:
    """Autouse session fixture: run the log-audit gate at Phase 3 teardown.

    Yields first (tests run), then at teardown:
      1. Flush SQLiteLogHandler (deterministic drain, Bug #1078 mitigation).
      2. Query admin_logs_query via MCP front door.
      3. Diff against log_watermark to find new entries.
      4. Fail with detailed report if any new non-allowlisted ERROR/WARNING found.

    Calls admin_token_provider.get_token() at teardown time so the audit
    query uses a fresh token even if the phase ran longer than the JWT TTL.
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

    # Obtain a fresh-enough token at teardown time (not the session-start token).
    teardown_token = admin_token_provider.get_token()

    result = run_log_audit_gate(
        log_audit_app_client,
        teardown_token,
        watermark_id=log_watermark,
        phase_name="Phase 3 (Server In-Process)",
    )
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


def _seed_wait_for_job(
    client: TestClient,
    job_id: str,
    auth_headers: dict,
    label: str,
) -> None:
    """Poll GET /api/jobs/{job_id} until terminal state; fail loudly on timeout/failure.

    Bounded loop: terminates when either the deadline is reached (TimeoutError)
    or the job reaches a terminal state (Messi Rule #14 — provable termination).
    """
    deadline = time.monotonic() + _SEED_JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=auth_headers)
        assert resp.status_code < 500, (
            f"{label}: job poll returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            status = body.get("status")
            if status in _SEED_JOB_TERMINAL:
                assert status == "completed", (
                    f"{label}: job {job_id!r} ended with status {status!r}: {body}"
                )
                return
        time.sleep(_SEED_JOB_POLL_INTERVAL)
    raise TimeoutError(
        f"{label}: job {job_id!r} did not complete within {_SEED_JOB_TIMEOUT}s"
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

    # Poll until registration+indexing job completes.
    # Refresh headers before the long-running poll in case token nears expiry.
    _seed_wait_for_job(
        test_client, reg_job_id, admin_token_provider.get_headers(), "register"
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

    # Poll until activation job completes.
    _seed_wait_for_job(
        test_client, act_job_id, admin_token_provider.get_headers(), "activate"
    )

    # Yield the UNIFIED client (same app, same lifespan, no dual-app bug).
    yield test_client, alias
