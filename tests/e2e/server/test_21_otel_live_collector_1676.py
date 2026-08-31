"""Phase 3 E2E -- Story #1676 AC8: real live-collector round-trip verification.

Proves -- not via an in-process `InMemoryMetricReader` (that is what
test_20_telemetry_metrics_wiring_1586.py already covers) -- that CIDX
Server's real OTLP export actually round-trips over the wire to a real,
pinned-version OTEL Collector, and from there to Jaeger (traces) and
Prometheus (metrics), using EACH backend's own real query surface.

Stack: ``tests/otel-collector/docker-compose.yml`` (OTEL Collector, Jaeger,
Prometheus -- every image pinned to an exact tag + digest, no ``latest``).
Brought up here via ``docker compose ... up -d --wait`` (blocks on the
stack's real Docker health checks, never a blind sleep) and torn down via
``docker compose ... down -v`` in a fixture ``finally`` block so containers
AND volumes are removed on both success and failure.

Server under test: a FRESH, ISOLATED CIDX server instance (own data dir,
own in-process FastAPI TestClient) -- deliberately NOT the shared session
``test_client`` fixture every other Phase 3 test file uses. Telemetry's
``collector_endpoint`` is one of the ``restart_required_fields`` (Story
#1676 AC3/AC4): it is read once at ``TelemetryManager`` construction during
``create_app()``'s lifespan startup, so pointing it at this test's live
collector requires a fresh app built with that config already in place --
mutating the shared session server's config afterwards would not take
effect without a restart, and constructing a SECOND app against the SAME
data dir as the shared session server risks the exact shared-SQLite-
connection hazard documented in test_20's
``TestLifespanRealStartupWiring`` docstring. This test's isolated data dir
sidesteps both problems.

Per-signal verification methodology (Story #1676 AC8's own required
methodology -- the collector's raw OTLP ingestion ports :4317/:4318 and
/v1/{traces,metrics,logs} are NOT readable/queryable and are never treated
as such below):
  * TRACE  -> Jaeger's real HTTP query API (``GET /api/traces?service=...``).
  * METRIC -> a real Prometheus query (``GET /api/v1/query?query=...``).
  * LOG    -> there is no log-query UI anywhere in this stack. Log delivery
    is corroborated by (a) the collector's own self-metrics acceptance
    counter (``otelcol_receiver_accepted_log_records`` on its :8888
    self-metrics port -- proves ACCEPTANCE) and (b) a captured
    ``debug`` exporter log line from the collector's own container logs
    (a real ``LogRecord #`` block -- proves CONTENT actually arrived), per
    the story's own documented acceptable methodology for this one signal.

Docker availability: checked once at module import via ``_docker_available()``
and applied as a ``pytestmark`` skip -- this test SKIPS cleanly (not fails)
when Docker/`docker compose` is unavailable, with an unambiguous message.
It is wired into ``e2e-automation.sh`` as an explicitly Docker-dependent
sub-check of Phase 3 (see that script's "Phase 3 OTEL live-collector
sub-check" block) so it runs for real wherever Docker is guaranteed
present, rather than silently skipping everywhere forever.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.e2e.helpers import require_voyage_key
from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.server.mcp_helpers import call_mcp_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Docker / compose-stack constants
# ---------------------------------------------------------------------------
_COMPOSE_FILE: Path = (
    Path(__file__).parent.parent.parent / "otel-collector" / "docker-compose.yml"
)
_COMPOSE_PROJECT: str = "cidx-e2e-otel-1676"
_DOCKER_PROBE_TIMEOUT_S: int = 10
_COMPOSE_UP_WAIT_TIMEOUT_S: str = "120"
_COMPOSE_SUBPROCESS_TIMEOUT_S: int = 180

_JAEGER_QUERY_URL: str = "http://localhost:16686"
_PROMETHEUS_URL: str = "http://localhost:9090"
_COLLECTOR_HEALTH_URL: str = "http://localhost:13133/"
_COLLECTOR_SELF_METRICS_URL: str = "http://localhost:8888/metrics"
_OTLP_GRPC_ENDPOINT: str = "http://localhost:4317"

_COLLECTOR_HEALTH_TIMEOUT_S: float = 60.0
_COLLECTOR_HEALTH_POLL_S: float = 1.0

# ---------------------------------------------------------------------------
# Fresh isolated server / golden-repo constants
# ---------------------------------------------------------------------------
# seed_initial_admin() (user_manager.py) auto-creates this exact admin/admin
# account whenever a data dir's user store is empty -- this is a brand-new,
# throwaway, isolated data dir created fresh by this test module, never the
# shared dev/staging server the CLAUDE.md "admin password sacred" rule
# protects.
_ADMIN_USERNAME: str = "admin"
_ADMIN_PASSWORD: str = "admin"  # noqa: S105 -- fresh throwaway data dir only

_ALIAS: str = "otel1676probe"
_REGEX_PATTERN_ANY_FUNCTION: str = "def .+"
_REGEX_SEARCH_LIMIT: int = 5
_HTTP_OK: int = 200

_JOB_TIMEOUT_S: float = float(os.environ.get("E2E_GOLDEN_JOB_TIMEOUT", "300"))
_JOB_POLL_S: float = 0.5
_TERMINAL_JOB_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

_TELEMETRY_METRICS_EXPORT_INTERVAL_S: int = 3
_FORCE_FLUSH_TIMEOUT_MS: int = 30_000

_SIGNAL_POLL_TIMEOUT_S: float = 30.0
_SIGNAL_POLL_INTERVAL_S: float = 1.0

# Guards the config_service.py singleton swap in telemetry_app_client()
# below. Production's own get_config_service() has no dedicated lock for
# its "first call wins" check, so this test-only lock only synchronizes
# THIS fixture's read-modify-write swap against a concurrent thread (e.g.
# a shared session's background job worker) also calling
# get_config_service() during this fixture's isolated-app construction
# window -- it cannot make production's own getter atomic.
_CONFIG_SERVICE_SWAP_LOCK: threading.Lock = threading.Lock()


def _docker_available() -> bool:
    """Return True iff both ``docker`` and ``docker compose`` are usable."""
    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=_DOCKER_PROBE_TIMEOUT_S,
            check=True,
        )
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=_DOCKER_PROBE_TIMEOUT_S,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason=(
        "Docker (or `docker compose`) is not available in this environment. "
        "Story #1676 AC8's live-collector round trip requires a real "
        "docker-compose stack (OTEL Collector + Jaeger + Prometheus) and "
        "deliberately does NOT mock/fake the collector. Run this test in an "
        "environment where Docker is guaranteed present -- see "
        "e2e-automation.sh's Phase 3 OTEL live-collector sub-check."
    ),
)


def _compose(*args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(_COMPOSE_FILE),
            "-p",
            _COMPOSE_PROJECT,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=_COMPOSE_SUBPROCESS_TIMEOUT_S,
    )


def _poll_until(
    predicate_fn: Callable[[], bool],
    timeout_s: float,
    interval_s: float,
    description: str,
) -> None:
    """Bounded poll loop (Messi Rule #14 -- provable termination).

    Calls ``predicate_fn()`` until it returns truthy or the deadline is
    reached; raises AssertionError with ``description`` on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate_fn():
            return
        time.sleep(interval_s)
    raise AssertionError(f"Timed out after {timeout_s}s waiting for: {description}")


def _wait_for_collector_health() -> None:
    """Bounded host-side poll of the collector's health_check extension.

    The otel-collector service intentionally carries NO Docker-level
    `healthcheck:` (the image is distroless -- no shell/wget/curl at all,
    see docker-compose.yml's comment on that service) so `docker compose
    up --wait` cannot observe its readiness. This poll is the real,
    deterministic substitute: a bounded loop against the collector's own
    real health_check extension endpoint, never a blind sleep.
    """

    def _healthy() -> bool:
        try:
            resp = httpx.get(_COLLECTOR_HEALTH_URL, timeout=3.0)
            return resp.status_code == _HTTP_OK
        except httpx.HTTPError:
            return False

    _poll_until(
        _healthy,
        _COLLECTOR_HEALTH_TIMEOUT_S,
        _COLLECTOR_HEALTH_POLL_S,
        f"otel-collector health_check extension at {_COLLECTOR_HEALTH_URL}",
    )


def _wait_for_job(client: TestClient, job_id: str, headers: dict, label: str) -> None:
    """Poll GET /api/jobs/{job_id} until terminal state; fail loudly on timeout/failure."""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert resp.status_code < 500, (
            f"{label}: job poll returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == _HTTP_OK:
            body = resp.json()
            status = body.get("status")
            if status in _TERMINAL_JOB_STATES:
                assert status == "completed", (
                    f"{label}: job {job_id!r} ended with status {status!r}: {body}"
                )
                return
        time.sleep(_JOB_POLL_S)
    raise TimeoutError(
        f"{label}: job {job_id!r} did not complete within {_JOB_TIMEOUT_S}s"
    )


def _create_tiny_local_repo(base_dir: Path) -> Path:
    """Create a minimal, self-contained local git repo for golden-repo registration.

    Deliberately NOT the shared E2E_SEED_CACHE_DIR seed repos (markupsafe,
    mock-test-repo, ...) -- this test must be runnable standalone in any
    Docker-present environment without depending on e2e-automation.sh's
    seed-repo cloning step.
    """
    repo_dir = base_dir / "otel_1676_probe_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "sample.py").write_text(
        "def probe_function():\n"
        '    """Sample function for Story #1676 AC8 regex_search probe."""\n'
        "    return True\n"
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "otel-probe@example.invalid"],
        ["git", "config", "user.name", "OTEL Probe"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "initial commit"],
    ):
        subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True)
    return repo_dir


