"""The SIEM capture hook inside insert_events (SQLite and PostgreSQL)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List

import pytest

from code_indexer.server.services.audit_events import (
    AuditEvent,
    build_event,
    build_legacy_event,
)
from code_indexer.server.services.siem_delivery import capture
from code_indexer.server.services.siem_delivery.capture import (
    CaptureSnapshot,
    SiemTarget,
    capture_failures_since_boot,
    capture_skipped_snapshot_expired,
    publish_capture_state,
)

from .backends import SiemBackendHarness

DEST = "gsecops:00000000000000aa"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> Iterator[_Clock]:
    c = _Clock()
    capture.reset_capture_state_for_tests(c)
    yield c
    capture.reset_capture_state_for_tests()


def _arm(clock: _Clock, active: bool = True) -> None:
    publish_capture_state(CaptureSnapshot(True, active, DEST, clock.now))


def _login(actor: str = "alice") -> AuditEvent:
    return build_event(
        actor=actor,
        action_type="authentication_success",
        target_type="auth",
        target_id=actor,
        outcome="success",
        details={"method": "password", "mfa": "not_enrolled", "flow": "rest_token"},
    )


def _config_row() -> AuditEvent:
    return build_event(
        actor="alice",
        action_type="config_changed",
        target_type="config",
        target_id="siem_delivery",
        outcome="success",
        details={
            "change_kind": "update",
            "changed_keys": ["siem_delivery_config.enabled"],
        },
    )


def _queue(b: SiemBackendHarness) -> List[dict]:
    return b.db.read(
        lambda tx: tx.query(
            "SELECT event_uuid, destination_key, status, event_payload, "
            "projection_error, boundary_kind, mapping_version "
            "FROM siem_delivery_queue ORDER BY id"
        )
    )


def _audit_uuids(b: SiemBackendHarness) -> List[str]:
    rows = b.db.read(
        lambda tx: tx.query("SELECT event_uuid FROM audit_logs ORDER BY id")
    )
    return [r["event_uuid"] for r in rows]


def test_armed_pilot_event_commits_audit_and_queue_row_together(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    event = _login()
    siem_backend.audit.insert_events([event])
    assert _audit_uuids(siem_backend) == [event.event_uuid]
    rows = _queue(siem_backend)
    assert [r["event_uuid"] for r in rows] == [event.event_uuid]
    assert rows[0]["status"] == "pending" and rows[0]["destination_key"] == DEST
    assert rows[0]["event_payload"] and rows[0]["projection_error"] is None
    assert rows[0]["boundary_kind"] is None


def test_a_retried_batch_counts_only_the_final_failure(
    siem_backend: SiemBackendHarness,
    clock: _Clock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The writer retries a failed multi-row batch row by row: only the row
    that still fails is a capture gap (and an ERROR), not the whole batch."""
    from code_indexer.server.services.audit_log_service import AuditLogService

    service = (
        siem_backend.audit
        if siem_backend.name == "sqlite"
        else AuditLogService(tmp_path / "unused.db", storage_backend=siem_backend.audit)
    )
    _arm(clock)
    good1, poison, good2 = _login("a"), _login("b"), _login("c")
    siem_backend.fail_audit_inserts(only_uuid=poison.event_uuid)
    with caplog.at_level(logging.ERROR, logger=capture.__name__):
        service._write_batch([good1, poison, good2])
    assert capture_failures_since_boot().get("transaction_failed") == 1
    errors = [r for r in caplog.records if r.name == capture.__name__]
    assert len(errors) == 1 and poison.event_uuid in errors[0].getMessage()
    assert [r["event_uuid"] for r in _queue(siem_backend)] == [
        good1.event_uuid,
        good2.event_uuid,
    ]


