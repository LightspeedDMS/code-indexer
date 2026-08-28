"""Phase 3 E2E — Story #1586: telemetry metrics wired end-to-end.

Drives a REAL MCP regex_search call through the REAL FastAPI front door
(session-scoped test_client + seeded_indexed_client fixtures already
established for this phase -- see conftest.py), against the already
registered+activated "markupsafe" golden repo, and verifies the real
cidx.fts.* OTEL metric was recorded.

The metrics SINK is swapped to a real OTEL SDK InMemoryMetricReader for the
duration of this one test only -- exactly the technique the story itself
calls out ("an in-memory/fake OTEL collector or the SDK's own in-memory
exporters"), reusing the same otel_test_support helper already exercised
throughout tests/unit/server/telemetry/test_search_handler_wiring_1586.py.
Every other part of the stack (HTTP request, routing, auth, MCP dispatch,
regex search execution) is the real, unmodified production code path.

Story #1586 code-review Finding 4: the first test below installs
ApplicationMetrics directly via active_application_metrics_singleton()
BEFORE making the MCP request -- it would still pass even if
startup/lifespan.py stopped constructing/storing ApplicationMetrics
entirely. TestLifespanRealStartupWiring (bottom of this file) closes that
gap: it creates a genuinely FRESH app via create_app() with real startup
config (telemetry_config.enabled=true seeded into config.json -- the
DB-backed value; Story #1676 AC1 removed the CIDX_TELEMETRY_ENABLED env var
override) and asserts, by object IDENTITY, that
app.state.application_metrics/job_metrics are the exact singletons
lifespan.py's own code resolves through get_application_metrics()/
get_job_metrics() -- proving the real startup wiring itself, not merely
that metric-recording works once a private singleton is already in place.
"""

from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from code_indexer.server.app import create_app
from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.server.mcp_helpers import call_mcp_tool
from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    active_job_metrics_singleton,
    find_metric,
)

_REGEX_PATTERN_ANY_FUNCTION = "def .+"
_REGEX_SEARCH_LIMIT = 3
_HTTP_OK = 200


def test_regex_search_emits_fts_metric_via_real_mcp_front_door(
    seeded_indexed_client: "tuple[TestClient, str]",
    admin_token_provider: AdminTokenProvider,
) -> None:
    test_client, alias = seeded_indexed_client

    with active_application_metrics_singleton() as (_metrics, reader):
        headers = admin_token_provider.get_headers()
        resp = call_mcp_tool(
            test_client,
            "regex_search",
            {
                "repository_alias": f"{alias}-global",
                "pattern": _REGEX_PATTERN_ANY_FUNCTION,
                "limit": _REGEX_SEARCH_LIMIT,
            },
            headers,
        )
        assert resp.status_code == _HTTP_OK, (
            f"regex_search failed: {resp.status_code} {resp.text[:300]}"
        )

        fts_metric = find_metric(reader, "cidx.fts.requests")
        assert fts_metric is not None, (
            "cidx.fts.requests was not emitted by the real regex_search "
            "MCP call -- AC1 wiring regression"
        )
        data_points = list(fts_metric.data.data_points)
        assert len(data_points) >= 1
        assert data_points[0].attributes["repository"] == f"{alias}-global"
        assert data_points[0].attributes["status"] == "success"


class TestLifespanRealStartupWiring:
    """Story #1586 Finding 4: prove startup/lifespan.py itself -- driven by
    REAL startup config, not a test substituting a private singleton --
    constructs and assigns ApplicationMetrics/JobMetrics.
    """

    def test_lifespan_constructs_real_application_and_job_metrics_from_startup_config(
        self, tmp_path
    ) -> None:
        """Uses an ISOLATED CIDX_SERVER_DATA_DIR (never the shared session
        one) -- create_app() against the SAME data dir as the session's
        test_client would share DatabaseConnectionManager's
        singleton-per-path instance, so this test's own TestClient.__exit__
        shutdown would close the SQLite connection the rest of the E2E
        session still needs (observed directly: a later session-teardown
        fixture failed with "Cannot operate on a closed database" before
        this isolation fix).

        Story #1676 AC1: telemetry configuration is managed exclusively via
        the Web UI Config Screen (DB-backed) -- environment variables no
        longer enable it. Telemetry is enabled here by seeding
        telemetry_config.enabled=true directly into config.json at the
        isolated data dir before create_app() runs (the DB-backed
        bootstrap-equivalent value), not via CIDX_TELEMETRY_ENABLED."""
        previous_data_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
        isolated_data_dir = tmp_path / "isolated-data-dir"
        os.environ["CIDX_SERVER_DATA_DIR"] = str(isolated_data_dir)
        try:
            isolated_data_dir.mkdir(parents=True, exist_ok=True)
            (isolated_data_dir / "config.json").write_text(
                json.dumps({"telemetry_config": {"enabled": True}})
            )
            with active_application_metrics_singleton() as (app_metrics, _areader):
                with active_job_metrics_singleton() as (job_metrics, _jreader):
                    fresh_app = create_app()
                    with TestClient(fresh_app, raise_server_exceptions=False):
                        assert fresh_app.state.application_metrics is app_metrics, (
                            "lifespan.py must assign the REAL ApplicationMetrics "
                            "singleton it resolves from actual startup config -- "
                            "not a private test-injected replacement"
                        )
                        assert fresh_app.state.application_metrics.is_active is True
                        assert fresh_app.state.job_metrics is job_metrics, (
                            "lifespan.py must assign the REAL JobMetrics "
                            "singleton it resolves from actual startup config"
                        )
                        assert fresh_app.state.job_metrics.is_active is True
        finally:
            if previous_data_dir is None:
                os.environ.pop("CIDX_SERVER_DATA_DIR", None)
            else:
                os.environ["CIDX_SERVER_DATA_DIR"] = previous_data_dir