def _extract_counter_value(prometheus_text: str, metric_name: str) -> float:
    """Sum all sample values for ``metric_name`` from a raw Prometheus
    text-exposition-format scrape (the collector's own :8888 self-metrics
    endpoint). Returns 0.0 when the metric has not been emitted yet.

    Matches the metric name EXACTLY (up to the first ``{`` label-block
    delimiter, or the first whitespace for a label-less sample) rather than
    via ``str.startswith`` -- a prefix match would also incorrectly match an
    unrelated metric sharing the same prefix (e.g. a hypothetical
    ``otelcol_receiver_accepted_log_records_created`` line). Whitespace
    splitting uses ``split(None, ...)`` (generic whitespace, matching the
    Prometheus text-exposition-format spec) rather than a literal ``" "``.
    """
    total = 0.0
    for line in prometheus_text.splitlines():
        if line.startswith("#"):
            continue
        sample_name = line.split("{", 1)[0].split(None, 1)[0]
        if sample_name != metric_name:
            continue
        _, _, value_part = line.rpartition(" ")
        try:
            total += float(value_part)
        except ValueError:
            continue
    return total


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def otel_stack() -> Iterator[None]:
    """Bring up the pinned OTEL Collector + Jaeger + Prometheus stack.

    Waits on the stack's real Docker health checks via `--wait` (Jaeger,
    Prometheus) plus a host-side poll of the collector's own health
    endpoint (see _wait_for_collector_health). Tears the stack down --
    containers AND volumes -- unconditionally in `finally`, on both the
    success and failure path (Story #1676 AC8).
    """
    # Defensive self-heal: remove any orphaned stack left by a prior crashed
    # run under the SAME project name before starting a fresh one.
    _compose("down", "-v")

    try:
        # Deliberately inside `try` (not above it): a `subprocess.TimeoutExpired`
        # from this call must still reach `finally`'s cleanup below, rather
        # than escaping before the stack's teardown logic exists to run.
        up_result = _compose(
            "up", "-d", "--wait", "--wait-timeout", _COMPOSE_UP_WAIT_TIMEOUT_S
        )
        assert up_result.returncode == 0, (
            f"docker compose up --wait failed (rc={up_result.returncode}):\n"
            f"stdout={up_result.stdout}\nstderr={up_result.stderr}"
        )
        _wait_for_collector_health()
        yield
    finally:
        down_result = _compose("down", "-v")
        if down_result.returncode != 0:
            logger.error(
                "docker compose down -v FAILED (rc=%s) -- potential orphaned "
                "containers/volumes for project %r:\nstdout=%s\nstderr=%s",
                down_result.returncode,
                _COMPOSE_PROJECT,
                down_result.stdout,
                down_result.stderr,
            )
            raise AssertionError(
                f"docker compose down -v failed (rc={down_result.returncode}) "
                f"-- see logs above for orphaned container/volume risk"
            )


