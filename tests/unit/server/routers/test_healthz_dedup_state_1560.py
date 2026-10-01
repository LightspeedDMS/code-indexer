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

Bug #1932 root cause: only overriding `service.database_url` (as done
above) does NOT isolate HealthCheckService from the REAL host it runs
on. Real-host signals feed _calculate_overall_status() independently of
the dedup row and are proven (on this exact repro) to push the overall
status past DEGRADED into UNHEALTHY, which correctly drives /healthz to
503 per its own (unbroken) mapping -- a false failure of THIS test's
premise, not a production regression:

  1. HealthCheckService._get_system_info() calls real psutil functions,
     including psutil.disk_partitions()/psutil.disk_usage(), which
     enumerate every REAL mounted, non-removable volume on the host.
     On the exact repro host the real root filesystem was independently
     at 91.4% used (`psutil.disk_usage("/").percent == 91.4`), at/above
     the real DISK_CRITICAL_THRESHOLD_PERCENT (90.0) loaded by
     _load_thresholds_from_config() -- this alone flips has_error=True
     in _collect_volume_failures(), regardless of the dedup row.
     (_check_storage_health() also calls psutil.disk_usage(), on
     config.server_dir i.e. ~/.cidx-server, which happens to live on a
     separate, less-full real mount on the repro host -- proving the
     two disk checks read different real paths through the same psutil
     primitive, and neither is touched by swapping database_url.)
  2. get_system_health() also calls the module-level
     get_database_health_service() singleton directly (not
     `self.database_url`) -- a completely separate real-host dependency
     the per-instance override never touches.
  3. _collect_golden_repos_storage_failures() resolves
     app.state.golden_repos_dir via `from ..app import app as
     app_module` -- server/app.py's own PEP 562 lazily-initialized
     module-level singleton, which is NOT guaranteed to be the same
     FastAPI instance create_app() builds for this test's TestClient
     (another test earlier in the same process may already have
     realized a different `app` singleton, or none may exist yet) --
     and, when a golden_repos_dir is resolved, shells out a real,
     multi-second `timeout ... ls <dir>` subprocess probe.

