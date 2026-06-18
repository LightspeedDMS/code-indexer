"""Phase 3 E2E — Story #1133 (Epic #1121): Dep-Map Coordination.

Re-Entrancy (409) + Deterministic Anomalies, all through the REST / MCP front
door against the in-process FastAPI server (TestClient).  No Claude, no external
API key, no mocks.

What is validated
-----------------
AC1 — synchronous-sentinel re-entrancy guard (Story #1035 / #1133)
    The dep-map trigger claims a SharedJobSentinel SYNCHRONOUSLY (before
    returning 202).  While the sentinel is held, a second trigger returns
    409 with the active job_id (REST: literal HTTP 409; MCP: success=False,
    error="already in progress").  After release, a fresh trigger is accepted
    (202).  Concurrent triggers resolve to exactly ONE 202 + ONE 409.

AC2 — deterministic anomalies (NO Claude)
    Hand-written deterministic anomaly files seeded into the dep-map read path
    (cidx-meta/dependency-map/) are surfaced and channel-classified by the
    read-only MCP tool ``depmap_get_cross_domain_graph``:
        - SELF_LOOP               (data channel)
        - GARBAGE_DOMAIN_REJECTED (data channel)
        - MALFORMED_YAML          (parser channel)
    BIDIRECTIONAL_MISMATCH (Claude-audited) is OUT of scope.

Determinism approach (resolved empirically via manual front-door execution)
---------------------------------------------------------------------------
AC1 uses **sentinel seeding** (prompt option (b)): a sentinel lock file is
written via the real ``SharedJobSentinel.try_claim`` API — the EXACT artifact a
second cluster node writes for an in-flight job.  This is a legitimate
coordination artifact, NOT a mock.  A single real front-door trigger then
deterministically returns 409 with that seeded job_id; releasing the sentinel
and re-triggering returns 202.  This avoids the flaky "keep the worker alive"
race (empirically, the accepted worker finishes in <1s, releasing its own
sentinel almost immediately).

The MCP trigger checks ``dependency_map_enabled`` BEFORE the sentinel claim, so
the feature must be enabled first.  That is done through the real Web-UI config
front door (POST /admin/config/claude_cli with a session cookie + CSRF token) —
``set_global_config`` is only for the repo-sync ``refresh_interval`` and does
not accept this key.

The accepted (202) trigger spawns a real dep-map worker which schedules a
cidx-meta-global refresh.  In this empty in-process harness that refresh fails
deterministically with "No files found to index" (and, for overlapping accepted
triggers, a "global_repo_refresh already running for cidx-meta-global" duplicate)
— benign zero-data artifacts allowlisted in ``log_audit_gate.LOG_AUDIT_ALLOWLIST``
via three cidx-meta-global-anchored substrings.  The AC2 MALFORMED_YAML parser
WARNING (the asserted signal of the unclosed-frontmatter seed) is allowlisted
separately, anchored on the test-unique seed filename.  The sentinel coordination
under test is independent of all of these.

Fixtures
--------
Uses the lightweight ``test_client`` + ``auth_headers`` (NOT
``seeded_indexed_client``): ``DependencyMapService.get_sentinel_dir()`` is
already non-None with bare ``test_client`` (golden_repos_manager is wired at
app construction), so no golden repo / VOYAGE key is required.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

# ---------------------------------------------------------------------------
# Module skip-guard: admin credentials must be present (mirror other Phase-3
# server tests). Without them no login is possible and the whole module skips.
# ---------------------------------------------------------------------------
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"

pytestmark = pytest.mark.skipif(
    not (os.environ.get(_ENV_ADMIN_USER) and os.environ.get(_ENV_ADMIN_PASS)),
    reason=(
        f"{_ENV_ADMIN_USER}/{_ENV_ADMIN_PASS} not set — run via e2e-automation.sh "
        "or export admin credentials manually."
    ),
)

# ---------------------------------------------------------------------------
# Front-door endpoint + protocol constants (no inline magic strings in tests)
# ---------------------------------------------------------------------------
LOGIN_PAGE = "/login"
CONFIG_PAGE = "/admin/config"
CONFIG_POST_CLAUDE_CLI = "/admin/config/claude_cli"
REST_TRIGGER = "/admin/dependency-map/trigger"
JOB_STATUS_TMPL = "/api/jobs/{job_id}"

MCP_TRIGGER_TOOL = "trigger_dependency_analysis"
MCP_GRAPH_TOOL = "depmap_get_cross_domain_graph"

# HTTP status codes asserted at the front door
HTTP_ACCEPTED = 202
HTTP_CONFLICT = 409

# Sentinel op_type + payload identity used by the dep-map analysis coordination
SENTINEL_OP_ANALYSIS = "analysis"
SEED_NODE_ID = "test-1133-other-node"

# Terminal background-job states (front-door /api/jobs/{id})
_TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

# Bounded-loop budgets (monotonic deadline — Messi Rule #14)
_JOB_DRAIN_TIMEOUT_S = 30.0
_JOB_DRAIN_POLL_S = 0.25

# CSRF token is rendered as a hidden form input on the login + config pages.
_CSRF_INPUT_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


# ===========================================================================
# Front-door helpers
# ===========================================================================
def _extract_csrf(html: str) -> str:
    """Return the csrf_token value from a rendered page, or "" if absent."""
    match = _CSRF_INPUT_RE.search(html)
    return match.group(1) if match else ""


def _enable_dependency_map(client: TestClient) -> None:
    """Enable ``dependency_map_enabled`` through the real Web-UI config front door.

    Establishes an admin WEB SESSION (cookie) — distinct from the JWT bearer
    used for MCP — then POSTs the claude_cli config section with the feature
    flag set.  This is required because the MCP/REST trigger handlers gate on
    ``dependency_map_enabled`` BEFORE the sentinel claim.

    Bounded: no loops; a fixed sequence of front-door calls.
    """
    username = os.environ[_ENV_ADMIN_USER]
    password = os.environ[_ENV_ADMIN_PASS]

    login_page = client.get(LOGIN_PAGE)
    assert login_page.status_code == 200, (
        f"GET {LOGIN_PAGE} failed: {login_page.status_code}"
    )
    csrf = _extract_csrf(login_page.text)

    login_resp = client.post(
        LOGIN_PAGE,
        data={"username": username, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    # Successful unified login returns a 303 redirect and sets the session cookie.
    assert login_resp.status_code in (302, 303), (
        f"POST {LOGIN_PAGE} expected redirect, got {login_resp.status_code}: "
        f"{login_resp.text[:200]}"
    )
    assert "session" in client.cookies, (
        "web login did not set a session cookie — cannot drive the config front door"
    )

    config_page = client.get(CONFIG_PAGE)
    assert config_page.status_code == 200, (
        f"GET {CONFIG_PAGE} failed: {config_page.status_code} — "
        f"{config_page.text[:200]}"
    )
    csrf2 = _extract_csrf(config_page.text) or csrf

    save_resp = client.post(
        CONFIG_POST_CLAUDE_CLI,
        data={"dependency_map_enabled": "true", "csrf_token": csrf2},
        follow_redirects=False,
    )
    assert save_resp.status_code in (200, 302, 303), (
        f"POST {CONFIG_POST_CLAUDE_CLI} failed: {save_resp.status_code} — "
        f"{save_resp.text[:200]}"
    )

    # Verify the runtime config actually flipped (front-door write took effect).
    from code_indexer.server.services.config_service import get_config_service

    cfg = get_config_service().get_config()
    ci = cfg.claude_integration_config if cfg else None
    assert ci is not None and ci.dependency_map_enabled is True, (
        "dependency_map_enabled did not flip to True after the config-screen POST"
    )
    # NOTE: the web session cookie is deliberately RETAINED — the REST trigger
    # route (/admin/dependency-map/trigger) authenticates via _require_admin_session
    # (web session), NOT the JWT bearer.  The MCP calls pass the JWT explicitly via
    # auth_headers and are unaffected by the cookie.  The cookie is cleared at module
    # teardown (see depmap_enabled_client) so sibling Phase-3 tests are unaffected.


def _dep_map_service(client: TestClient):
    """Return the live server's DependencyMapService from app state.

    Uses getattr against ``client.app`` (typed by mypy as the bare ASGI callable,
    which has no ``.state`` attribute) so static typing is satisfied while the
    runtime behaviour is identical to ``client.app.state.dependency_map_service``.
    """
    state = getattr(client.app, "state", None)
    service = getattr(state, "dependency_map_service", None)
    assert service is not None, (
        "dependency_map_service is not wired on app.state — the dep-map "
        "coordination front door is unavailable in this harness"
    )
    return service


def _new_sentinel(client: TestClient):
    """Build a SharedJobSentinel bound to the live server's sentinel directory.

    Resolves the directory from the SAME source the production handlers use:
    ``app.state.dependency_map_service.get_sentinel_dir()`` — the single source
    of truth (Story #1035 B6).  Asserts it is non-None so the synchronous-claim
    409 path is guaranteed to be active in this harness (Codex RISK 3).
    """
    from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel
    from code_indexer.server.services.dependency_map_service import (
        ANALYSIS_STALE_TIMEOUT_SECONDS,
    )

    dep_map_service = _dep_map_service(client)
    sentinel_dir = dep_map_service.get_sentinel_dir()
    assert sentinel_dir is not None, (
        "get_sentinel_dir() returned None — the synchronous-claim block would be "
        "SKIPPED and the 409 guard would be non-deterministic (Codex RISK 3)."
    )
    return SharedJobSentinel(
        sentinel_dir=Path(sentinel_dir),
        stale_timeout_seconds=ANALYSIS_STALE_TIMEOUT_SECONDS,
    )


def _wait_for_service_available(
    client: TestClient, *, timeout_s: float = _JOB_DRAIN_TIMEOUT_S
) -> bool:
    """Bounded-wait until dep_map_service.is_available() returns True.

    is_available() checks BOTH the SharedJobSentinel file AND the in-process
    threading.Lock.  Waiting only for the sentinel file to disappear misses the
    window where the prior worker's finally block has not yet called
    self._lock.release() (which happens BEFORE _sentinel.release() in
    run_full_analysis).  If the test's force-clear removed the sentinel file
    while the prior worker still holds the lock, is_available() returns False
    and both concurrent triggers in test_ac1_concurrent_triggers_single_winner
    get 409 — the [409,409] failure.

    Returns True when the service becomes available within timeout_s; False
    when the deadline expires (caller decides whether to assert or proceed).
    Bounded by timeout_s (Messi Rule #14).
    """
    dep_map_service = _dep_map_service(client)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if dep_map_service.is_available():
            return True
        time.sleep(_JOB_DRAIN_POLL_S)
    return bool(dep_map_service.is_available())


def _drain_jobs(client: TestClient, auth_headers: dict, job_ids: list[str]) -> None:
    """Bounded-wait until each given background job reaches a terminal state.

    Ensures any worker spawned by an accepted (202) trigger has finished (and
    its log lines flushed) before the test returns, so teardown and the
    session log-audit gate observe a settled state.  Monotonic-deadline bound
    (Messi Rule #14): terminates on terminal state OR deadline — never hangs.
    """
    for job_id in job_ids:
        if not job_id:
            continue
        deadline = time.monotonic() + _JOB_DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            resp = client.get(
                JOB_STATUS_TMPL.format(job_id=job_id), headers=auth_headers
            )
            if resp.status_code == 200:
                if resp.json().get("status") in _TERMINAL_JOB_STATES:
                    break
            elif resp.status_code == 404:
                # Job record already pruned — treat as drained.
                break
            time.sleep(_JOB_DRAIN_POLL_S)


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture(scope="module")
def depmap_enabled_client(
    test_client: TestClient,
) -> Iterator[TestClient]:
    """Yield a TestClient with ``dependency_map_enabled`` turned on (front door).

    Module-scoped: the flag is a process-wide runtime config, enabled once for
    all tests in this module.  No teardown reset is required — the config lives
    in the session-scoped ``test_client`` data dir which is discarded with the
    session.

    Note: ``auth_headers`` is intentionally NOT a parameter here — it is
    ``scope="function"`` (for JWT near-expiry refresh) and cannot be requested
    by a ``scope="module"`` fixture.  The fixture body does not need it:
    ``_enable_dependency_map`` performs its own web-form login internally.
    """
    _enable_dependency_map(test_client)
    try:
        yield test_client
    finally:
        # Drop the web session cookie so sibling Phase-3 tests sharing the
        # session-scoped test_client see a clean (JWT-only) auth surface.
        test_client.cookies.clear()


@pytest.fixture()
def held_sentinel(depmap_enabled_client: TestClient) -> Iterator[str]:
    """Seed an 'analysis' sentinel simulating a concurrent in-flight cluster node.

    Yields the seeded job_id.  Teardown ALWAYS releases the sentinel (owner-only
    delete via the real API) so later tests + the session log-audit gate are
    unaffected, even if the test body raised.

    Pre-seeding the sentinel is a REAL coordination artifact (the identical file
    a second cluster node writes via O_CREAT|O_EXCL), NOT a mock.
    """
    sentinel = _new_sentinel(depmap_enabled_client)
    seed_job_id = f"seed-inflight-{int(time.time() * 1000)}"

    claim = sentinel.try_claim(SENTINEL_OP_ANALYSIS, seed_job_id, SEED_NODE_ID)
    assert claim.success, (
        "pre-seed sentinel claim failed — another analysis sentinel is already "
        "held in this harness; cannot establish a deterministic in-flight state"
    )
    try:
        yield seed_job_id
    finally:
        sentinel.release(SENTINEL_OP_ANALYSIS, expected_job_id=seed_job_id)


# ===========================================================================
# AC1 — synchronous-sentinel re-entrancy guard (409)
# ===========================================================================
def test_ac1_rest_trigger_returns_409_with_active_job_id(
    depmap_enabled_client: TestClient, auth_headers: dict, held_sentinel: str
) -> None:
    """REST trigger while sentinel held -> literal HTTP 409 carrying active job_id.

    Exercises dependency_map_routes.py:1794-1801 (synchronous-claim conflict).
    """
    resp = depmap_enabled_client.post(REST_TRIGGER, data={"mode": "full"})

    assert resp.status_code == HTTP_CONFLICT, (
        f"expected HTTP 409 while sentinel held, got {resp.status_code}: "
        f"{resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("error") == "already in progress", (
        f"unexpected 409 error body: {body}"
    )
    assert body.get("job_id") == held_sentinel, (
        f"409 must surface the active (seeded) job_id {held_sentinel!r}, "
        f"got {body.get('job_id')!r}"
    )


def test_ac1_mcp_trigger_returns_conflict_with_active_job_id(
    depmap_enabled_client: TestClient, auth_headers: dict, held_sentinel: str
) -> None:
    """MCP trigger while sentinel held -> success=False + 'already in progress' + job_id.

    Exercises mcp/handlers/admin/__init__.py:1627-1634 (sentinel read-back on the
    pre-flight conflict).  MCP wraps the conflict as HTTP 200 with success=False.
    """
    resp = call_mcp_tool(
        depmap_enabled_client, MCP_TRIGGER_TOOL, {"mode": "full"}, auth_headers
    )
    assert resp.status_code == 200, (
        f"MCP tools/call HTTP {resp.status_code}: {resp.text[:300]}"
    )
    result = parse_mcp_result(resp.json())

    assert result.get("success") is False, (
        f"MCP trigger should report success=False while sentinel held: {result}"
    )
    assert result.get("error") == "already in progress", (
        f"unexpected MCP conflict error: {result}"
    )
    assert result.get("job_id") == held_sentinel, (
        f"MCP conflict must surface the seeded active job_id {held_sentinel!r}, "
        f"got {result.get('job_id')!r}"
    )


def test_ac1_release_then_trigger_is_accepted(
    depmap_enabled_client: TestClient, auth_headers: dict
) -> None:
    """Seed -> 409 -> release -> fresh trigger is ACCEPTED (202).

    Proves the guard is not a permanent lockout: once the in-flight sentinel is
    released, the very next front-door trigger is accepted with a fresh job_id.

    The accepted worker spawns a cidx-meta-global refresh that fails on empty
    data (allowlisted in log_audit_gate); we drain it to a terminal state so the
    session gate observes a settled run.
    """
    sentinel = _new_sentinel(depmap_enabled_client)
    seed_job_id = f"seed-release-{int(time.time() * 1000)}"
    accepted_job_ids: list[str] = []

    claim = sentinel.try_claim(SENTINEL_OP_ANALYSIS, seed_job_id, SEED_NODE_ID)
    assert claim.success, "pre-seed claim failed for release test"
    try:
        # While held -> 409 (control: guard is active).
        held = depmap_enabled_client.post(REST_TRIGGER, data={"mode": "full"})
        assert held.status_code == HTTP_CONFLICT, (
            f"expected 409 while held, got {held.status_code}: {held.text[:200]}"
        )

        # Release the in-flight sentinel (owner-only delete).
        sentinel.release(SENTINEL_OP_ANALYSIS, expected_job_id=seed_job_id)
        assert sentinel.read_active(SENTINEL_OP_ANALYSIS) is None, (
            "sentinel still present after owner release — cannot prove acceptance"
        )

        # Now a fresh trigger must be ACCEPTED (202) with a NEW job_id.
        accepted = depmap_enabled_client.post(REST_TRIGGER, data={"mode": "full"})
        assert accepted.status_code == HTTP_ACCEPTED, (
            f"expected 202 after release, got {accepted.status_code}: "
            f"{accepted.text[:300]}"
        )
        accepted_body = accepted.json()
        assert accepted_body.get("success") is True, (
            f"accepted trigger body should report success: {accepted_body}"
        )
        new_job_id = accepted_body.get("job_id")
        assert new_job_id and new_job_id != seed_job_id, (
            f"accepted trigger must mint a fresh job_id (not the seeded "
            f"{seed_job_id!r}): {accepted_body}"
        )
        accepted_job_ids.append(new_job_id)
    finally:
        # Defensive: release the seed ONLY if it is STILL the active owner (i.e.
        # the body short-circuited before its own release).  After a successful
        # body run the sentinel belongs to the accepted WORKER's job, so an
        # owner-mismatched release here would emit a benign-but-flagged
        # "NOT releasing" WARNING — guard against that by checking ownership.
        active = sentinel.read_active(SENTINEL_OP_ANALYSIS)
        if active is not None and active.job_id == seed_job_id:
            sentinel.release(SENTINEL_OP_ANALYSIS, expected_job_id=seed_job_id)
        _drain_jobs(depmap_enabled_client, auth_headers, accepted_job_ids)

        # Wait until dep_map_service.is_available() returns True before returning.
        #
        # is_available() checks BOTH the sentinel file AND the in-process
        # threading.Lock.  run_full_analysis.finally releases them in this order:
        #   1. self._lock.release()       <- threading.Lock released first
        #   2. _sentinel.release(...)     <- sentinel released second
        # When the worker raises an exception the except block calls fail_job
        # (marking the job terminal) and then raises; only then does the finally
        # run.  _drain_jobs can therefore return (job terminal) while the finally
        # block has not yet run at all, meaning the Lock AND the sentinel are
        # still held.  Using sentinel-only wait is not sufficient because after
        # the sentinel clears the lock is guaranteed free; but under full-phase
        # load a force-clear in the next test's precheck can remove the sentinel
        # file while the worker still holds the lock, leaving is_available()
        # returning False and causing [409,409].
        #
        # Waiting for is_available() covers both resources with one predicate
        # (Messi Rule #14: bounded wait).
        _wait_for_service_available(depmap_enabled_client)


async def test_ac1_concurrent_triggers_single_winner(
    depmap_enabled_client: TestClient, auth_headers: dict
) -> None:
    """Two truly-concurrent REST triggers -> exactly ONE 202 + ONE 409.

    Directly exercises the synchronous-claim single-winner property: the sentinel
    O_CREAT|O_EXCL claim admits exactly one trigger; the loser is rejected with
    409.  No pre-seed — the contention is real and concurrent.

    Concurrency approach: httpx.AsyncClient + ASGITransport(app) +
    asyncio.gather().  Two coroutines land on the same event loop concurrently;
    FastAPI dispatches sync routes (Form-based) to a ThreadPoolExecutor so both
    route handlers run in parallel OS threads, creating real O_CREAT|O_EXCL
    contention on the shared sentinel.

    Why NOT threading.Barrier + shared TestClient: Starlette TestClient uses a
    single anyio.BlockingPortal when entered as a context manager.  portal.call()
    is synchronous and serialises on the portal's event loop, so the two threads
    run sequentially — the first accepted trigger's worker can release its sentinel
    before the second thread's request even starts, yielding two 202s.
    """
    # Order-independence: a prior test's accepted worker may still own an
    # in-flight 'analysis' sentinel OR hold the in-process threading.Lock.
    # We must wait for dep_map_service.is_available() — not just for the
    # sentinel file to disappear — because is_available() checks BOTH:
    #   (a) SharedJobSentinel file (cluster-wide)
    #   (b) threading.Lock (in-process belt)
    #
    # Waiting only for the sentinel file and then force-clearing it creates the
    # [409,409] failure: if force-clear removes the sentinel while the prior
    # worker's finally block is between self._lock.release() and
    # _sentinel.release(), is_available() returns False (lock still held) and
    # both concurrent requests get a 409 from the pre-flight check.
    #
    # Strategy:
    #   Phase 1 — bounded wait for is_available() (both sentinel + lock free).
    #   Phase 2 — if service is still unavailable, force-clear ONLY the sentinel
    #             file (to unblock a stale/leaked sentinel), then do a SHORT
    #             bounded wait for is_available() to confirm the lock is also
    #             free.  This preserves the legitimate test-setup purpose of the
    #             force-clear while guaranteeing we never fire the concurrent pair
    #             while the threading.Lock is held.
    #
    # (Messi Rule #14: all loops must have provable termination bounds.)
    precheck_sentinel = _new_sentinel(depmap_enabled_client)
    service_available = _wait_for_service_available(depmap_enabled_client)

    if not service_available:
        # Service still unavailable after the full wait.  The only residual holder
        # at this point is a worker from a PRIOR test — not the concurrent pair
        # we are about to fire.  Force-clear the sentinel file to unblock a
        # stale/leaked lock file, then give the threading.Lock a short window to
        # release (it should be nearly free since the lock releases BEFORE the
        # sentinel in run_full_analysis.finally).
        sentinel_path = (
            precheck_sentinel._sentinel_dir / f"_active_{SENTINEL_OP_ANALYSIS}.lock"
        )
        try:
            os.unlink(str(sentinel_path))
        except FileNotFoundError:
            pass  # Already cleared by a concurrent release — that's fine.

        # SHORT bounded wait: after the sentinel file is gone, the threading.Lock
        # should release imminently (lock.release() ran just before sentinel.release()
        # so the lock is either already free or will be within milliseconds).
        # We wait up to _JOB_DRAIN_TIMEOUT_S for is_available() rather than just
        # checking the sentinel to avoid the race.
        service_available = _wait_for_service_available(depmap_enabled_client)

    assert service_available, (
        "dep_map_service.is_available() still False after bounded wait and force-clear — "
        "analysis sentinel or threading.Lock is stuck; cannot establish zero-contention "
        "baseline for concurrent-triggers test"
    )

    # Extract the session cookie that _enable_dependency_map() set on the
    # TestClient (required by _require_admin_session on the REST trigger).
    session_cookies = dict(depmap_enabled_client.cookies)
    app = depmap_enabled_client.app

    accepted_job_ids: list[str] = []
    try:
        # Two genuinely-concurrent POSTs via ASGITransport.  asyncio.gather()
        # launches both coroutines before either awaits, so they contend on the
        # sentinel claim in parallel FastAPI thread-pool workers.
        async def _fire() -> int:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                cookies=session_cookies,
            ) as ac:
                resp = await ac.post(REST_TRIGGER, data={"mode": "full"})
                return resp.status_code

        status_a, status_b = await asyncio.gather(_fire(), _fire())
        statuses = sorted([status_a, status_b])

        assert statuses == [HTTP_ACCEPTED, HTTP_CONFLICT], (
            f"concurrent triggers must yield exactly one 202 + one 409, got {statuses}"
        )
    finally:
        # Drain whatever job the single winner spawned (best-effort): the winner's
        # sentinel auto-releases when its worker finishes; query the active job (if
        # any) and drain by job list is not exposed, so we rely on the bounded
        # job-state drain only for ids we can observe. The winner's worker is short
        # and self-releasing, so a brief settle keeps teardown clean.
        sentinel = _new_sentinel(depmap_enabled_client)
        deadline = time.monotonic() + _JOB_DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            active = sentinel.read_active(SENTINEL_OP_ANALYSIS)
            if active is None:
                break
            accepted_job_ids = [active.job_id]
            time.sleep(_JOB_DRAIN_POLL_S)
        _drain_jobs(depmap_enabled_client, auth_headers, accepted_job_ids)


# ===========================================================================
# AC2 — deterministic anomalies read back via the read-only MCP graph tool
# ===========================================================================
# Domain names used by the seeded anomaly fixture.
_DOMAIN_SELF = "alpha"
_DOMAIN_TARGET = "beta"
# Test-unique token so the parser's MALFORMED_YAML WARNING can be allowlisted by
# filename WITHOUT masking parse failures of any real domain file.
_DOMAIN_BROKEN = "broken_1133_malformed"

# A prose-fragment target domain: contains '(' and ':' so is_prose_fragment()
# rejects it -> GARBAGE_DOMAIN_REJECTED.
_GARBAGE_TARGET = "this is (prose): not a domain"

_DOMAINS_JSON = json.dumps(
    [
        {"name": _DOMAIN_SELF, "participating_repos": ["repoA"]},
        {"name": _DOMAIN_TARGET, "participating_repos": ["repoB"]},
        {"name": _DOMAIN_BROKEN, "participating_repos": ["repoC"]},
    ]
)

# alpha.md: a SELF_LOOP outgoing row (target == alpha), a GARBAGE prose-fragment
# target, and a real alpha->beta edge.  Outgoing table layout matches
# dep_map_parser_graph columns: [This Repo, Repo, Target Domain, Dep Type, ...].
_ALPHA_MD = f"""---
domain: {_DOMAIN_SELF}
last_analyzed: 2025-01-01T00:00:00Z
---
## Cross-Domain Dependencies
### Outgoing Dependencies
| This Repo | Repo | Target Domain | Dep Type | Why | Evidence |
|-----------|------|---------------|----------|-----|----------|
| repoA | repoA | {_DOMAIN_SELF} | code-level | self reference | ev |
| repoA | repoA | {_GARBAGE_TARGET} | code-level | garbage target | ev |
| repoA | repoA | {_DOMAIN_TARGET} | code-level | real edge | ev |
"""

# beta.md: confirms the alpha->beta incoming claim so the only data anomalies
# from the real edge are the intended SELF_LOOP + GARBAGE, not extra noise.
_BETA_MD = f"""---
domain: {_DOMAIN_TARGET}
last_analyzed: 2025-01-01T00:00:00Z
---
## Cross-Domain Dependencies
### Incoming Dependencies
| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---------------|-----------|---------------|----------|-----|----------|
| repoA | repoB | {_DOMAIN_SELF} | code-level | real edge | ev |
"""

# broken.md: frontmatter opens with '---' but never closes -> parse_frontmatter_strict
# raises -> MALFORMED_YAML (parser channel).
_BROKEN_MD = (
    "---\n"
    f"domain: {_DOMAIN_BROKEN}\n"
    "last_analyzed: 2025-01-01\n"
    "(this frontmatter block is never closed)\n"
    "## Outgoing Dependencies\n"
)


@pytest.fixture()
def seeded_anomaly_dir(depmap_enabled_client: TestClient) -> Iterator[Path]:
    """Seed hand-written deterministic anomaly files into the dep-map read path.

    Files are written into ``<cidx_meta_read_path>/dependency-map/`` — the exact
    directory the read handler (`depmap_get_cross_domain_graph`) resolves via
    ``app.state.dependency_map_service.cidx_meta_read_path``.  Teardown ALWAYS
    removes every seeded file so later tests + the session log-audit gate are
    unaffected.
    """
    dep_map_service = _dep_map_service(depmap_enabled_client)
    read_root = Path(dep_map_service.cidx_meta_read_path)
    out_dir = read_root / "dependency-map"
    out_dir.mkdir(parents=True, exist_ok=True)

    seeded = {
        out_dir / "_domains.json": _DOMAINS_JSON,
        out_dir / f"{_DOMAIN_SELF}.md": _ALPHA_MD,
        out_dir / f"{_DOMAIN_TARGET}.md": _BETA_MD,
        out_dir / f"{_DOMAIN_BROKEN}.md": _BROKEN_MD,
    }
    # Track only files that did NOT pre-exist so teardown never deletes real data.
    created: list[Path] = []
    try:
        for path, content in seeded.items():
            preexisting = path.exists()
            path.write_text(content, encoding="utf-8")
            if not preexisting:
                created.append(path)
        yield out_dir
    finally:
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _anomaly_messages(graph: dict, channel_key: str) -> list[str]:
    """Return the 'error' message strings for a given anomaly channel list."""
    return [
        str(item.get("error", ""))
        for item in graph.get(channel_key, [])
        if isinstance(item, dict)
    ]


def test_ac2_deterministic_anomalies_surface_and_classify(
    depmap_enabled_client: TestClient, auth_headers: dict, seeded_anomaly_dir: Path
) -> None:
    """Read the cross-domain graph and assert all three anomalies surface + classify.

    SELF_LOOP + GARBAGE_DOMAIN_REJECTED route to the DATA channel; MALFORMED_YAML
    routes to the PARSER channel.  Assertions are on the stable descriptive
    messages emitted by the parser (the read tool exposes {file, error} dicts).
    """
    resp = call_mcp_tool(depmap_enabled_client, MCP_GRAPH_TOOL, {}, auth_headers)
    assert resp.status_code == 200, (
        f"{MCP_GRAPH_TOOL} HTTP {resp.status_code}: {resp.text[:300]}"
    )
    graph = parse_mcp_result(resp.json())
    assert graph.get("success") is True, f"graph read failed: {graph}"

    all_msgs = _anomaly_messages(graph, "anomalies")
    parser_msgs = _anomaly_messages(graph, "parser_anomalies")
    data_msgs = _anomaly_messages(graph, "data_anomalies")

    joined_all = " || ".join(all_msgs)
    joined_parser = " || ".join(parser_msgs)
    joined_data = " || ".join(data_msgs)

    # --- SELF_LOOP (data channel) ---
    assert any("self-loop edge" in m for m in all_msgs), (
        f"SELF_LOOP anomaly not surfaced. anomalies={joined_all}"
    )
    assert any("self-loop edge" in m for m in data_msgs), (
        f"SELF_LOOP must be on the DATA channel. data_anomalies={joined_data}"
    )

    # --- GARBAGE_DOMAIN_REJECTED (data channel) ---
    assert any("prose-fragment target domain rejected" in m for m in all_msgs), (
        f"GARBAGE_DOMAIN_REJECTED anomaly not surfaced. anomalies={joined_all}"
    )
    assert any("prose-fragment target domain rejected" in m for m in data_msgs), (
        f"GARBAGE_DOMAIN_REJECTED must be on the DATA channel. "
        f"data_anomalies={joined_data}"
    )

    # --- MALFORMED_YAML (parser channel) ---
    assert any("never closed" in m for m in all_msgs), (
        f"MALFORMED_YAML anomaly not surfaced. anomalies={joined_all}"
    )
    assert any("never closed" in m for m in parser_msgs), (
        f"MALFORMED_YAML must be on the PARSER channel. "
        f"parser_anomalies={joined_parser}"
    )

    # The valid alpha->beta edge survives hygiene (proves the parser still builds
    # real edges alongside anomaly detection).
    edges = graph.get("edges", [])
    assert any(
        e.get("source_domain") == _DOMAIN_SELF
        and e.get("target_domain") == _DOMAIN_TARGET
        for e in edges
    ), f"expected the real {_DOMAIN_SELF}->{_DOMAIN_TARGET} edge to survive: {edges}"