@pytest.fixture(scope="module")
def telemetry_app_client(
    otel_stack: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TestClient, AdminTokenProvider, FastAPI]]:
    """Fresh, ISOLATED CIDX server app pointed at the live collector stack.

    See module docstring for why this cannot reuse the shared session
    `test_client` fixture. `export_logs`/`export_traces`/`export_metrics`
    are all enabled (Story #1676 AC1-AC7's now-complete config surface).
    """
    require_voyage_key()

    data_dir = tmp_path_factory.mktemp("otel_1676_data_dir")
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "telemetry_config": {
                    "enabled": True,
                    "collector_endpoint": _OTLP_GRPC_ENDPOINT,
                    "collector_protocol": "grpc",
                    "service_name": "cidx-server",
                    "export_traces": True,
                    "export_metrics": True,
                    "export_logs": True,
                    "machine_metrics_interval_seconds": _TELEMETRY_METRICS_EXPORT_INTERVAL_S,
                }
            }
        )
    )

    previous_data_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
    os.environ["CIDX_SERVER_DATA_DIR"] = str(data_dir)

    # get_config_service()/get_telemetry_manager() are BOTH "first call
    # wins" process-wide singletons (each constructor arg is documented as
    # "required on first call, optional on subsequent calls" -- see
    # config_service.py's get_config_service() and
    # telemetry/manager.py's get_telemetry_manager()). If either was
    # already constructed earlier in this process (e.g. the shared Phase 3
    # session `test_client` fixture, or an earlier test module in the same
    # pytest run), create_app() below would silently reuse that STALE
    # instance -- bound to a different data dir/config -- instead of
    # genuinely picking up THIS isolated data dir's telemetry_config.
    # Empirically confirmed: this exact isolated-app technique works
    # correctly in a bare process with no prior singleton construction;
    # running it inside this test suite's session (whose conftest.py
    # lazily triggers these singletons before this fixture runs) silently
    # left app.state.telemetry_manager as None until this reset was added.
    # Mirrors the established
    # tests/unit/server/telemetry/otel_test_support.py LIFO save/restore
    # pattern (peek/replace the private module global directly, restore in
    # `finally` below) so any OTHER test's already-installed singleton is
    # put back exactly as found, never left cleared for a later test.
    #
    # Two MORE process-wide "first call wins" singletons touched by this
    # isolated app's construction/use also need the same save/None/restore
    # treatment (Story #1676 AC8 round-2 review finding): `ApplicationMetrics`
    # (metrics_instrumentation.py) and `JobMetrics` (job_metrics.py). Leaving
    # either bound to THIS fixture's now-shut-down MeterProvider causes a
    # later test in the same pytest session (e.g.
    # test_20_telemetry_metrics_wiring_1586.py) to silently observe zero data
    # points on its own freshly-installed InMemoryMetricReader -- reproduced
    # empirically: running this module before test_20 in the same session
    # fails test_20's AC1 assertion, while running test_20 first (or this
    # module alone) passes.
    import code_indexer.server.telemetry.job_metrics as _job_metrics_module
    import code_indexer.server.telemetry.manager as _telemetry_manager_module
    import code_indexer.server.telemetry.metrics_instrumentation as _app_metrics_module
    import code_indexer.server.services.config_service as _config_service_module

    with _telemetry_manager_module._manager_lock:
        previous_telemetry_manager = _telemetry_manager_module._telemetry_manager
        _telemetry_manager_module._telemetry_manager = None
    with _CONFIG_SERVICE_SWAP_LOCK:
        previous_config_service = _config_service_module._config_service
        _config_service_module._config_service = None
    previous_application_metrics = _app_metrics_module._application_metrics
    _app_metrics_module._application_metrics = None
    previous_job_metrics = _job_metrics_module._job_metrics
    _job_metrics_module._job_metrics = None

    # `code_indexer.server.app`'s module-level `app` attribute is a THIRD
    # "first call wins" mechanism, but of a different shape: it is PEP 562
    # lazy-`__getattr__`-backed (Bug #1638), so a bound `None` behaves
    # differently from the key being genuinely ABSENT from `__dict__`.
    # `__dict__.get(...)` (never a plain `getattr`) is used deliberately so
    # reading the previous value can never itself trigger/resurrect a lazy
    # construction when the key was absent before this fixture ran.
    import code_indexer.server.app as _app_module

    _app_module_had_app = "app" in _app_module.__dict__
    previous_app_module_app = _app_module.__dict__.get("app")

    try:
        from code_indexer.server.app import create_app

        fresh_app = create_app()
        _app_module.app = fresh_app
        with TestClient(fresh_app, raise_server_exceptions=False) as client:
            login_resp = client.post(
                "/auth/login",
                json={"username": _ADMIN_USERNAME, "password": _ADMIN_PASSWORD},
            )
            assert login_resp.status_code == _HTTP_OK, (
                f"telemetry_app_client: initial admin login failed: "
                f"{login_resp.status_code} {login_resp.text[:300]}"
            )
            login_body = login_resp.json()

            def _relogin() -> "tuple[str, str | None]":
                resp = client.post(
                    "/auth/login",
                    json={"username": _ADMIN_USERNAME, "password": _ADMIN_PASSWORD},
                )
                assert resp.status_code == _HTTP_OK, (
                    f"telemetry_app_client: re-login failed: "
                    f"{resp.status_code} {resp.text[:300]}"
                )
                body = resp.json()
                return str(body["access_token"]), body.get("refresh_token")

            token_provider = AdminTokenProvider(
                login_fn=_relogin,
                initial_access_token=login_body["access_token"],
                initial_refresh_token=login_body.get("refresh_token"),
            )

            assert fresh_app.state.telemetry_manager is not None, (
                "telemetry_app_client: lifespan did not construct a "
                "TelemetryManager despite telemetry_config.enabled=true"
            )

            yield client, token_provider, fresh_app
    finally:
        # Shut down THIS fixture's own manager (TestClient.__exit__ above
        # already triggered lifespan shutdown -> telemetry_manager.shutdown()
        # on the instance, but that does not clear the global singleton --
        # only reset_telemetry_manager()/manual restore below does) and put
        # ALL FIVE singletons back exactly as found, so a shared-session test
        # running later never sees an unexpectedly-cleared config/telemetry/
        # metrics singleton, or an app-module `app` bound to this fixture's
        # own throwaway isolated app.
        with _telemetry_manager_module._manager_lock:
            _telemetry_manager_module._telemetry_manager = previous_telemetry_manager
        _config_service_module._config_service = previous_config_service
        _app_metrics_module._application_metrics = previous_application_metrics
        _job_metrics_module._job_metrics = previous_job_metrics

        # `app` is PEP 562 lazy (Bug #1638): a bound `None` and an ABSENT
        # key are semantically different for `__getattr__`, so restoring
        # "no prior value" must DELETE the key, never set it to `None`.
        if _app_module_had_app:
            _app_module.app = previous_app_module_app
        else:
            _app_module.__dict__.pop("app", None)

        if previous_data_dir is None:
            os.environ.pop("CIDX_SERVER_DATA_DIR", None)
        else:
            os.environ["CIDX_SERVER_DATA_DIR"] = previous_data_dir


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_real_mcp_call_round_trips_span_metric_and_log_through_live_collector(
    telemetry_app_client: "tuple[TestClient, AdminTokenProvider, FastAPI]",
    tmp_path: Path,
) -> None:
    client, token_provider, fresh_app = telemetry_app_client
    headers = token_provider.get_headers()

    # Snapshot the collector's log-records acceptance counter BEFORE the
    # front-door call, so the comparison below is unambiguous even though
    # the counter is cumulative for the container's lifetime.
    before_metrics = httpx.get(_COLLECTOR_SELF_METRICS_URL, timeout=5.0).text
    log_records_before = _extract_counter_value(
        before_metrics, "otelcol_receiver_accepted_log_records"
    )

    # Register + activate a tiny, self-contained local repo (no dependency
    # on the shared E2E_SEED_CACHE_DIR).
    repo_path = _create_tiny_local_repo(tmp_path)

    reg_resp = client.post(
        "/api/admin/golden-repos",
        json={"repo_url": str(repo_path), "alias": _ALIAS},
        headers=headers,
    )
    assert reg_resp.status_code in (200, 202), (
        f"golden-repo register returned HTTP {reg_resp.status_code}: "
        f"{reg_resp.text[:300]}"
    )
    reg_job_id = reg_resp.json().get("job_id", "")
    assert reg_job_id, f"register response missing job_id: {reg_resp.json()}"
    _wait_for_job(client, reg_job_id, token_provider.get_headers(), "register")

    act_resp = client.post(
        "/api/repos/activate",
        json={"golden_repo_alias": _ALIAS},
        headers=token_provider.get_headers(),
    )
    assert act_resp.status_code in (200, 202), (
        f"activate returned HTTP {act_resp.status_code}: {act_resp.text[:300]}"
    )
    act_job_id = act_resp.json().get("job_id", "")
    assert act_job_id, f"activate response missing job_id: {act_resp.json()}"
    _wait_for_job(client, act_job_id, token_provider.get_headers(), "activate")

    # Drive the real front-door call known to produce a span (FastAPI +
    # custom instrumentation), a metric (cidx.fts.requests), and log
    # records (INFO+ logs emitted along the request path).
    search_resp = call_mcp_tool(
        client,
        "regex_search",
        {
            "repository_alias": f"{_ALIAS}-global",
            "pattern": _REGEX_PATTERN_ANY_FUNCTION,
            "limit": _REGEX_SEARCH_LIMIT,
        },
        token_provider.get_headers(),
    )
    assert search_resp.status_code == _HTTP_OK, (
        f"regex_search failed: {search_resp.status_code} {search_resp.text[:300]}"
    )

    # Force-flush all three providers so the round trip is deterministic (no
    # reliance on background batch/export timers) before querying each
    # backend's real surface.
    telemetry_manager = fresh_app.state.telemetry_manager
    assert telemetry_manager.tracer_provider is not None
    assert telemetry_manager.meter_provider is not None
    assert telemetry_manager.logger_provider is not None
    telemetry_manager.tracer_provider.force_flush(
        timeout_millis=_FORCE_FLUSH_TIMEOUT_MS
    )
    telemetry_manager.meter_provider.force_flush(timeout_millis=_FORCE_FLUSH_TIMEOUT_MS)
    telemetry_manager.logger_provider.force_flush(
        timeout_millis=_FORCE_FLUSH_TIMEOUT_MS
    )

    # SIGNAL 1: TRACE -- verified via Jaeger's real HTTP query API.
    def _trace_found() -> bool:
        resp = httpx.get(
            f"{_JAEGER_QUERY_URL}/api/traces",
            params={"service": "cidx-server", "limit": 20},
            timeout=5.0,
        )
        if resp.status_code != _HTTP_OK:
            return False
        traces = resp.json().get("data") or []
        return len(traces) >= 1

    _poll_until(
        _trace_found,
        _SIGNAL_POLL_TIMEOUT_S,
        _SIGNAL_POLL_INTERVAL_S,
        "at least one trace for service=cidx-server in Jaeger's query API",
    )

    # SIGNAL 2: METRIC -- verified via a real Prometheus query. Prometheus
    # exporter naming: namespace "cidx" + dotted metric name
    # "cidx.fts.requests" (dots -> underscores) + "_total" counter suffix,
    # confirmed empirically against this exact collector config.
    prometheus_metric_name = "cidx_cidx_fts_requests_total"

    def _metric_found() -> bool:
        resp = httpx.get(
            f"{_PROMETHEUS_URL}/api/v1/query",
            params={"query": prometheus_metric_name},
            timeout=5.0,
        )
        if resp.status_code != _HTTP_OK:
            return False
        result = resp.json().get("data", {}).get("result") or []
        return len(result) >= 1

    _poll_until(
        _metric_found,
        _SIGNAL_POLL_TIMEOUT_S,
        _SIGNAL_POLL_INTERVAL_S,
        f"Prometheus query {prometheus_metric_name!r} returning >=1 series",
    )

    # SIGNAL 3: LOG -- there is no log-query UI in this stack (Story #1676
    # AC8's documented, accepted methodology for this one signal). Verified
    # via TWO corroborating pieces of evidence: (a) the collector's own
    # self-metrics acceptance counter increased (ACCEPTANCE evidence, not
    # content), and (b) a real "LogRecord #" block appears in the
    # collector's debug exporter container output (CONTENT-level
    # corroboration).
    def _log_records_accepted() -> bool:
        current = httpx.get(_COLLECTOR_SELF_METRICS_URL, timeout=5.0).text
        after = _extract_counter_value(current, "otelcol_receiver_accepted_log_records")
        return after > log_records_before

    _poll_until(
        _log_records_accepted,
        _SIGNAL_POLL_TIMEOUT_S,
        _SIGNAL_POLL_INTERVAL_S,
        "otelcol_receiver_accepted_log_records to increase on the collector's "
        "self-metrics port",
    )

    collector_logs = _compose("logs", "otel-collector").stdout
    assert "LogRecord #" in collector_logs, (
        "collector debug-exporter output did not contain a LogRecord block "
        "-- log content-level corroboration missing"
    )