def test_inactive_or_unloaded_capture_writes_no_queue_row(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    siem_backend.audit.insert_events([_login()])  # not loaded
    _arm(clock, active=False)
    siem_backend.audit.insert_events([_login()])
    assert _queue(siem_backend) == []
    assert len(_audit_uuids(siem_backend)) == 2


def test_expired_snapshot_captures_nothing_and_is_counted(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    clock.now += 91.0
    siem_backend.audit.insert_events([_login()])
    assert _queue(siem_backend) == []
    assert capture_skipped_snapshot_expired() == 1


def test_out_of_scope_events_are_never_captured(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    events = [
        build_legacy_event(
            actor="alice",
            action_type="token_refresh_success",
            target_type="auth",
            target_id="alice",
            details_json=None,
        )
        for _ in range(100)
    ]
    events.append(
        build_event(
            actor="alice",
            action_type="api_key_deleted",
            target_type="api_key",
            target_id="k1",
            outcome="success",
            details={"key_id": "k1"},
        )
    )
    siem_backend.audit.insert_events(events)
    assert _queue(siem_backend) == []


def test_self_report_uses_its_explicit_destination_even_when_unarmed(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    row = _config_row()
    siem_backend.audit.insert_events(
        [row], siem_destinations={row.event_uuid: SiemTarget(DEST, "enable")}
    )
    rows = _queue(siem_backend)
    assert rows[0]["boundary_kind"] == "enable" and rows[0]["destination_key"] == DEST


def test_self_report_without_destination_is_deliberately_not_captured(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    row = _config_row()
    siem_backend.audit.insert_events([row], siem_destinations={row.event_uuid: None})
    assert _queue(siem_backend) == [] and capture_failures_since_boot() == {}


def test_self_report_on_the_writer_path_is_counted_never_guessed(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    siem_backend.audit.insert_events([_config_row()])
    assert _queue(siem_backend) == []
    assert capture_failures_since_boot() == {"self_report_without_destination": 1}


def test_failed_queue_insert_never_blocks_the_audit_row(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    siem_backend.fail_queue_inserts()
    event = _login()
    siem_backend.audit.insert_events([event])
    assert _audit_uuids(siem_backend) == [event.event_uuid]
    assert _queue(siem_backend) == []
    assert capture_failures_since_boot() == {"insert_failed": 1}


def test_one_failed_queue_row_costs_only_itself(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    events = [_login("alice"), _login("bob"), _login("carol")]
    siem_backend.fail_queue_inserts(only_uuid=events[1].event_uuid)
    siem_backend.audit.insert_events(events)
    assert len(_audit_uuids(siem_backend)) == 3
    assert [r["event_uuid"] for r in _queue(siem_backend)] == [
        events[0].event_uuid,
        events[2].event_uuid,
    ]
    assert capture_failures_since_boot() == {"insert_failed": 1}


def test_failed_whole_transaction_is_counted_and_raises_to_the_audit_layer(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    siem_backend.fail_audit_inserts()
    with pytest.raises(Exception):
        siem_backend.audit.insert_events([_login()])
    assert _queue(siem_backend) == [] and _audit_uuids(siem_backend) == []
    assert capture_failures_since_boot() == {"transaction_failed": 1}


def test_duplicate_event_uuid_is_a_counted_constraint_failure(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    _arm(clock)
    event = _login()
    siem_backend.audit.insert_events([event])
    siem_backend.audit.insert_events([event])
    assert len(_queue(siem_backend)) == 1
    assert capture_failures_since_boot() == {"insert_failed": 1}


def test_projection_error_row_is_still_captured_with_the_field_name(
    siem_backend: SiemBackendHarness, clock: _Clock
) -> None:
    import dataclasses
    import json

    _arm(clock)
    bad = dataclasses.replace(_login(), details_json=json.dumps({"method": "x y"}))
    siem_backend.audit.insert_events([bad])
    row = _queue(siem_backend)[0]
    assert row["event_payload"] is None
    assert row["projection_error"] == "details.method"


def test_no_projection_runs_inside_the_sqlite_audit_transaction(
    tmp_path: object, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instrument project() and the transaction: zero projections while the
    write lock is held, and the only SIEM statement inside is the INSERT."""
    from pathlib import Path

    from code_indexer.server.services.audit_log_service import AuditLogService

    path = Path(str(tmp_path)) / "groups.db"
    audit = AuditLogService(path)
    in_tx = {"flag": False, "projections_in_tx": 0, "statements": []}
    real_project = capture.project

    def _spy_project(*args, **kwargs):  # type: ignore[no-untyped-def]
        if in_tx["flag"]:
            in_tx["projections_in_tx"] += 1  # type: ignore[operator]
        return real_project(*args, **kwargs)

    monkeypatch.setattr(capture, "project", _spy_project)
    manager = audit._conn_manager
    real_atomic = manager.execute_atomic

    def _traced_atomic(op):  # type: ignore[no-untyped-def]
        def _wrapped(conn):  # type: ignore[no-untyped-def]
            in_tx["flag"] = True
            conn.set_trace_callback(in_tx["statements"].append)  # type: ignore[attr-defined]
            try:
                return op(conn)
            finally:
                conn.set_trace_callback(None)
                in_tx["flag"] = False

        return real_atomic(_wrapped)

    monkeypatch.setattr(manager, "execute_atomic", _traced_atomic)
    _arm(clock)
    audit.insert_events([_login()])
    assert in_tx["projections_in_tx"] == 0
    statements = in_tx["statements"]
    assert isinstance(statements, list)
    siem = [s for s in statements if "siem" in s.lower()]
    assert siem and all(
        s.startswith(("INSERT INTO siem_delivery_queue", "SAVEPOINT", "RELEASE"))
        for s in siem
    ), siem
