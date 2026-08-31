"""
Story #1560 AC15a regression: /healthz must stay HTTP 200 even when the
fleet-migration duplicate-point-id auto-resolution outcome (AC15) is the
cause that drove the overall health status to DEGRADED.

Maintainer decision 5: this condition is "DEGRADED on detection, never
UNHEALTHY" -- an affected repo remains fully queryable, so the liveness
probe a load balancer polls must never drain it.

Unlike test_healthz_liveness_endpoint_1433.py's generic mapping tests
(which mock health_service.get_system_health()'s return value), this
test swaps in a REAL HealthCheckService pointed at a real temp SQLite DB
carrying an active fleet_migration_dedup_state row (the exact fixture
test_health_service_dedup_state_1560.py already proves drives
_calculate_overall_status() to DEGRADED) so the full, unmocked
get_system_health() -> _calculate_overall_status() ->
_collect_fleet_migration_dedup_failures() chain runs for real, all the
way through the actual /healthz route.
"""

import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from code_indexer.server.app import create_app
from code_indexer.server.services.health_service import HealthCheckService

HEALTHZ_PATH = "/healthz"
INLINE_MISC_MODULE_PATH = "code_indexer.server.routers.inline_misc"

# Fixture row values for the one active fleet_migration_dedup_state
# record used by every test below -- a realistic outcome from repairing
# one legacy collection with 33 duplicate id_index groups.
DEDUP_ROW_ALIAS = "click"
DEDUP_ROW_DUPLICATE_GROUPS = 33
DEDUP_ROW_RECORDS_BEFORE = 343604
DEDUP_ROW_RECORDS_DELETED = 43
DEDUP_ROW_WINNER_KEPT_GROUPS = 23
DEDUP_ROW_WHOLE_GROUP_DELETED_GROUPS = 10
DEDUP_ROW_COLLECTION_TOTAL = 343604
DEDUP_ROW_DROPPED_AT = "2026-08-11T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_healthz_ttl_cache():
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    inline_misc._reset_healthz_cache()
    yield
    inline_misc._reset_healthz_cache()


def _real_service_with_active_dedup_row(tmp_path) -> HealthCheckService:
    """A genuine HealthCheckService (real ConfigManager/system-info/DB
    connectivity checks) pointed at a real temp SQLite DB pre-populated
    with one active (uncleared) fleet_migration_dedup_state row."""
    service = HealthCheckService()
    db_path = str(tmp_path / "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE fleet_migration_dedup_state (
                golden_alias TEXT PRIMARY KEY NOT NULL,
                duplicate_groups INTEGER NOT NULL DEFAULT 0,
                records_before INTEGER NOT NULL DEFAULT 0,
                records_deleted INTEGER NOT NULL DEFAULT 0,
                winner_kept_groups INTEGER NOT NULL DEFAULT 0,
                whole_group_deleted_groups INTEGER NOT NULL DEFAULT 0,
                collection_total INTEGER NOT NULL DEFAULT 0,
                first_dropped_at TEXT,
                dropped_at TEXT,
                cleared_at TEXT,
                cleared_reason TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO fleet_migration_dedup_state "
            "(golden_alias, duplicate_groups, records_before, "
            "records_deleted, winner_kept_groups, "
            "whole_group_deleted_groups, collection_total, "
            "first_dropped_at, dropped_at, cleared_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                DEDUP_ROW_ALIAS,
                DEDUP_ROW_DUPLICATE_GROUPS,
                DEDUP_ROW_RECORDS_BEFORE,
                DEDUP_ROW_RECORDS_DELETED,
                DEDUP_ROW_WINNER_KEPT_GROUPS,
                DEDUP_ROW_WHOLE_GROUP_DELETED_GROUPS,
                DEDUP_ROW_COLLECTION_TOTAL,
                DEDUP_ROW_DROPPED_AT,
                DEDUP_ROW_DROPPED_AT,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return service


def _call_real_healthz_with_active_dedup_row(tmp_path, monkeypatch) -> Response:
    """Shared setup for both tests below: swap the module-level
    health_service singleton for a real instance carrying an active
    dedup-state row, then issue a real GET /healthz through the real
    route -- no mocking of the system under test."""
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    real_service = _real_service_with_active_dedup_row(tmp_path)
    monkeypatch.setattr(inline_misc, "health_service", real_service)
    with TestClient(create_app()) as client:
        return client.get(HEALTHZ_PATH)


class TestHealthzStaysUpForDedupStateCause:
    def test_healthz_returns_200_when_real_dedup_row_causes_degraded(
        self, tmp_path, monkeypatch
    ):
        response = _call_real_healthz_with_active_dedup_row(tmp_path, monkeypatch)

        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"status"}
        assert body["status"] == "degraded"

    def test_healthz_body_never_leaks_dedup_state_detail(self, tmp_path, monkeypatch):
        """The unauthenticated liveness probe stays minimal -- proves the
        new fleet_migration_dedup_state field is not an information-
        disclosure leak on this public endpoint, even when it is
        populated server-side by a real row."""
        response = _call_real_healthz_with_active_dedup_row(tmp_path, monkeypatch)

        raw_text = response.text
        assert DEDUP_ROW_ALIAS not in raw_text
        assert "records_deleted" not in raw_text
