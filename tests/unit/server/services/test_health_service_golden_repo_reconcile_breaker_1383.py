"""
Unit tests for GitHub Issue #1383 (follow-up to Bug #1382): the golden-repo
registry-reconcile circuit-breaker's health-escalation signal goes silent
exactly when auto-removal fires, and the buildup message doesn't say which
repos are at risk.

This module covers the two health_service.py-side behaviors from issue
#1383:

1. The DEGRADED `failure_reasons` message during buildup (confirmation
   counts 1-2) must be enriched to include the actual orphan alias set at
   risk and a "will auto-remove at N/N confirmations" framing, instead of
   a bare count.
2. A new, independently queryable read method exposes the persisted
   auto-heal event record (Issue #1383, sqlite_backends.py /
   golden_repo_metadata_backend.py) so an operator can discover after the
   fact that an automatic mass-removal occurred and which repos were
   affected -- WITHOUT that historical, already-resolved event being folded
   into the DEGRADED failure_reasons surface (it is informational only).
3. Code-review remediation (Finding 1): the discovery surface from (2) must
   actually be reachable through a real front door. `get_system_health()`
   is the method `GET /api/system/health` (routers/inline_misc.py) returns
   verbatim as its `HealthCheckResponse`, and is also what the MCP
   dashboard and other health surfaces call -- so proving the auto-heal
   event appears on the object `get_system_health()` returns (via a new
   `last_golden_repo_reconcile_auto_heal` field) proves front-door
   reachability, not just internal-method correctness.
"""

import os
import sqlite3
import tempfile
from typing import Optional, Tuple
from unittest.mock import patch

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.services.golden_repo_reconciler import (
    CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD,
)


