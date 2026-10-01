"""Shared helpers for the audit capture tests.

Every helper works against a REAL AuditLogService on a real temporary
SQLite file.  ``ThreadRecordingAuditLogService`` is the real service with one
addition: it records which thread performed each ``insert_events`` call, so a
test can prove where a write happened.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import AuditEvent
from code_indexer.server.services.audit_log_service import AuditLogService


class ThreadRecordingAuditLogService(AuditLogService):
    """Real AuditLogService that records the thread of every insert."""

    def __init__(self, db_path: Path, **kwargs) -> None:
        self.insert_threads: List[int] = []
        super().__init__(db_path, **kwargs)

    def insert_events(self, events: Sequence[AuditEvent]) -> None:
        self.insert_threads.append(threading.get_ident())
        super().insert_events(events)


def bind_service(
    service: AuditLogService, node_id: Optional[str] = None
) -> AuditLogService:
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(service, node_id=node_id)
    return service


def unbind(service: Optional[AuditLogService] = None) -> None:
    audit_capture.clear_audit_service()
    audit_capture.reset_server_process_mark()
    if service is not None:
        service.stop()


def bound_started_service(
    db_path: Path, node_id: Optional[str] = None
) -> Iterator[ThreadRecordingAuditLogService]:
    service = ThreadRecordingAuditLogService(db_path)
    service.start()
    bind_service(service, node_id=node_id)
    try:
        yield service
    finally:
        unbind(service)


def rows(db_path: Path, where: str = "1=1", params: Tuple = ()) -> List[Tuple]:
    """Read audit rows through a SEPARATE connection (no shared cache)."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT action_type, admin_id, outcome, source, correlation_id, "
            "node_id, actor_is_system, event_uuid, details FROM audit_logs "
            f"WHERE {where} ORDER BY id",
            params,
        ).fetchall()
    finally:
        conn.close()
