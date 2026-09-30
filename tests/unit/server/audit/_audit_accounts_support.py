"""Shared helpers for the account / credential / permission audit tests.

Every helper works against a REAL, started ``AuditLogService`` on a real
temporary SQLite file, bound as the process audit sink exactly as the server
lifespan binds it.  Rows are read back through a SEPARATE connection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"


@dataclass(frozen=True)
class AuditRow:
    action_type: str
    actor: str
    target_type: str
    target_id: str
    outcome: Optional[str]
    source: Optional[str]
    auth_method: Optional[str]
    actor_is_system: int
    correlation_id: Optional[str]
    details: Dict[str, Any]
    raw_details: Optional[str]


class AuditStore:
    """A real audit store bound as this process's audit sink."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.service = AuditLogService(db_path)

    def rows(self, action_prefix: str = "") -> List[AuditRow]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            fetched = conn.execute(
                "SELECT action_type, admin_id, target_type, target_id, outcome, "
                "source, auth_method, actor_is_system, correlation_id, details "
                "FROM audit_logs WHERE action_type LIKE ? ORDER BY id",
                (f"{action_prefix}%",),
            ).fetchall()
        finally:
            conn.close()
        return [
            AuditRow(
                action_type=r[0],
                actor=r[1],
                target_type=r[2],
                target_id=r[3],
                outcome=r[4],
                source=r[5],
                auth_method=r[6],
                actor_is_system=r[7],
                correlation_id=r[8],
                details=json.loads(r[9]) if r[9] else {},
                raw_details=r[9],
            )
            for r in fetched
        ]

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


def make_user_manager(tmp_path: Path):
    """A real SQLite-backed UserManager on a freshly initialized schema."""
    from code_indexer.server.auth.user_manager import UserManager
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path=db_path).initialize_database()
    return UserManager(use_sqlite=True, db_path=db_path)


def capture_errors(caplog, *, phases: Optional[List[str]] = None) -> List[str]:
    """ERROR lines the capture path logged (drops, loop misuse, rejections).

    By default reads the current phase's records; pass *phases* (e.g.
    ``["setup", "call"]``) when checking from a fixture's teardown, where
    ``caplog.records`` holds only teardown records.
    """
    records = (
        [r for phase in phases for r in caplog.get_records(phase)]
        if phases is not None
        else caplog.records
    )
    return [
        r.getMessage()
        for r in records
        if r.name == CAPTURE_LOGGER and r.levelno >= logging.ERROR
    ]