def _make_service_with_temp_db(
    create_breaker_table: bool = False,
    breaker_row: Optional[Tuple] = None,
    create_auto_heal_table: bool = False,
    auto_heal_row: Optional[Tuple] = None,
):
    """
    Build a HealthCheckService pointed at a temp SQLite DB, optionally
    pre-populated with a golden_repo_reconcile_breaker_state and/or
    golden_repo_reconcile_auto_heal_event row.
    """
    service = HealthCheckService()
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"

    conn = sqlite3.connect(db_path)
    try:
        if create_breaker_table:
            conn.execute(
                """
                CREATE TABLE golden_repo_reconcile_breaker_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    orphan_fingerprint TEXT,
                    consecutive_count INTEGER NOT NULL DEFAULT 0,
                    first_observed_at TEXT,
                    last_observed_at TEXT,
                    updated_at TEXT
                )
                """
            )
            if breaker_row is not None:
                conn.execute(
                    "INSERT INTO golden_repo_reconcile_breaker_state "
                    "(id, orphan_fingerprint, consecutive_count) VALUES (1, ?, ?)",
                    breaker_row,
                )
        if create_auto_heal_table:
            conn.execute(
                """
                CREATE TABLE golden_repo_reconcile_auto_heal_event (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    removed_aliases TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
            if auto_heal_row is not None:
                conn.execute(
                    "INSERT INTO golden_repo_reconcile_auto_heal_event "
                    "(id, removed_aliases, occurred_at) VALUES (1, ?, ?)",
                    auto_heal_row,
                )
        conn.commit()
    finally:
        conn.close()

    return service


class TestGoldenRepoReconcileBreakerBuildupMessageEnrichment1383:
    def test_buildup_message_includes_alias_set_at_count_one(self):
        """At confirmation count 1, the DEGRADED message must name the
        actual at-risk aliases, not just a bare count."""
        service = _make_service_with_temp_db(
            create_breaker_table=True, breaker_row=("alias-a,alias-b", 1)
        )

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is True
        assert has_error is False
        assert len(reasons) == 1
        assert "alias-a" in reasons[0]
        assert "alias-b" in reasons[0]

    def test_buildup_message_includes_alias_set_at_count_two(self):
        service = _make_service_with_temp_db(
            create_breaker_table=True, breaker_row=("alias-a,alias-b,alias-c", 2)
        )

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is True
        for alias in ("alias-a", "alias-b", "alias-c"):
            assert alias in reasons[0]

    def test_buildup_message_states_will_auto_remove_at_threshold(self):
        """The message must explicitly frame the risk: it WILL auto-remove
        once CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD confirmations are
        reached -- not just report the current count in isolation."""
        service = _make_service_with_temp_db(
            create_breaker_table=True, breaker_row=("alias-x,alias-y", 2)
        )

        _, _, reasons = service._collect_golden_repo_reconcile_breaker_failures()

        message = reasons[0]
        assert f"2/{CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD}" in message
        assert (
            f"{CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD}/"
            f"{CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD}" in message
        )
        assert "auto-remove" in message.lower()
        assert "confirmation" in message.lower()

    def test_buildup_message_handles_empty_fingerprint_defensively(self):
        """Defensive: an empty/blank fingerprint must not crash message
        construction or render a literal 'None'."""
        service = _make_service_with_temp_db(
            create_breaker_table=True, breaker_row=("", 1)
        )

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is True
        assert "None" not in reasons[0]


class TestGoldenRepoReconcileAutoHealEventDiscoverySurface1383:
    def test_no_table_returns_none(self):
        """Fresh install / no auto-heal event has ever fired -- fail-open,
        no crash."""
        service = _make_service_with_temp_db()

        assert service.get_golden_repo_reconcile_auto_heal_event() is None

    def test_table_exists_but_no_row_returns_none(self):
        service = _make_service_with_temp_db(create_auto_heal_table=True)

        assert service.get_golden_repo_reconcile_auto_heal_event() is None

    def test_recorded_event_is_discoverable(self):
        """The core Issue #1383 fix: a confirmed auto-removal event must be
        independently queryable/discoverable -- an operator who wasn't
        watching /health in real time can still find it."""
        service = _make_service_with_temp_db(
            create_auto_heal_table=True,
            auto_heal_row=("orphan-1,orphan-2", "2026-01-01T00:00:00+00:00"),
        )

        event = service.get_golden_repo_reconcile_auto_heal_event()

        assert event is not None
        assert sorted(event["removed_aliases"]) == ["orphan-1", "orphan-2"]
        assert event["occurred_at"] == "2026-01-01T00:00:00+00:00"

    def test_db_error_is_fail_open_not_raised(self):
        """Any unexpected DB error reading the auto-heal event must never
        propagate -- this is a best-effort discovery aid, not a source of
        crashes."""
        service = _make_service_with_temp_db()

        with patch.object(
            service,
            "_read_golden_repo_reconcile_auto_heal_event",
            side_effect=RuntimeError("boom"),
        ):
            assert service.get_golden_repo_reconcile_auto_heal_event() is None

    def test_recorded_event_is_never_folded_into_degraded_status(self):
        """Requirement 2 explicitly states this is INFORMATIONAL, NOT
        DEGRADED -- a historical, already-resolved auto-heal event must
        never appear in _collect_golden_repo_reconcile_breaker_failures()
        or cause has_warning/has_error to be True on its own."""
        service = _make_service_with_temp_db(
            create_auto_heal_table=True,
            auto_heal_row=("orphan-1", "2026-01-01T00:00:00+00:00"),
        )

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []


class TestGoldenRepoReconcileAutoHealEventWiredIntoHealthResponse1383:
    """Finding 1 (code review of #1383): the auto-heal event discovery
    surface has zero production callers unless it is wired into the actual
    `/health` response object. `get_system_health()` is the exact method
    `GET /api/system/health` (routers/inline_misc.py) returns verbatim, so
    asserting on ITS return value -- not just the internal
    get_golden_repo_reconcile_auto_heal_event() helper -- proves front-door
    reachability."""

    def test_recorded_event_appears_in_full_health_response(self):
        service = _make_service_with_temp_db(
            create_auto_heal_table=True,
            auto_heal_row=("orphan-1,orphan-2", "2026-01-01T00:00:00+00:00"),
        )

        response = service.get_system_health()

        event = response.last_golden_repo_reconcile_auto_heal
        assert event is not None
        assert sorted(event["removed_aliases"]) == ["orphan-1", "orphan-2"]
        assert event["occurred_at"] == "2026-01-01T00:00:00+00:00"

    def test_no_event_yields_none_in_full_health_response(self):
        service = _make_service_with_temp_db()

        response = service.get_system_health()

        assert response.last_golden_repo_reconcile_auto_heal is None

    def test_recorded_event_never_affects_status_or_failure_reasons_via_full_response(
        self,
    ):
        """The event is a historical, already-resolved record -- its mere
        presence on the response must never push status to DEGRADED/
        UNHEALTHY nor leak into failure_reasons (that surface is reserved
        for CURRENT, unresolved conditions). Compared against a control
        response built from an otherwise-identical service with no
        auto-heal row, since real system resource readings (disk/CPU/
        memory) are not controlled by this test and must not be asserted
        as a fixed value.
        """
        service_with_event = _make_service_with_temp_db(
            create_auto_heal_table=True,
            auto_heal_row=("orphan-1", "2026-01-01T00:00:00+00:00"),
        )
        control_service = _make_service_with_temp_db()

        response_with_event = service_with_event.get_system_health()
        control_response = control_service.get_system_health()

        assert response_with_event.last_golden_repo_reconcile_auto_heal is not None
        assert control_response.last_golden_repo_reconcile_auto_heal is None
        assert response_with_event.status == control_response.status
        assert "auto-heal" not in " ".join(response_with_event.failure_reasons).lower()
        assert "orphan-1" not in " ".join(response_with_event.failure_reasons)
