"""The event loop is never blocked by a DURABLE audit write.

``async def`` emitters use the ``_async`` variants, which run the whole
capture on a worker thread.  A missed offload (a DURABLE capture made
directly on a running loop) is logged at ERROR and routed to the writer
thread instead of blocking the loop.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import SystemComponent, build_event

from _audit_capture_support import bound_started_service, rows

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


@pytest.fixture(autouse=True)
def fresh_reporter(monkeypatch):
    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "groups.db"


@pytest.fixture()
def service(db_path: Path):
    yield from bound_started_service(db_path)


_KWARGS = dict(
    actor="alice",
    action_type="user_deleted",
    target_type="user",
    target_id="bob",
    outcome="success",
    details={"deleted_role": "normal_user"},
)


async def test_durable_capture_on_the_loop_is_routed_to_the_writer(
    service, db_path, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    loop_thread = threading.get_ident()
    before = audit_capture.records_dropped_since_boot()
    audit_capture.capture(**_KWARGS)  # type: ignore[arg-type]
    assert loop_thread not in service.insert_threads
    service.flush()
    assert [r[0] for r in rows(db_path)] == ["user_deleted"]
    assert loop_thread not in service.insert_threads
    assert audit_capture.records_dropped_since_boot() == before
    errors = [
        r.getMessage()
        for r in caplog.records
        if r.name == CAPTURE_LOGGER and r.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    assert errors[0].startswith(audit_capture.ON_EVENT_LOOP)


async def test_capture_async_writes_durably_from_a_worker_thread(
    service, db_path
) -> None:
    loop_thread = threading.get_ident()
    await audit_capture.capture_async(**_KWARGS)  # type: ignore[arg-type]
    # Durable: visible without flushing the writer.
    assert [r[0] for r in rows(db_path)] == ["user_deleted"]
    assert len(service.insert_threads) == 1
    assert service.insert_threads[0] != loop_thread


async def test_capture_system_async_writes_from_a_worker_thread(
    service, db_path
) -> None:
    await audit_capture.capture_system_async(
        component=SystemComponent.MCP_SELF_REGISTRATION,
        action_type="mcp_credential_created",
        target_type="mcp_credential",
        target_id="cred-1",
        outcome="success",
        details={"credential_id": "cred-1", "for_self": False},
    )
    found = rows(db_path)
    assert found[0][1] == "system:mcp-self-registration"
    assert service.insert_threads[0] != threading.get_ident()


async def test_record_async_writes_from_a_worker_thread(service, db_path) -> None:
    event = build_event(**_KWARGS)  # type: ignore[arg-type]
    await audit_capture.record_async(event)
    assert rows(db_path)[0][7] == event.event_uuid
    assert service.insert_threads[0] != threading.get_ident()


async def test_async_capture_never_raises_on_invalid_event(service) -> None:
    before = audit_capture.records_dropped_since_boot()
    await audit_capture.capture_async(
        actor="",
        action_type="user_deleted",
        target_type="user",
        target_id="bob",
        outcome="success",
    )
    assert audit_capture.records_dropped_since_boot() == before + 1
