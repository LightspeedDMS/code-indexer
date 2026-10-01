"""Shared helpers for the MFA, elevation and login-door audit parity tests.

Every helper works against a REAL, started ``AuditLogService`` on a real
temporary SQLite file, bound as the process audit sink exactly as the server
lifespan binds it.  Rows are read back through a SEPARATE connection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import AuditEvent
from code_indexer.server.services.audit_log_service import AuditLogService

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


@dataclass(frozen=True)
class Row:
    action_type: str
    actor: str
    target_type: str
    target_id: str
    outcome: Optional[str]
    source: Optional[str]
    auth_method: Optional[str]
    details: Dict[str, Any]


class _ThreadRecordingService(AuditLogService):
    """The real service; also records the thread of every durable insert."""

    def __init__(self, db_path: Path) -> None:
        self.insert_threads: List[int] = []
        super().__init__(db_path)

    def insert_events(self, events: Sequence[AuditEvent]) -> None:
        self.insert_threads.append(threading.get_ident())
        super().insert_events(events)


class AuditStore:
    """A real audit store bound as this process's audit sink."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.service = _ThreadRecordingService(db_path)

    def rows(self, *action_types: str) -> List[Row]:
        """Rows (oldest first), optionally only the given action types."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            fetched = conn.execute(
                "SELECT action_type, admin_id, target_type, target_id, outcome, "
                "source, auth_method, details FROM audit_logs ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        result = [
            Row(
                action_type=r[0],
                actor=r[1],
                target_type=r[2],
                target_id=r[3],
                outcome=r[4],
                source=r[5],
                auth_method=r[6],
                details=json.loads(r[7]) if r[7] else {},
            )
            for r in fetched
        ]
        if action_types:
            result = [r for r in result if r.action_type in action_types]
        return result

    def all_raw_text(self) -> str:
        """Every stored column of every row, concatenated (secret scans)."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            fetched = conn.execute("SELECT * FROM audit_logs").fetchall()
        finally:
            conn.close()
        return "\n".join(" ".join(str(v) for v in row) for row in fetched)


def bound_audit_store(db_path: Path) -> Iterator[AuditStore]:
    """Yield a started store bound as the audit sink; unbind afterwards."""
    store = AuditStore(db_path)
    store.service.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(store.service, node_id=None)
    try:
        yield store
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        store.service.stop()


def capture_errors(caplog: Any) -> List[str]:
    """ERROR lines the capture path logged (drops, loop misuse, rejections)."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == CAPTURE_LOGGER and r.levelno >= logging.ERROR
    ]
