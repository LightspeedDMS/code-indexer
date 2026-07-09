"""Phase 6 E2E tests: PostgreSQL parity -- shared coordination & backends (Story #1137).

Epic #1121 / Story #1137 validates the PostgreSQL dimension of the Story #1083
production-backend work against a LIVE PG-backed uvicorn instance provisioned by
``e2e-automation.sh --phase 6`` (ephemeral PG cluster over a UNIX socket).

All OPERATIONS are driven through the real REST / MCP front door.  Direct
psycopg reads of the PG tables are used ONLY as EVIDENCE of backend state (the
equivalent of reading a file on disk to prove a write happened) -- never to
fabricate state.  The sole filesystem artifact written by the test is the
dep-map ``SharedJobSentinel`` lock, which is the EXACT coordination file a
second cluster node writes via ``O_CREAT|O_EXCL`` -- a real artifact, not a mock.

Acceptance criteria
-------------------
AC1 -- dep-map sentinel 409 under PG (DB-atomic single winner)
    The dep-map re-entrancy guard works under PostgreSQL.  The sentinel is
    filesystem-based (Story #1035), but the PG dimension is
    ``JobTracker.register_job_if_no_conflict`` backed by the partial unique
    index ``idx_active_job_per_repo`` (migration 004,
    ``background_jobs_backend.py``).  A real front-door trigger while a seeded
    sentinel is held returns the conflict (MCP ``success=False`` /
    "already in progress").  After release a fresh trigger is ACCEPTED and
    registers EXACTLY ONE active ``(dependency_map_*, server)`` row in the PG
    ``background_jobs`` table -- the DB-atomic single-winner property.

AC2 -- PG backend variants store / serve
  * ``QueryEmbeddingCachePostgresBackend`` stores/serves a query embedding,
    proven by a real front-door semantic search + direct inspection of the PG
    ``query_embedding_cache`` table.  The search is driven through the REST
    ``POST /api/query`` (``search_mode=semantic``) front door, which resolves the
    target repo from the caller's OWN activated-repo list -- NOT the group-based
    MCP access guard.  This deliberately side-steps the KNOWN PG DIVERGENCE
    (Story #1136): the PG ``groups`` table is never seeded with the default
    ``admins`` group, so ``is_admin_user('admin')`` is False and the MCP
    ``search_code`` tool raises access-denied BEFORE the query-embed path runs
    (empirically reproduced; that path is asserted/xfailed in
    ``test_01_pg_functional.py``).  The ``/api/query`` path runs the real
    query-embed pipeline, so a MISS UPSERTs the embedding rows (voyage-code-3 +
    cohere) into PG and an identical re-query is SERVED from PG (row count stable,
    ``last_used`` advanced, ``created_at`` unchanged).
  * Batched metrics writer persists AND drains ON SHUTDOWN
    (``upsert_buckets_batch`` on ``ApiMetricsPostgresBackend``).  A DEDICATED
    throwaway PG-backed uvicorn (same invocation as the harness, pointed at the
    SAME ephemeral PG cluster) is fed a burst of api-metric-producing front-door
    requests, then GRACEFULLY shut down (SIGTERM) so the lifespan ``stop_writer()``
    final-drain runs; the persisted buckets are then asserted via direct psycopg.
  * ``NodeMetricsPostgresBackend`` writes interval snapshots -- proven by PG rows.

Empirical resolution (manual-execute-first, 2026-06-16)
-------------------------------------------------------
Every assumption below was verified by driving a real PG-backed server manually:
  * PG DSN = ``postgresql:///{E2E_PG_DB_NAME}?host={E2E_PG_DATA}`` (UNIX socket).
  * Sentinel lock path = ``{server_dir}/data/golden-repos/cidx-meta/dependency-map/_active_analysis.lock``.
  * dep-map MCP trigger gates on ``dependency_map_enabled`` FIRST (enabled here
    through the real Web-UI config front door), THEN the sentinel.
  * Accepted dep-map worker registers ``operation_type='dependency_map_delta'``,
    ``repo_alias='server'`` -- the ``(operation_type, repo_alias)`` covered by
    ``idx_active_job_per_repo``.
  * A non-search MCP call (``list_groups``) records an ``other_api`` api-metrics
    bucket regardless of the #1136 divergence -- the drain-on-shutdown driver.
  * node_metrics first row appears within ~1s of startup (5s interval).
  * MCP ``search_code`` is access-denied under #1136, but REST ``POST /api/query``
    (``search_mode=semantic``) warms the cache: a MISS UPSERTs exactly two PG
    rows (voyage-ai/voyage-code-3 dim=1024 -> 4096-byte blob, cohere/embed-v4.0
    dim=1536 -> 6144-byte blob); an identical re-query keeps the row count stable
    and advances ``last_used`` while ``created_at`` is unchanged (served-from-PG).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pyotp
import pytest

from tests.e2e.helpers import (
    login,
    require_postgres,
    require_voyage_key,
    rest_call,
    wait_for_job,
    wait_for_repo_activation,
)

# ---------------------------------------------------------------------------
# Module skip-guard: PostgreSQL must be available (mirror test_01).
# ---------------------------------------------------------------------------


def setup_module(_: Any) -> None:
    """Loud-skip the whole module when PostgreSQL is absent."""
    require_postgres()


# ---------------------------------------------------------------------------
# Constants (no inline magic strings/ints in test bodies).
# ---------------------------------------------------------------------------

# dep-map MCP trigger
MCP_TRIGGER_TOOL = "trigger_dependency_analysis"
MCP_LIST_GROUPS_TOOL = "list_groups"

# REST semantic-query front door (resolves the repo from the caller's OWN
# activated-repo list, NOT the group-based MCP access guard -- so it is NOT
# blocked by the Story #1136 PG-groups divergence).  Used to GENUINELY warm the
# PG query_embedding_cache for the AC2a store/serve proof.
REST_QUERY_PATH = "/api/query"
QUERY_MODE_SEMANTIC = "semantic"

# Expected per-provider embedding blob sizes (float32 little-endian = 4 bytes
# per dimension).  A real query is embedded by BOTH providers when both keys are
# present, so a MISS upserts one row per provider.
_FLOAT32_BYTES = 4
_VOYAGE_DIM = 1024
_COHERE_DIM = 1536
_VOYAGE_BLOB_BYTES = _VOYAGE_DIM * _FLOAT32_BYTES  # 4096
_COHERE_BLOB_BYTES = _COHERE_DIM * _FLOAT32_BYTES  # 6144

# Sentinel coordination identity (Story #1035).
SENTINEL_OP_ANALYSIS = "analysis"
SEED_NODE_ID = "test-1137-other-node"

# dep-map background-job identity registered by the accepted worker.
DEPMAP_OP_DELTA = "dependency_map_delta"
DEPMAP_OP_FULL = "dependency_map_full"
DEPMAP_REPO_ALIAS = "server"
# Active job states covered by the idx_active_job_per_repo partial unique index.
_ACTIVE_JOB_STATES = ("pending", "running")
_TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

# Web-config front door for enabling dep-map (out-of-process flow).
WEB_LOGIN = "/login"
WEB_CONFIG = "/admin/config"
WEB_CONFIG_CLAUDE_CLI = "/admin/config/claude_cli"

# api-metrics bucket dimensions.
API_METRIC_OTHER = "other_api"
_GRANULARITIES = ("min1", "min5", "hour1", "day1")

# Bounded-loop budgets (monotonic-deadline -- Messi Rule #14).
_JOB_DRAIN_TIMEOUT_S = 45.0
_JOB_DRAIN_POLL_S = 0.5
_SENTINEL_CLEAR_TIMEOUT_S = 45.0
_NODE_METRICS_WAIT_S = 20.0
_SERVER_READY_TIMEOUT_S = 90.0
_SERVER_SHUTDOWN_TIMEOUT_S = 30.0

# Dedicated throwaway-server config for the drain-on-shutdown AC.
_THROWAWAY_PORT = int(os.environ.get("E2E_PG_THROWAWAY_PORT", "8912"))
_THROWAWAY_BURST = 12
_THROWAWAY_HOST = "127.0.0.1"

# CSRF hidden-input extractor for the web config flow.
_CSRF_RE = re.compile(
    r'<input[^>]*name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']'
    r"|"
    r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']'
)

# TOTP manual-entry-key scraper for /admin/mfa/setup (Bug #1324 elevation
# retry).  Mirrors tests/e2e/cli_remote/test_10_elevation_maintenance_1132.py's
# _MK_RE -- the established pattern for enrolling admin TOTP via the web
# session front door.
_MK_RE = re.compile(r"<div class='mk'>([^<]+)</div>")


# ---------------------------------------------------------------------------
# PG DSN / server-dir resolution (from harness env, with default fallback).
#
# e2e-automation.sh forwards E2E_PG_DATA / E2E_PG_DB_NAME / E2E_PG_SERVER_DATA_DIR
# to the Phase-6 pytest invocation; the defaults below match the harness defaults
# so a manual standalone run still resolves.
# ---------------------------------------------------------------------------


def _pg_data_dir() -> str:
    return os.environ.get(
        "E2E_PG_DATA", str(Path.home() / ".tmp" / "cidx-e2e-pg-cluster")
    )


def _pg_db_name() -> str:
    return os.environ.get("E2E_PG_DB_NAME", "cidx_e2e")


def _pg_server_data_dir() -> str:
    return os.environ.get(
        "E2E_PG_SERVER_DATA_DIR",
        str(Path.home() / ".tmp" / "cidx-e2e-pg-server-data"),
    )


def _pg_dsn() -> str:
    """UNIX-socket DSN to the ephemeral Phase-6 cluster (direct inspection only)."""
    return f"postgresql:///{_pg_db_name()}?host={_pg_data_dir()}"


def _pg_connect() -> Any:
    """Open a direct psycopg connection for EVIDENCE reads.

    Raises pytest.skip if psycopg or the cluster socket is unavailable (a
    standalone run without the harness cannot inspect PG).
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a server dep
        pytest.skip("psycopg not importable -- cannot inspect PG tables")
    sock_dir = Path(_pg_data_dir())
    if not sock_dir.exists():
        pytest.skip(
            f"PG cluster socket dir {sock_dir} absent -- run via "
            "e2e-automation.sh --phase 6"
        )
    try:
        return psycopg.connect(_pg_dsn(), autocommit=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot connect to Phase-6 PG cluster ({_pg_dsn()}): {exc}")


@pytest.fixture()
def pg_conn() -> Iterator[Any]:
    """Yield a direct psycopg connection; always closed in teardown."""
    conn = _pg_connect()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Front-door helpers (out-of-process httpx against the live PG server).
# ---------------------------------------------------------------------------


def _mcp_call(client: httpx.Client, token: str, name: str, arguments: dict) -> dict:
    """Invoke an MCP tool via /mcp JSON-RPC and return the parsed result dict.

    Returns either the decoded ``content[0].text`` JSON object, or a
    ``{"_jsonrpc_error": ...}`` envelope when the JSON-RPC layer itself errored
    (e.g. the #1136 access-denied path on search_code).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    resp = client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    assert resp.status_code == 200, (
        f"MCP {name} HTTP {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    if "error" in body:
        return {"_jsonrpc_error": body["error"]}
    result = body.get("result", {})
    content = result.get("content") if isinstance(result, dict) else None
    if content:
        text = content[0].get("text", "")
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"_raw_text": text}
        return parsed if isinstance(parsed, dict) else {"_parsed": parsed}
    return result if isinstance(result, dict) else {"_result": result}


def _fetch_csrf(client: httpx.Client, path: str) -> str:
    page = client.get(path, timeout=30.0)
    page.raise_for_status()
    match = _CSRF_RE.search(page.text)
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def _elevation_error_code(resp: httpx.Response) -> Any:
    """Return the ``detail.error`` code from a 403 response, or None.

    ``require_elevation()`` (src/code_indexer/server/auth/dependencies.py)
    raises HTTPException(403, detail={"error": "totp_setup_required" |
    "elevation_required", ...}); FastAPI serialises HTTPException as JSON
    regardless of the route's declared response_class.
    """
    if resp.status_code != 403:
        return None
    try:
        detail = resp.json().get("detail", {})
    except ValueError:
        return None
    return detail.get("error") if isinstance(detail, dict) else None


def _obtain_elevation(web: httpx.Client, admin_user: str) -> None:
    """Satisfy a TOTP step-up elevation gate on the current web session.

    Bug #1324: ``POST /admin/config/{section}`` is guarded by
    ``Depends(dependencies.require_elevation())``.  Reuses the exact
    enrol + elevate primitives from
    tests/e2e/cli_remote/test_10_elevation_maintenance_1132.py
    (``_enroll_admin_totp`` / ``web.post("/auth/elevate", ...)``): scrape the
    manual-entry key from ``/admin/mfa/setup``, verify it to enrol, then open
    an elevation window with the current TOTP code.  Bounded, no loops.
    """
    setup_page = web.get("/admin/mfa/setup")
    assert setup_page.status_code == 200, (
        f"/admin/mfa/setup expected 200, got {setup_page.status_code}: "
        f"{setup_page.text[:200]}"
    )
    mk_match = _MK_RE.search(setup_page.text)
    assert mk_match, f"No '.mk' manual-key div on setup page: {setup_page.text[:300]}"
    secret = mk_match.group(1).replace(" ", "").strip()

    verify = web.post(
        "/admin/mfa/verify",
        data={"totp_code": pyotp.TOTP(secret).now(), "target_user": admin_user},
    )
    assert verify.status_code == 200, (
        f"/admin/mfa/verify expected 200, got {verify.status_code}: {verify.text[:200]}"
    )

    elevate = web.post("/auth/elevate", json={"totp_code": pyotp.TOTP(secret).now()})
    assert elevate.status_code == 200, (
        f"/auth/elevate expected 200, got {elevate.status_code}: {elevate.text[:200]}"
    )


def _enable_dependency_map(server_url: str) -> None:
    """Enable ``dependency_map_enabled`` via the real Web-UI config front door.

    Uses a dedicated cookie-bearing httpx.Client (web /login session + CSRF),
    distinct from the JWT bearer used for MCP.  Bounded, no loops.

    Bug #1324: the config write is gated by TOTP step-up elevation
    (``Depends(dependencies.require_elevation())`` on
    ``POST /admin/config/{section}``).  When
    ``elevation_enforcement_enabled`` is False (the default -- see
    tests/e2e/cli_remote/test_03_admin_users.py / test_04_admin_groups.py),
    the gate is a passthrough and the first POST succeeds with zero side
    effects.  Only when the server actually rejects the write with 403
    ``totp_setup_required`` / ``elevation_required`` do we enrol TOTP and open
    an elevation window (``_obtain_elevation``), then retry the POST once --
    this avoids enrolling TOTP on the shared admin account (which would break
    the plain-password web/JSON logins other Phase 6 fixtures rely on) in the
    normal case where elevation isn't required at all.
    """
    admin_user = os.environ.get("E2E_ADMIN_USER", "")
    admin_pass = os.environ.get("E2E_ADMIN_PASS", "")
    if not admin_user or not admin_pass:
        pytest.skip("E2E_ADMIN_USER / E2E_ADMIN_PASS not set")

    with httpx.Client(base_url=server_url, follow_redirects=False, timeout=30.0) as web:
        login_csrf = _fetch_csrf(web, WEB_LOGIN)
        resp = web.post(
            WEB_LOGIN,
            data={
                "username": admin_user,
                "password": admin_pass,
                "csrf_token": login_csrf,
            },
        )
        assert resp.status_code in (302, 303), (
            f"web login expected redirect, got {resp.status_code}: {resp.text[:200]}"
        )
        assert "session" in web.cookies, "web login did not set a session cookie"

        form_csrf = _fetch_csrf(web, WEB_CONFIG) or login_csrf
        save = web.post(
            WEB_CONFIG_CLAUDE_CLI,
            data={"dependency_map_enabled": "true", "csrf_token": form_csrf},
        )

        error_code = _elevation_error_code(save)
        if error_code in ("totp_setup_required", "elevation_required"):
            _obtain_elevation(web, admin_user)
            retry_csrf = _fetch_csrf(web, WEB_CONFIG) or form_csrf
            save = web.post(
                WEB_CONFIG_CLAUDE_CLI,
                data={"dependency_map_enabled": "true", "csrf_token": retry_csrf},
            )

        assert save.status_code in (200, 302, 303), (
            f"enable dep-map POST failed: {save.status_code} -- {save.text[:200]}"
        )


def _sentinel():
    """Return a real SharedJobSentinel bound to the PG server's sentinel dir.

    The dir is resolved the SAME way production does:
    ``{server_dir}/data/golden-repos/cidx-meta/dependency-map`` (Story #1035 /
    DependencyMapService.get_sentinel_dir()).  The test process and the
    out-of-process server share this on-disk directory.
    """
    from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel
    from code_indexer.server.services.dependency_map_service import (
        ANALYSIS_STALE_TIMEOUT_SECONDS,
    )

    sentinel_dir = (
        Path(_pg_server_data_dir())
        / "data"
        / "golden-repos"
        / "cidx-meta"
        / "dependency-map"
    )
    return SharedJobSentinel(
        sentinel_dir=sentinel_dir,
        stale_timeout_seconds=ANALYSIS_STALE_TIMEOUT_SECONDS,
    )


def _wait_sentinel_clear(sentinel: Any) -> None:
    """Bounded-wait until no analysis sentinel is held (monotonic deadline)."""
    deadline = time.monotonic() + _SENTINEL_CLEAR_TIMEOUT_S
    while time.monotonic() < deadline:
        if sentinel.read_active(SENTINEL_OP_ANALYSIS) is None:
            return
        time.sleep(_JOB_DRAIN_POLL_S)


def _drain_job(client: httpx.Client, token: str, job_id: str) -> None:
    """Bounded-wait until a background job reaches a terminal state."""
    if not job_id:
        return
    deadline = time.monotonic() + _JOB_DRAIN_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = rest_call(client, "GET", f"/api/jobs/{job_id}", token=token)
        if resp.status_code == 404:
            return
        if (
            resp.status_code == 200
            and resp.json().get("status") in _TERMINAL_JOB_STATES
        ):
            return
        time.sleep(_JOB_DRAIN_POLL_S)


def _count_active_depmap_jobs(conn: Any) -> int:
    """Direct PG EVIDENCE: count active (pending/running) dep-map jobs.

    The active-state set is sourced from ``_ACTIVE_JOB_STATES`` (the same states
    covered by the ``idx_active_job_per_repo`` partial unique index) -- single
    source of truth, no inline literal.
    """
    placeholders = ", ".join(["%s"] * len(_ACTIVE_JOB_STATES))
    sql = (
        "SELECT count(*) FROM background_jobs "
        "WHERE operation_type IN (%s, %s) AND repo_alias = %s "
        f"AND status IN ({placeholders})"
    )
    params = (DEPMAP_OP_DELTA, DEPMAP_OP_FULL, DEPMAP_REPO_ALIAS, *_ACTIVE_JOB_STATES)
    cur = conn.execute(sql, params)
    return int(cur.fetchone()[0])


def _wait_for_depmap_job_row(conn: Any, job_id: str) -> tuple | None:
    """Bounded-poll PG background_jobs for the accepted dep-map job row.

    The accepted worker registers its background_jobs row ASYNCHRONOUSLY (after
    the trigger returns the job_id), so the row may not exist the instant the
    trigger responds.  Monotonic-deadline bound (Messi Rule #14): returns the
    ``(operation_type, repo_alias, status)`` tuple once present, else None.
    """
    deadline = time.monotonic() + _JOB_DRAIN_TIMEOUT_S
    while time.monotonic() < deadline:
        cur = conn.execute(
            "SELECT operation_type, repo_alias, status FROM background_jobs "
            "WHERE job_id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if row is not None:
            return tuple(row)
        time.sleep(_JOB_DRAIN_POLL_S)
    return None


# ===========================================================================
# AC1 -- dep-map sentinel 409 under PG + DB-atomic single winner
# ===========================================================================


@pytest.fixture(scope="module")
def depmap_enabled(pg_server_url: str) -> str:
    """Enable dep-map once for the module via the web config front door."""
    _enable_dependency_map(pg_server_url)
    return pg_server_url


def test_ac1_pg_dep_map_sentinel_conflict(
    depmap_enabled: str,
    pg_http_client: httpx.Client,
    pg_admin_token: str,
) -> None:
    """Seeded sentinel -> real front-door trigger reports 'already in progress'.

    The sentinel lock is the EXACT artifact a second cluster node writes; the
    live PG-backed server reads it via get_sentinel_dir() and returns the
    conflict.  Releasing the sentinel clears the active state.
    """
    sentinel = _sentinel()
    _wait_sentinel_clear(sentinel)
    assert sentinel.read_active(SENTINEL_OP_ANALYSIS) is None, (
        "an analysis sentinel is already held -- cannot establish a "
        "deterministic in-flight state for the 409 conflict"
    )

    seed_job_id = f"seed-1137-{int(time.time() * 1000)}"
    claim = sentinel.try_claim(SENTINEL_OP_ANALYSIS, seed_job_id, SEED_NODE_ID)
    assert claim.success, "pre-seed sentinel claim failed"
    try:
        result = _mcp_call(
            pg_http_client, pg_admin_token, MCP_TRIGGER_TOOL, {"mode": "delta"}
        )
        assert result.get("success") is False, (
            f"trigger should report conflict while sentinel held: {result}"
        )
        assert result.get("error") == "already in progress", (
            f"unexpected conflict error under PG: {result}"
        )
        assert result.get("job_id") == seed_job_id, (
            f"conflict must surface the seeded active job_id {seed_job_id!r}: {result}"
        )
    finally:
        sentinel.release(SENTINEL_OP_ANALYSIS, expected_job_id=seed_job_id)
        assert sentinel.read_active(SENTINEL_OP_ANALYSIS) is None, (
            "sentinel still held after owner release"
        )


def test_ac1_pg_release_then_accept_single_active_job(
    depmap_enabled: str,
    pg_http_client: httpx.Client,
    pg_admin_token: str,
    pg_conn: Any,
) -> None:
    """After release, a fresh trigger is ACCEPTED and registers EXACTLY ONE
    active dep-map row in the PG ``background_jobs`` table.

    This is the DB-atomic single-winner dimension: the accepted worker calls
    ``register_job_if_no_conflict(dependency_map_delta, repo_alias='server')``
    and the partial unique index ``idx_active_job_per_repo`` guarantees a single
    active ``(operation_type, repo_alias)`` row.
    """
    sentinel = _sentinel()
    _wait_sentinel_clear(sentinel)
    assert sentinel.read_active(SENTINEL_OP_ANALYSIS) is None, (
        "analysis sentinel did not clear -- cannot establish a clean accept state"
    )

    seed_job_id = f"seed-rel-1137-{int(time.time() * 1000)}"
    accepted_job_id = ""
    claim = sentinel.try_claim(SENTINEL_OP_ANALYSIS, seed_job_id, SEED_NODE_ID)
    assert claim.success, "pre-seed claim failed for release-then-accept"
    try:
        # Control: while held -> conflict (guard active).
        held = _mcp_call(
            pg_http_client, pg_admin_token, MCP_TRIGGER_TOOL, {"mode": "delta"}
        )
        assert held.get("success") is False, f"expected conflict while held: {held}"

        # Release the in-flight sentinel (owner-only delete).
        sentinel.release(SENTINEL_OP_ANALYSIS, expected_job_id=seed_job_id)
        assert sentinel.read_active(SENTINEL_OP_ANALYSIS) is None, (
            "sentinel still present after owner release"
        )

        # Fresh trigger must now be ACCEPTED with a new job_id.
        accepted = _mcp_call(
            pg_http_client, pg_admin_token, MCP_TRIGGER_TOOL, {"mode": "delta"}
        )
        assert accepted.get("success") is True, (
            f"trigger should be accepted after release: {accepted}"
        )
        accepted_job_id = accepted.get("job_id") or ""
        assert accepted_job_id and accepted_job_id != seed_job_id, (
            f"accepted trigger must mint a fresh job_id: {accepted}"
        )

        # The accepted job registers its background_jobs row ASYNCHRONOUSLY via
        # register_job_if_no_conflict(dependency_map_delta, repo_alias='server').
        # Bounded-poll for it to appear (proof it registered through the PG
        # JobTracker path guarded by idx_active_job_per_repo).
        row = _wait_for_depmap_job_row(pg_conn, accepted_job_id)
        assert row is not None, (
            f"accepted job {accepted_job_id!r} never appeared in PG "
            f"background_jobs within the wait budget"
        )
        assert row[0] == DEPMAP_OP_DELTA and row[1] == DEPMAP_REPO_ALIAS, (
            f"unexpected dep-map job identity in PG: {row}"
        )

        # DB-atomic single-winner EVIDENCE: at most ONE active dep-map row.
        # (The worker may already have completed -- 0 or 1 is correct; >1 would
        # mean the partial unique index failed to enforce single-active.)
        active = _count_active_depmap_jobs(pg_conn)
        assert active <= 1, (
            f"PG idx_active_job_per_repo must keep at most one active "
            f"(dependency_map_*, server) row; found {active}"
        )
    finally:
        # Release the seed only if it is still the active owner.
        active_info = sentinel.read_active(SENTINEL_OP_ANALYSIS)
        if active_info is not None and active_info.job_id == seed_job_id:
            sentinel.release(SENTINEL_OP_ANALYSIS, expected_job_id=seed_job_id)
        # Drain the accepted worker so the session log gate sees a settled state.
        _drain_job(pg_http_client, pg_admin_token, accepted_job_id)
        _wait_sentinel_clear(sentinel)


# ===========================================================================
# AC2a -- QueryEmbeddingCachePostgresBackend store / serve
# ===========================================================================


def _qec_rows(conn: Any) -> dict[tuple, tuple]:
    """Return the PG query_embedding_cache rows keyed by (cache_key, provider).

    Each value is ``(model, dimension, blob_bytes, last_used, created_at)`` so the
    serve-from-PG (touch-on-hit) assertion can compare last_used / created_at for a
    specific key across two reads.  Direct PG read = EVIDENCE only.
    """
    cur = conn.execute(
        "SELECT cache_key, provider, model, dimension, "
        "octet_length(embedding), last_used, created_at "
        "FROM query_embedding_cache"
    )
    return {(row[0], row[1]): (row[2], row[3], row[4], row[5], row[6]) for row in cur}


def _rest_semantic_query(
    client: httpx.Client, token: str, alias: str, query_text: str
) -> httpx.Response:
    """Drive the REST ``POST /api/query`` semantic front door.

    This endpoint resolves the repo from the caller's OWN activated-repo list
    (inline_query.semantic_query -> query_user_repositories), so it runs the real
    query-embed pipeline for an activated repo WITHOUT the group-based MCP access
    guard that the Story #1136 PG divergence breaks.
    """
    return rest_call(
        client,
        "POST",
        REST_QUERY_PATH,
        token=token,
        json={
            "query_text": query_text,
            "repository_alias": alias,
            "search_mode": QUERY_MODE_SEMANTIC,
            "limit": 3,
        },
        timeout=120.0,
    )


@pytest.fixture(scope="module")
def pg_cache_repo_1137(
    pg_http_client: httpx.Client,
    pg_admin_token: str,
) -> Iterator[str]:
    """Register + activate a DEDICATED golden repo for the AC2a cache test.

    Uses a unique alias ("pgcache1137") that is independent of the session-
    scoped "markupsafe" alias consumed and deleted by test_01_pg_functional.py.
    Module-scoped so the indexing cost is paid once; tears down after the
    module regardless of test outcome (best-effort, never raises).
    """
    require_voyage_key()
    seed_cache_dir = os.environ.get("E2E_SEED_CACHE_DIR", "")
    if not seed_cache_dir:
        pytest.skip("E2E_SEED_CACHE_DIR not set")

    import pathlib

    repo_path = str(pathlib.Path(seed_cache_dir) / "markupsafe")
    alias = "pgcache1137"

    # Register
    reg_resp = rest_call(
        pg_http_client,
        "POST",
        "/api/admin/golden-repos",
        token=pg_admin_token,
        json={"repo_url": repo_path, "alias": alias},
    )
    reg_resp.raise_for_status()
    job_id: str = reg_resp.json()["job_id"]
    job_timeout = float(os.environ.get("E2E_GOLDEN_REPO_JOB_TIMEOUT", "300.0"))
    job_status = wait_for_job(
        pg_http_client,
        job_id,
        token=pg_admin_token,
        timeout=job_timeout,
        poll_interval=2.0,
    )
    assert job_status["status"] == "completed", (
        f"pgcache1137 registration job did not complete: {job_status}"
    )

    # Activate
    act_resp = rest_call(
        pg_http_client,
        "POST",
        "/api/repos/activate",
        token=pg_admin_token,
        json={"golden_repo_alias": alias},
    )
    assert act_resp.status_code in (200, 202), (
        f"Activation of pgcache1137 returned unexpected status "
        f"{act_resp.status_code}: {act_resp.text[:300]}"
    )
    act_job_id: str | None = act_resp.json().get("job_id") or None
    if act_job_id:
        act_job_status = wait_for_job(
            pg_http_client,
            act_job_id,
            token=pg_admin_token,
            timeout=job_timeout,
            poll_interval=2.0,
        )
        assert act_job_status["status"] == "completed", (
            f"pgcache1137 activation job did not complete: {act_job_status}"
        )
    wait_for_repo_activation(
        pg_http_client,
        alias=alias,
        token=pg_admin_token,
        timeout=90.0,
    )

    yield alias

    # Teardown: best-effort deactivate + delete (never raises so test results are preserved)
    try:
        rest_call(
            pg_http_client,
            "DELETE",
            f"/api/repos/{alias}",
            token=pg_admin_token,
        )
    except Exception:
        pass
    try:
        rest_call(
            pg_http_client,
            "DELETE",
            f"/api/admin/golden-repos/{alias}",
            token=pg_admin_token,
        )
    except Exception:
        pass


def test_ac2_pg_query_embedding_cache_store_and_serve(
    pg_cache_repo_1137: str,
    pg_http_client: httpx.Client,
    pg_admin_token: str,
    pg_conn: Any,
) -> None:
    """A real REST semantic query STORES a query embedding in PG, then SERVES it.

    The front door (``POST /api/query``, ``search_mode=semantic``) drives the
    real query-embed pipeline against the admin's activated repo; PG inspection
    is EVIDENCE only.

    STORE proof: a UNIQUE query is a MISS, so the PG
    ``QueryEmbeddingCachePostgresBackend`` UPSERTs one row per active provider
    (voyage-code-3 dim=1024 -> 4096-byte float32-LE blob, cohere embed-v4.0
    dim=1536 -> 6144-byte blob).  SERVE proof: the IDENTICAL query is a HIT, so
    the row count does NOT grow and the served rows' ``last_used`` advances while
    ``created_at`` is unchanged (touch-on-hit, not a re-insert).

    NOTE on the #1136 PG divergence: the MCP ``search_code`` tool IS access-denied
    under PG (admin not in the unseeded ``admins`` group -- see
    test_01_pg_functional.py).  This test deliberately uses the activated-repo
    ``/api/query`` path, which is NOT group-gated, so the PG cache backend is
    exercised genuinely rather than xfailed.
    """
    require_voyage_key()
    alias = pg_cache_repo_1137

    # Unique query text so this is a guaranteed cache MISS regardless of any rows
    # left by sibling tests -- the STORE proof is the DELTA of new keys, and the
    # SERVE proof tracks those exact new keys.
    query_text = f"phase6 unique cache probe {int(time.time() * 1000)}"

    before = _qec_rows(pg_conn)

    resp1 = _rest_semantic_query(pg_http_client, pg_admin_token, alias, query_text)
    assert resp1.status_code == 200, (
        f"REST semantic query failed: HTTP {resp1.status_code}: {resp1.text[:300]}"
    )

    after_miss = _qec_rows(pg_conn)
    new_keys = set(after_miss) - set(before)
    assert new_keys, (
        "no NEW query_embedding_cache rows after a real REST semantic query -- the "
        "PG QueryEmbeddingCachePostgresBackend did not STORE the query embedding. "
        f"before={len(before)} after={len(after_miss)}"
    )

    # The MISS must have stored a non-empty float32-LE blob.  When the cohere key
    # is also configured (harness sets CO_API_KEY for dual-provider runs) both
    # providers are embedded -> two new rows of the expected per-provider sizes.
    new_sizes = {after_miss[k][2] for k in new_keys}
    assert all(size and size > 0 for size in new_sizes), (
        f"stored embedding blob is empty in PG for new keys: "
        f"{[(k, after_miss[k]) for k in new_keys]}"
    )
    # At least the voyage row must be present and exactly dim*4 bytes.
    assert _VOYAGE_BLOB_BYTES in new_sizes or _COHERE_BLOB_BYTES in new_sizes, (
        "stored blob sizes do not match the expected float32-LE per-provider "
        f"sizes (voyage={_VOYAGE_BLOB_BYTES}, cohere={_COHERE_BLOB_BYTES}); "
        f"got {sorted(new_sizes)}"
    )

    # SERVE proof: identical query -> HIT.  Row count for the new keys must NOT
    # grow, and last_used must advance while created_at stays put (touch-on-hit).
    resp2 = _rest_semantic_query(pg_http_client, pg_admin_token, alias, query_text)
    assert resp2.status_code == 200, (
        f"second identical REST query failed: HTTP {resp2.status_code}: "
        f"{resp2.text[:300]}"
    )

    after_hit = _qec_rows(pg_conn)
    # No NEW keys beyond the ones the MISS created (identical query == same keys).
    assert set(after_hit) - set(before) == new_keys, (
        "the identical query minted unexpected NEW cache keys -- it was NOT served "
        f"from PG. miss_new={new_keys} hit_new={set(after_hit) - set(before)}"
    )
    for key in new_keys:
        miss_model, miss_dim, miss_bytes, miss_last_used, miss_created = after_miss[key]
        hit_last_used, hit_created = after_hit[key][3], after_hit[key][4]
        assert hit_last_used >= miss_last_used, (
            f"served row {key} last_used did NOT advance on the cache HIT "
            f"({miss_last_used} -> {hit_last_used}) -- PG serve path not exercised"
        )
        assert hit_created == miss_created, (
            f"served row {key} created_at CHANGED on the HIT "
            f"({miss_created} -> {hit_created}) -- the row was re-inserted, not "
            "served from PG"
        )


# ===========================================================================
# AC2b -- node_metrics PG rows present
# ===========================================================================


def test_ac2_pg_node_metrics_rows_present(
    pg_http_client: httpx.Client,
    pg_conn: Any,
) -> None:
    """NodeMetricsPostgresBackend writes interval snapshots to PG node_metrics.

    The writer runs once immediately at startup and every 5s thereafter; this
    bounded-waits for at least one row to land, then proves the snapshot shape.
    """
    deadline = time.monotonic() + _NODE_METRICS_WAIT_S
    rows: list[tuple] = []
    while time.monotonic() < deadline:
        cur = pg_conn.execute(
            "SELECT node_id, node_ip, server_version, timestamp "
            "FROM node_metrics ORDER BY timestamp DESC LIMIT 5"
        )
        rows = list(cur.fetchall())
        if rows:
            break
        time.sleep(_JOB_DRAIN_POLL_S)

    assert rows, (
        "no node_metrics rows in PG within the wait budget -- the "
        "NodeMetricsPostgresBackend interval writer did not persist a snapshot"
    )
    node_id, node_ip, server_version, _ts = rows[0]
    assert node_id, f"node_metrics row missing node_id: {rows[0]}"
    assert node_ip, f"node_metrics row missing node_ip: {rows[0]}"
    assert server_version, f"node_metrics row missing server_version: {rows[0]}"


# ===========================================================================
# AC2c -- batched metrics writer drains ON SHUTDOWN (dedicated throwaway server)
# ===========================================================================


def _throwaway_server_data_dir() -> Path:
    return Path.home() / ".tmp" / "cidx-e2e-pg-throwaway-1137"


def _write_throwaway_config(data_dir: Path, port: int) -> None:
    """Write a bootstrap config.json for the throwaway server.

    Points at the SAME ephemeral PG cluster as the shared session server so the
    drained buckets land in the same inspectable PG database.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "server_dir": str(data_dir),
        "host": _THROWAWAY_HOST,
        "port": port,
        "storage_mode": "postgres",
        "postgres_dsn": _pg_dsn(),
    }
    (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _wait_throwaway_ready(url: str, admin_user: str, admin_pass: str) -> str:
    """Bounded-wait for the throwaway server: /health non-5xx AND a JWT login.

    Returns the JWT token.  Monotonic-deadline bound (Messi Rule #14).
    """
    deadline = time.monotonic() + _SERVER_READY_TIMEOUT_S
    last = ""
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=url, timeout=5.0) as c:
                health = c.get("/health")
                if health.status_code < 500:
                    token = login(
                        base_url=url, username=admin_user, password=admin_pass
                    )
                    if token:
                        return token
        except Exception as exc:  # server not up yet
            last = str(exc)
        time.sleep(0.5)
    raise AssertionError(
        f"throwaway PG server did not become ready within "
        f"{_SERVER_READY_TIMEOUT_S}s (last: {last})"
    )


def _terminate_and_reap(proc: subprocess.Popen) -> tuple[int, bool]:
    """Gracefully SIGTERM the server and bounded-wait; always reaps.

    SIGTERM (NOT SIGKILL) so the lifespan stop_writer() final-drain runs.
    uvicorn handles SIGTERM by running the lifespan shutdown to completion, then
    the process exits with ``-SIGTERM`` (signal-terminated) -- a GRACEFUL exit
    (empirically verified: the drain runs and "Application shutdown complete."
    is logged).  Escalates to SIGKILL ONLY if the graceful window is exceeded.

    Returns ``(exit_code, escalated_to_kill)``.  ``escalated_to_kill is False``
    means the server shut down gracefully within the window.
    """
    if proc.poll() is not None:
        return int(proc.returncode), False
    proc.send_signal(signal.SIGTERM)
    try:
        return int(proc.wait(timeout=_SERVER_SHUTDOWN_TIMEOUT_S)), False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            return int(proc.wait(timeout=10.0)), True
        except subprocess.TimeoutExpired:  # pragma: no cover
            return -1, True


def test_ac2_pg_api_metrics_drain_on_shutdown(
    pg_conn: Any,
) -> None:
    """Batched metrics writer persists AND drains on GRACEFUL shutdown to PG.

    Spins up a DEDICATED throwaway PG-backed uvicorn (same invocation as the
    harness, SAME ephemeral cluster, OWN data dir + port), feeds it a burst of
    api-metric-producing front-door MCP requests, then SIGTERMs it.  The
    lifespan ``stop_writer()`` final-drain flushes the queued buckets via
    ``ApiMetricsPostgresBackend.upsert_buckets_batch``; the persisted rows are
    then proven via direct psycopg.

    The shared session server is untouched (separate process/port/data dir).
    """
    admin_user = os.environ.get("E2E_ADMIN_USER", "")
    admin_pass = os.environ.get("E2E_ADMIN_PASS", "")
    if not admin_user or not admin_pass:
        pytest.skip("E2E_ADMIN_USER / E2E_ADMIN_PASS not set")

    repo_root = Path(__file__).resolve().parents[3]
    src_dir = repo_root / "src"
    if not (src_dir / "code_indexer").is_dir():
        pytest.skip(f"cannot locate src/ at {src_dir} -- not a source checkout")

    data_dir = _throwaway_server_data_dir()
    # Clean any leftover data dir from a previous run.
    if data_dir.exists():
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)
    _write_throwaway_config(data_dir, _THROWAWAY_PORT)

    url = f"http://{_THROWAWAY_HOST}:{_THROWAWAY_PORT}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_dir)
    env["CIDX_SERVER_DATA_DIR"] = str(data_dir)
    voyage = os.environ.get("E2E_VOYAGE_API_KEY") or os.environ.get(
        "VOYAGE_API_KEY", ""
    )
    if voyage:
        env["VOYAGE_API_KEY"] = voyage

    log_path = data_dir / "server.log"
    log_file = open(log_path, "wb")  # noqa: SIM115 - closed in finally
    proc = subprocess.Popen(
        [
            "python3",
            "-m",
            "uvicorn",
            "code_indexer.server.app:app",
            "--host",
            _THROWAWAY_HOST,
            "--port",
            str(_THROWAWAY_PORT),
            # INFO so uvicorn's "Application shutdown complete." lifespan marker
            # (the graceful-drain signal asserted below) is captured in the log.
            "--log-level",
            "info",
            "--workers",
            "1",
        ],
        cwd=str(repo_root),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    distinct_username = admin_user
    try:
        token = _wait_throwaway_ready(url, admin_user, admin_pass)

        # Sanity: a front-door MCP call works on the throwaway server.
        with httpx.Client(base_url=url, timeout=30.0) as client:
            who = _mcp_call(client, token, MCP_LIST_GROUPS_TOOL, {})
            assert not who.get("_jsonrpc_error"), (
                f"list_groups failed on throwaway server: {who}"
            )

        # Clear any pre-existing buckets for this username so the assertion is
        # isolated to THIS test's burst.
        pg_conn.execute(
            "DELETE FROM api_metrics_buckets WHERE username = %s",
            (distinct_username,),
        )

        # Burst of api-metric-producing front-door calls (other_api).
        with httpx.Client(base_url=url, timeout=30.0) as client:
            for _ in range(_THROWAWAY_BURST):
                _mcp_call(client, token, MCP_LIST_GROUPS_TOOL, {})

        # The buckets MAY already be drained by the 1s writer loop; the
        # authoritative assertion is AFTER the graceful shutdown final-drain.
    finally:
        exit_code, escalated_to_kill = _terminate_and_reap(proc)
        log_file.close()

    # A graceful SIGTERM shutdown exits with -SIGTERM (signal-terminated), NOT 0;
    # uvicorn still runs the lifespan shutdown (incl. stop_writer() final-drain)
    # to completion.  The failure mode we guard against is a FORCED kill (the
    # graceful window was exceeded -> the drain may not have run).
    assert not escalated_to_kill, (
        f"throwaway server had to be SIGKILLed (exit {exit_code}); the graceful "
        f"SIGTERM shutdown / stop_writer() final-drain did not finish in time. "
        f"Log: {log_path}"
    )
    # The lifespan shutdown ran to completion (uvicorn's marker).  This is the
    # log signal that the final-drain code path executed.
    shutdown_log = log_path.read_text(encoding="utf-8", errors="ignore")
    assert "Application shutdown complete." in shutdown_log, (
        "uvicorn did not log a clean lifespan shutdown -- the stop_writer() "
        f"final-drain may not have run. Log: {log_path}"
    )

    # AFTER graceful shutdown: the final-drain must have persisted the buckets.
    cur = pg_conn.execute(
        "SELECT granularity, count FROM api_metrics_buckets "
        "WHERE username = %s AND metric_type = %s",
        (distinct_username, API_METRIC_OTHER),
    )
    drained = {row[0]: int(row[1]) for row in cur.fetchall()}
    assert drained, (
        "no api_metrics_buckets rows for the burst after a graceful shutdown -- "
        f"upsert_buckets_batch / stop_writer() final-drain did not persist to PG. "
        f"Log: {log_path}"
    )
    # Each granularity bucket must have accumulated at least the burst count
    # (the DELETE ran before the burst, so the floor is the burst size).
    for gran in _GRANULARITIES:
        assert gran in drained, (
            f"missing {gran} bucket after drain -- buckets={drained}"
        )
        assert drained[gran] >= _THROWAWAY_BURST, (
            f"{gran} bucket count {drained[gran]} < burst {_THROWAWAY_BURST} -- "
            f"the shutdown drain lost events. buckets={drained}"
        )

    # Cleanup: remove this test's buckets so the shared PG db stays tidy.
    pg_conn.execute(
        "DELETE FROM api_metrics_buckets WHERE username = %s",
        (distinct_username,),
    )
    # Best-effort throwaway data-dir removal.
    try:
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)
    except Exception:
        pass