None of these participate in the #1560 contract under test here
(dedup-state row -> DEGRADED -> /healthz stays 200). They are
neutralized below at their true external boundaries -- the real
`psutil` library functions HealthCheckService calls, the real
module-level `get_database_health_service` collaborator, and the real
server/app.py module-level `app` singleton (wired to this test's own
app instance, then cleared) -- never by patching a HealthCheckService
method itself, so the ONLY variable driving _calculate_overall_status()
is the dedup-state row, making the assertions deterministic on any
machine and in any pytest process state. The dedup collection chain
itself (_collect_fleet_migration_dedup_failures(),
_calculate_overall_status()'s aggregation/priority logic, and the
/healthz route's status -> HTTP mapping) remains completely real and
unmocked.
"""

import importlib
import sqlite3
import types

import psutil
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from code_indexer.server.app import create_app
from code_indexer.server.services.health_service import HealthCheckService

HEALTHZ_PATH = "/healthz"
INLINE_MISC_MODULE_PATH = "code_indexer.server.routers.inline_misc"
HEALTH_SERVICE_MODULE_PATH = "code_indexer.server.services.health_service"
SERVER_APP_MODULE_PATH = "code_indexer.server.app"

# Bug #1932: fixture values for the fixed, deterministic psutil readings
# patched in below -- well clear of the real
# DISK_WARNING_THRESHOLD_PERCENT (80.0) / DISK_CRITICAL_THRESHOLD_PERCENT
# (90.0) and CPU/memory thresholds on any machine, so this test's only
# active signal is the dedup-state row.
CLEAN_DISK_TOTAL_BYTES = 100 * (1024**3)
CLEAN_DISK_USED_BYTES = 20 * (1024**3)
CLEAN_DISK_FREE_BYTES = CLEAN_DISK_TOTAL_BYTES - CLEAN_DISK_USED_BYTES
CLEAN_DISK_USED_PERCENT = 20.0
CLEAN_MEMORY_TOTAL_BYTES = 16 * (1024**3)
CLEAN_MEMORY_USED_BYTES = 3 * (1024**3)
CLEAN_MEMORY_AVAILABLE_BYTES = CLEAN_MEMORY_TOTAL_BYTES - CLEAN_MEMORY_USED_BYTES
CLEAN_MEMORY_FREE_BYTES = CLEAN_MEMORY_AVAILABLE_BYTES
CLEAN_MEMORY_USAGE_PERCENT = 20.0
CLEAN_CPU_USAGE_PERCENT = 20.0

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


class _NoOpDatabaseHealthService:
    """Bug #1932: stand-in for the REAL, separate
    get_database_health_service() singleton, which reads the actual
    host's production database independently of this test's
    per-instance `service.database_url` override. Returning an empty
    list here is the deterministic "no database failures" input
    _collect_database_failures() already treats as healthy -- it does
    not touch the dedup-state logic under test."""

    def get_all_database_health(self):
        return []


def _clean_disk_usage(_path=None):
    """Bug #1932: deterministic stand-in for the real external
    psutil.disk_usage() syscall -- called by BOTH
    HealthCheckService._get_system_info()'s root-disk read and
    _check_storage_health()'s config.server_dir read -- so neither
    check ever depends on this machine's actual real disk usage."""
    return types.SimpleNamespace(
        total=CLEAN_DISK_TOTAL_BYTES,
        used=CLEAN_DISK_USED_BYTES,
        free=CLEAN_DISK_FREE_BYTES,
        percent=CLEAN_DISK_USED_PERCENT,
    )


def _clean_virtual_memory():
    """Bug #1932: deterministic stand-in for the real external
    psutil.virtual_memory() syscall. Includes total/used/available/free
    (not just percent) because real background services started by
    create_app()'s real lifespan -- MemoryGovernor, SystemMetricsCollector
    -- also call this same patched function and read those fields."""
    return types.SimpleNamespace(
        total=CLEAN_MEMORY_TOTAL_BYTES,
        used=CLEAN_MEMORY_USED_BYTES,
        available=CLEAN_MEMORY_AVAILABLE_BYTES,
        free=CLEAN_MEMORY_FREE_BYTES,
        percent=CLEAN_MEMORY_USAGE_PERCENT,
    )


def _patch_real_host_dependencies(monkeypatch) -> None:
    """Bug #1932: neutralize the real psutil disk/memory/CPU syscalls and
    the separate get_database_health_service() collaborator at their
    true external boundaries -- never a HealthCheckService method --
    so a real host's disk/CPU/memory usage and production database
    state can never influence _calculate_overall_status()."""
    monkeypatch.setattr(psutil, "disk_partitions", lambda *a, **k: [])
    monkeypatch.setattr(psutil, "disk_usage", _clean_disk_usage)
    monkeypatch.setattr(psutil, "virtual_memory", _clean_virtual_memory)
    monkeypatch.setattr(psutil, "cpu_percent", lambda *a, **k: CLEAN_CPU_USAGE_PERCENT)
    health_service_module = importlib.import_module(HEALTH_SERVICE_MODULE_PATH)
    monkeypatch.setattr(
        health_service_module,
        "get_database_health_service",
        lambda: _NoOpDatabaseHealthService(),
    )


def _seed_dedup_state_table(db_path: str) -> None:
    """Create fleet_migration_dedup_state in the given SQLite DB and
    insert the one active (uncleared) fixture row every test below
    relies on."""
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


@pytest.fixture(autouse=True)
def _reset_healthz_ttl_cache():
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    inline_misc._reset_healthz_cache()
    yield
    inline_misc._reset_healthz_cache()


def _real_service_with_active_dedup_row(tmp_path, monkeypatch) -> HealthCheckService:
    """A genuine HealthCheckService (real ConfigManager/system-info/DB
    connectivity checks) pointed at a real temp SQLite DB pre-populated
    with one active (uncleared) fleet_migration_dedup_state row, with
    the real-host confounds documented in the module docstring
    neutralized (Bug #1932) so the dedup-state row is the ONLY
    condition able to drive _calculate_overall_status() away from
    HEALTHY. _calculate_overall_status() itself and
    _collect_fleet_migration_dedup_failures() stay completely real."""
    service = HealthCheckService()
    db_path = str(tmp_path / "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"

    _patch_real_host_dependencies(monkeypatch)
    _seed_dedup_state_table(db_path)

    return service


def _call_real_healthz_with_active_dedup_row(tmp_path, monkeypatch) -> Response:
    """Shared setup for both tests below: swap the module-level
    health_service singleton for a real instance carrying an active
    dedup-state row, then issue a real GET /healthz through the real
    route -- no mocking of the system under test."""
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    real_service = _real_service_with_active_dedup_row(tmp_path, monkeypatch)
    monkeypatch.setattr(inline_misc, "health_service", real_service)
    app = create_app()
    with TestClient(app) as client:
        # Bug #1932: _collect_golden_repos_storage_failures() resolves
        # app.state.golden_repos_dir via `from ..app import app as
        # app_module` -- server/app.py's OWN lazily-initialized module
        # singleton, not necessarily this TestClient's own app instance.
        # Wiring the module's real `app` name directly to this test's
        # real `app` (raising=False: the name may not exist in the
        # module's __dict__ yet) makes the resolver read THIS test's
        # real, controlled app.state -- a genuine external module-level
        # dependency, not a HealthCheckService method.
        server_app_module = importlib.import_module(SERVER_APP_MODULE_PATH)
        monkeypatch.setattr(server_app_module, "app", app, raising=False)
        app.state.golden_repos_dir = None
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
