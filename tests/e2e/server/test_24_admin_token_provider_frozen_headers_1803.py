"""Phase 3 -- Bug #1803 regression: a poll helper that captures
``admin_token_provider.get_headers()`` ONCE and reuses that frozen dict
across a long-running poll loop can outlive the JWT's real remaining
life, causing every subsequent poll to 401 even though
``AdminTokenProvider.get_token()``'s own near-expiry refresh logic is
correct (see ``test_admin_token_provider.py`` for that proof, and the
Bug #1803 investigation notes for the live, timestamped evidence that
ruled out an internal AdminTokenProvider defect).

Root cause: four independently-drifted job-poll helpers (test_22, this
directory's ``seeded_indexed_client``, test_18, test_21) each accepted a
frozen ``auth_headers: dict`` parameter, refreshed only ONCE before the
loop starts, then reused for every poll -- never re-consulting the
provider, so its correct refresh logic never gets a chance to run for the
loop's duration. In a real ~9-10 minute Phase 3 session, a poll loop
landing near the end of a refresh cycle can legitimately outlive the
captured token's remaining life. All four are now consolidated into ONE
canonical ``wait_for_terminal_job()`` in conftest.py (Messi Rule #4).

Reproduction here does not require waiting ~9-10 minutes: it builds an
ISOLATED in-process CIDX server (own tmp data dir -- never the shared
session ``test_client``) and forces a short JWT lifetime on that isolated
app's ``jwt_manager`` only (the same technique manually validated against
the running dev server during the Bug #1803 investigation). A real
``BackgroundJobManager`` job (no golden-repo/indexing dependency needed)
sleeps for longer than the token's forced lifetime, so the real expiry
boundary is crossed deterministically and fast, in real seconds -- no
mocking of the job system, the JWT machinery, or time.

Credentials come from the SAME required ``E2E_ADMIN_USER``/
``E2E_ADMIN_PASS`` environment variables every other fixture in this
directory requires (``_require_env`` in conftest.py) -- e2e-automation.sh
sets these for every phase before invoking pytest; no credential default
is embedded in source. Timing is environment-overridable too, mirroring
the existing ``E2E_GOLDEN_JOB_TIMEOUT``/``E2E_GOLDEN_JOB_POLL`` convention
in test_22/conftest.py -- validated at import time (positive, and the
JWT lifetime strictly shorter than the probe job duration, which is the
whole point of the reproduction) so a bad override fails loudly at
collection instead of producing a confusing runtime result.

The forced short ``jwt_manager.token_expiration_minutes`` is saved and
restored explicitly in the fixture's ``finally`` block, as defense-in-
depth alongside conftest.py's existing autouse
``_restore_auth_dependencies_globals`` guard (which additionally swaps
the whole ``jwt_manager`` object reference back after every test).

``_isolated_server_data_dir`` mutates the process-global
``CIDX_SERVER_DATA_DIR`` env var for its duration -- unguarded by a lock,
matching test_23_shared_app_globals_isolation.py's identical helper: this
suite runs single-threaded/sequential (no pytest-randomly/xdist plugin,
per this directory's own execution-order documentation), so no concurrent
test can observe the mutation.

Test 1 proves the underlying HTTP-level defect mechanism directly (a
frozen headers dict reused across real requests) -- a standing regression
guard for the ROOT CAUSE pattern itself, independent of any specific
helper function. Test 2 proves the FIX: the real, canonical
``wait_for_terminal_job()`` helper (conftest.py), given the token
provider and calling ``get_headers()`` fresh on every poll, survives the
same token-expiry boundary and observes the job's real completion. Test 3
is a structural guard against the DEFECT CLASS: it fails if a NEW
hand-rolled ``/api/jobs/{job_id}`` poll loop appears anywhere in this
directory outside the one canonical helper -- see that test's own
docstring for the itemized, explicitly-allowlisted pre-existing
instances this pass did not fix.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as auth_dependencies
from tests.e2e.server.conftest import (
    AdminTokenProvider,
    _require_env,
    wait_for_terminal_job,
)

_ADMIN_USERNAME: str = _require_env("E2E_ADMIN_USER")
_ADMIN_PASSWORD: str = _require_env("E2E_ADMIN_PASS")

# Forced short JWT lifetime for this isolated app only (never the shared
# session server) -- must stay shorter than _PROBE_JOB_SLEEP_SECONDS for
# the real expiry boundary to fall while the probe job is still running.
_SHORT_LIFETIME_SECONDS = float(os.environ.get("E2E_1803_SHORT_JWT_SECONDS", "12"))
_PROBE_JOB_SLEEP_SECONDS = float(os.environ.get("E2E_1803_PROBE_JOB_SECONDS", "20"))
_POLL_TIMEOUT_SECONDS = float(os.environ.get("E2E_1803_POLL_TIMEOUT", "35"))
_POLL_INTERVAL_SECONDS = float(os.environ.get("E2E_1803_POLL_INTERVAL", "1"))

for _name, _value in (
    ("E2E_1803_SHORT_JWT_SECONDS", _SHORT_LIFETIME_SECONDS),
    ("E2E_1803_PROBE_JOB_SECONDS", _PROBE_JOB_SLEEP_SECONDS),
    ("E2E_1803_POLL_TIMEOUT", _POLL_TIMEOUT_SECONDS),
    ("E2E_1803_POLL_INTERVAL", _POLL_INTERVAL_SECONDS),
):
    if not (_value > 0):
        raise RuntimeError(f"{_name} must be a positive number, got {_value!r}")
if not (_SHORT_LIFETIME_SECONDS < _PROBE_JOB_SLEEP_SECONDS):
    raise RuntimeError(
        "E2E_1803_SHORT_JWT_SECONDS must be strictly less than "
        "E2E_1803_PROBE_JOB_SECONDS -- the reproduction requires the "
        "forced JWT lifetime to expire while the probe job is still "
        f"running, got short_jwt={_SHORT_LIFETIME_SECONDS}s "
        f"probe_job={_PROBE_JOB_SLEEP_SECONDS}s"
    )
if not (_PROBE_JOB_SLEEP_SECONDS < _POLL_TIMEOUT_SECONDS):
    raise RuntimeError(
        "E2E_1803_PROBE_JOB_SECONDS must be strictly less than "
        "E2E_1803_POLL_TIMEOUT -- the poll loop must have time to observe "
        f"the probe job's real completion, got probe_job="
        f"{_PROBE_JOB_SLEEP_SECONDS}s poll_timeout={_POLL_TIMEOUT_SECONDS}s"
    )

_SHORT_LIFETIME_MINUTES = _SHORT_LIFETIME_SECONDS / 60.0
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401


@contextmanager
def _isolated_server_data_dir(data_dir: Path) -> Iterator[None]:
    """Temporarily point CIDX_SERVER_DATA_DIR at an isolated directory.

    Mirrors test_23_shared_app_globals_isolation.py's helper of the same
    name/purpose: an isolated data dir prevents this throwaway app from
    sharing the shared session test_client's SQLite connection.
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


