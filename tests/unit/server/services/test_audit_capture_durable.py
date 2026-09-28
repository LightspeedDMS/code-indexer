"""DURABLE delivery: a security event is committed before capture returns."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import (
    SystemComponent,
    build_event,
)
from code_indexer.server.telemetry.correlation_bridge import (
    _correlation_id_var,
)

from _audit_capture_support import bound_started_service, rows


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "groups.db"


@pytest.fixture()
def service(db_path: Path):
    yield from bound_started_service(db_path, node_id="node-a")


def _capture_user_deleted(target: str = "bob") -> None:
    audit_capture.capture(
        actor="alice",
        action_type="user_deleted",
        target_type="user",
        target_id=target,
        outcome="success",
        details={"deleted_role": "normal_user"},
    )


def test_durable_row_is_readable_before_capture_returns(service, db_path) -> None:
    _capture_user_deleted()
    # No flush(): the writer thread is running, so only a synchronous write
    # on this thread can make the row visible to a separate connection now.
    found = rows(db_path)
    assert len(found) == 1
    row = found[0]
    action, actor, outcome, source, _, node_id, is_system, event_uuid, details = row
    assert (action, actor, outcome, source) == (
        "user_deleted",
        "alice",
        "success",
        "system",
    )
    assert node_id == "node-a"
    assert is_system == 0
    assert event_uuid
    assert details == '{"deleted_role": "normal_user"}'


def test_durable_write_runs_on_the_callers_thread(service) -> None:
    _capture_user_deleted()
    assert service.insert_threads == [threading.get_ident()]


def test_record_of_a_prebuilt_event_is_durable(service, db_path) -> None:
    event = build_event(
        actor="alice",
        action_type="user_email_changed",
        target_type="user",
        target_id="bob",
        outcome="success",
    )
    audit_capture.record(event)
    assert rows(db_path)[0][7] == event.event_uuid


def test_events_in_one_request_share_correlation_but_not_uuid(service, db_path) -> None:
    token = _correlation_id_var.set("req-123")
    try:
        _capture_user_deleted("bob")
        _capture_user_deleted("carol")
    finally:
        _correlation_id_var.reset(token)
    first, second = rows(db_path)
    assert first[4] == second[4] == "req-123"
    assert first[7] != second[7]


def test_system_event_is_written_with_the_trusted_flag(service, db_path) -> None:
    audit_capture.capture_system(
        component=SystemComponent.SELF_REGISTRATION,
        action_type="user_created",
        target_type="user",
        target_id="newbie",
        outcome="success",
        details={"role": "normal_user", "provisioning": "self_registration"},
    )
    found = rows(db_path)
    assert found[0][1] == "system:self-registration"
    assert found[0][6] == 1


def test_nothing_is_counted_on_the_happy_path(service) -> None:
    before = audit_capture.records_dropped_since_boot()
    _capture_user_deleted()
    assert audit_capture.records_dropped_since_boot() == before


def test_bound_node_id_is_reported(service) -> None:
    assert audit_capture.audit_node_id() == "node-a"


def test_solo_nested_service_shape_writes_through_the_inner_service(
    tmp_path: Path,
) -> None:
    """Solo production shape: the lifespan's started service wraps the
    registry's (unstarted) AuditLogService on the same groups.db."""
    from code_indexer.server.services.audit_log_service import AuditLogService

    from _audit_capture_support import bind_service, unbind

    db_path = tmp_path / "groups.db"
    outer = AuditLogService(db_path, storage_backend=AuditLogService(db_path))
    outer.start()
    bind_service(outer)
    try:
        _capture_user_deleted()
        assert [r[0] for r in rows(db_path)] == ["user_deleted"]
        outer.log("admin", "group_create", "group", "7", None)
        outer.flush()
        assert [r[0] for r in rows(db_path)] == ["user_deleted", "group_create"]
    finally:
        unbind(outer)