def _do_login(client: TestClient) -> Tuple[str, str | None]:
    resp = client.post(
        "/auth/login",
        json={"username": _ADMIN_USERNAME, "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code == _HTTP_OK, (
        f"isolated-app login failed: {resp.status_code} -- {resp.text[:300]}"
    )
    body = resp.json()
    return str(body["access_token"]), body.get("refresh_token")


@pytest.fixture
def short_lived_provider(
    test_client: TestClient, tmp_path: Path
) -> Iterator[Tuple[TestClient, AdminTokenProvider]]:
    """Isolated app + AdminTokenProvider with a forced short JWT lifetime.

    Depends on the shared ``test_client`` fixture ONLY so the autouse
    ``_golden_auth_dependencies_snapshot``/``_restore_auth_dependencies_globals``
    guard fixtures (conftest.py) see the correct baseline before this
    fixture's own throwaway ``create_app()`` call, and restore it
    afterward -- exactly the pattern test_23_shared_app_globals_isolation.py
    establishes for a second create_app() inside this shared pytest
    session.
    """
    from code_indexer.server.app import create_app

    with _isolated_server_data_dir(tmp_path / "isolated-data-dir"):
        app = create_app()
        this_jwt_manager = auth_dependencies.jwt_manager
        assert this_jwt_manager is not None, (
            "create_app() did not wire auth_dependencies.jwt_manager"
        )
        previous_lifetime = this_jwt_manager.token_expiration_minutes
        # int-typed in production; deliberate fractional override for sub-minute test timing.
        this_jwt_manager.token_expiration_minutes = _SHORT_LIFETIME_MINUTES  # type: ignore[assignment]
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                access, refresh = _do_login(client)

                def _relogin() -> Tuple[str, str | None]:
                    return _do_login(client)

                def _refresh_via_grant(rt: str):
                    r = client.post("/api/auth/refresh", json={"refresh_token": rt})
                    if r.status_code != _HTTP_OK:
                        return None
                    b = r.json()
                    return str(b["access_token"]), b.get("refresh_token")

                provider = AdminTokenProvider(
                    login_fn=_relogin,
                    initial_access_token=access,
                    initial_refresh_token=refresh,
                    refresh_fn=_refresh_via_grant,
                )
                yield client, provider
        finally:
            this_jwt_manager.token_expiration_minutes = previous_lifetime


def _probe_job_func() -> dict:
    """Real job body: sleep for the configured probe duration, then
    complete -- gives the poll loop a deterministic, real-time-controlled
    non-terminal window to observe (or, pre-fix, fail to observe past a
    forced token expiry)."""
    time.sleep(_PROBE_JOB_SLEEP_SECONDS)
    return {"probed": True}


def _submit_probe_job(client: TestClient) -> str:
    """Submit a real BackgroundJobManager job that sleeps then completes.

    No golden-repo/indexing dependency: exercises the same real job
    tracking + GET /api/jobs/{job_id} status path the reindex job in
    test_22 uses, without needing VoyageAI or a seeded repo.
    """
    app = cast(FastAPI, client.app)
    job_id = app.state.background_job_manager.submit_job(
        operation_type="test_probe_1803",
        func=_probe_job_func,
        submitter_username=_ADMIN_USERNAME,
        is_admin=True,
        repo_alias="test-probe-1803",
    )
    return str(job_id)


class TestBug1803FrozenHeadersAcrossPollLoop:
    """AC: a poll helper must re-consult the token provider on every
    request, not reuse a single get_headers() snapshot across the whole
    loop -- otherwise a real token expiry mid-loop makes the loop unable
    to ever observe the job's real completion."""

    def test_frozen_headers_snapshot_misses_job_completion_past_expiry(
        self,
        short_lived_provider: Tuple[TestClient, AdminTokenProvider],
    ) -> None:
        """Standing regression guard for the ROOT CAUSE mechanism itself:
        capturing get_headers() ONCE and reusing it for every poll request
        goes stale (401) before a real, still-running job completes."""
        client, provider = short_lived_provider
        job_id = _submit_probe_job(client)

        frozen_headers = provider.get_headers()

        deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
        observed_401 = False
        observed_completed = False
        while time.monotonic() < deadline:
            resp = client.get(f"/api/jobs/{job_id}", headers=frozen_headers)
            if resp.status_code == _HTTP_UNAUTHORIZED:
                observed_401 = True
            elif resp.status_code == _HTTP_OK and resp.json().get("status") == (
                "completed"
            ):
                observed_completed = True
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert observed_401, (
            "expected the frozen headers snapshot to go stale (401) before "
            "the probe job finished -- if this no longer happens, the "
            "forced short JWT lifetime in this test is no longer shorter "
            "than the probe job's real duration."
        )
        assert not observed_completed, (
            "Bug #1803 regression: a poll loop reusing a frozen "
            "get_headers() snapshot must NEVER observe a job's real "
            "completion once the token has genuinely expired mid-loop."
        )

    def test_provider_based_polling_survives_token_expiry_mid_loop(
        self,
        short_lived_provider: Tuple[TestClient, AdminTokenProvider],
    ) -> None:
        """Proves the fix: the real, canonical wait_for_terminal_job()
        helper (conftest.py), given the token provider itself (not a
        frozen dict), refreshes on every poll and correctly observes the
        job's real completion despite the same token-expiry boundary."""
        client, provider = short_lived_provider
        job_id = _submit_probe_job(client)

        body = wait_for_terminal_job(
            client,
            job_id,
            provider,
            timeout=_POLL_TIMEOUT_SECONDS,
            poll_interval=_POLL_INTERVAL_SECONDS,
        )

        assert body.get("status") == "completed", (
            f"Bug #1803 regression: provider-based polling should observe "
            f"the job's real completion despite a token expiry mid-loop, "
            f"got: {body}"
        )


# ---------------------------------------------------------------------------
# Test 3: structural guard against the DEFECT CLASS (not just the two
# instances originally fixed).
# ---------------------------------------------------------------------------

# Confirmed SAFE: these files' own poll loop already calls a fresh
# headers-producing expression (a function call, not a bare frozen dict)
# on every iteration, so they do not exhibit Bug #1803's mechanism even
# though they match the coarse textual pattern below.
_KNOWN_SAFE_JOB_POLL_FILES = frozenset(
    {
        # headers_fn() called fresh every poll iteration by design (its own
        # docstring: "called on EVERY poll iteration ... never hit 401").
        "test_14_snapshot_retention_1134.py",
        # _auth_headers(user_token) rebuilt fresh every iteration; the
        # underlying token itself doesn't auto-refresh, but the poll is
        # hard-bounded at 120s, far under any realistic JWT lifetime --
        # cannot practically cross the expiry boundary this bug requires.
        "test_14_auth_never_cached.py",
        # _poll_job's headers param is a get_headers: Callable[[], dict],
        # called fresh every iteration (fixed 2026-09-07 -- kept as a local
        # loop rather than routed through wait_for_terminal_job() since
        # this domain's helper chain has several call sites with a mixed
        # single-shot/polling headers usage that didn't warrant forcing
        # onto the shared helper's exact signature).
        "test_12_xray_functional_1129.py",
        # _drain_jobs takes admin_token_provider directly and calls
        # .get_headers() fresh every iteration (fixed 2026-09-07). Kept
        # local rather than routed through wait_for_terminal_job(): this
        # helper drains a LIST of jobs with any-terminal-state/404-as-
        # drained teardown semantics that helper's single-job,
        # completed-only contract does not express.
        "test_13_depmap_coordination_1133.py",
    }
)

# Confirmed VULNERABLE, not yet fixed. Empty as of 2026-09-07: the other 5
# instances found this session (test_09, test_12, test_13, test_16,
# test_19 -- test_19's own `_wait_for_job` carried a 900s timeout, the
# single highest real-world risk in the suite) are now either fully
# delegated to wait_for_terminal_job() (test_09/16/18/19/21 -- no longer
# even match the textual pattern below) or fixed in place and moved to
# _KNOWN_SAFE_JOB_POLL_FILES above (test_12/13). A newly-discovered
# instance goes here ONLY as a deliberate, reviewed decision to defer its
# fix -- never as a silent catch-all.
_KNOWN_PRE_EXISTING_OFFENDER_JOB_POLL_FILES: frozenset[str] = frozenset()

_SELF_FILENAME = Path(__file__).name


def _files_with_hand_rolled_job_poll_loop() -> list[str]:
    """Coarse textual scan: a file containing BOTH the job-status endpoint
    template and a monotonic poll loop is presumed to hand-roll its own
    polling instead of delegating to wait_for_terminal_job()."""
    server_dir = Path(__file__).parent
    found = []
    for path in sorted(server_dir.rglob("*.py")):
        if path.name in ("conftest.py", _SELF_FILENAME):
            continue
        text = path.read_text()
        if "/api/jobs/{job_id}" in text and "while time.monotonic()" in text:
            found.append(path.name)
    return found


def test_no_new_hand_rolled_job_poll_loop_outside_shared_helper() -> None:
    """Structural regression guard for the Bug #1803 DEFECT CLASS: any
    file in this directory that polls GET /api/jobs/{job_id} in its own
    while-loop, instead of calling the single canonical
    wait_for_terminal_job() in conftest.py, is a reintroduction risk -- a
    hand-rolled loop can always recreate the frozen-headers pattern this
    bug proved. Two explicit, commented allowlists above cover today's
    known state (confirmed-safe and confirmed-vulnerable-but-not-yet-
    fixed); anything outside both is a genuinely NEW offender and fails
    this test immediately.
    """
    found = set(_files_with_hand_rolled_job_poll_loop())
    known = _KNOWN_SAFE_JOB_POLL_FILES | _KNOWN_PRE_EXISTING_OFFENDER_JOB_POLL_FILES

    unexpected = sorted(found - known)
    assert not unexpected, (
        "Bug #1803 defect class reintroduced (or a newly-discovered "
        "pre-existing instance not yet triaged) -- these files hand-roll "
        "their own /api/jobs/{job_id} poll loop instead of calling the "
        f"canonical wait_for_terminal_job() in conftest.py: {unexpected}. "
        "Either route them through wait_for_terminal_job(), or -- after "
        "confirming the loop already refreshes headers fresh every "
        "iteration -- add them to _KNOWN_SAFE_JOB_POLL_FILES with a "
        "justification, or to "
        "_KNOWN_PRE_EXISTING_OFFENDER_JOB_POLL_FILES if confirmed "
        "vulnerable but deliberately deferred."
    )

    stale_offenders = sorted(_KNOWN_PRE_EXISTING_OFFENDER_JOB_POLL_FILES - found)
    assert not stale_offenders, (
        f"{stale_offenders} no longer match the hand-rolled-poll-loop "
        "pattern -- remove them from "
        "_KNOWN_PRE_EXISTING_OFFENDER_JOB_POLL_FILES (this is good news: "
        "someone already fixed them)."
    )
