"""Web repo-access grant/revoke write their audit rows off the event loop.

Both routes are ``async def``; their audit write is a durable (synchronous)
insert, so it must run in a worker thread.  Drives the real Web routes with
a real GroupAccessManager and a real, started AuditLogService on a temporary
SQLite file.  Only the session and CSRF plumbing is patched, as in the
existing Web route tests.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.group_access_manager import GroupAccessManager

CAPTURE_LOGGER = "code_indexer.server.services.audit_capture"
_CSRF = "example-csrf-token"
_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


class _Env:
    def __init__(self, client: TestClient, manager: GroupAccessManager, db: Path):
        self.client = client
        self.manager = manager
        self.db = db

    def rows(self) -> List[Tuple]:
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(
                "SELECT action_type, admin_id, target_id, source FROM audit_logs "
                "WHERE action_type LIKE 'repo_access_%' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def post(self, action: str, group_id: int):
        return self.client.post(
            f"/admin/groups/repo-access/{action}",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": _CSRF,
                "Content-Type": "application/json",
            },
            json={"repo_name": "example-repo", "group_id": group_id},
        )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[_Env]:
    from code_indexer.server.web.routes import web_router

    monkeypatch.setattr(audit_capture, "_reporter", audit_capture._DropReporter())
    db = tmp_path / "groups.db"
    manager = GroupAccessManager(db)
    service = AuditLogService(db)
    service.start()
    manager.set_audit_service(service)

    from code_indexer.server.middleware.audit_request_context import (
        AuditRequestContextMiddleware,
    )

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.include_router(web_router, prefix="/admin")
    for route in web_router.routes:
        if isinstance(route, APIRoute):
            for dep in route.dependencies or []:
                fn = getattr(dep, "dependency", None)
                if fn and getattr(fn, "__qualname__", "") == _ELEVATION_QUALNAME:
                    app.dependency_overrides[fn] = lambda: None
    session = MagicMock()
    session.username = "example-admin"
    session.role = "admin"
    patches = {
        "_require_admin_session": session,
        "_get_group_manager": manager,
        "get_csrf_token_from_cookie": _CSRF,
    }
    try:
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(
                    patch(f"code_indexer.server.web.routes.{name}", return_value=value)
                )
            yield _Env(TestClient(app), manager, db)
    finally:
        service.stop()


def _loop_errors(caplog) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == CAPTURE_LOGGER
        and r.getMessage().startswith(audit_capture.ON_EVENT_LOOP)
    ]


def test_grant_writes_its_row_off_the_event_loop(env, caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    group = env.manager.create_group(name="example-group", description="")
    assert env.post("grant", group.id).status_code == 200
    assert _loop_errors(caplog) == []
    # The request's attribution crosses the thread hop (source=web).
    assert env.rows() == [("repo_access_grant", "example-admin", "example-repo", "web")]


def test_revoke_writes_its_row_off_the_event_loop(env, caplog) -> None:
    group = env.manager.create_group(name="example-group", description="")
    assert env.post("grant", group.id).status_code == 200
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    caplog.clear()
    assert env.post("revoke", group.id).status_code == 200
    assert _loop_errors(caplog) == []
    assert [r[0] for r in env.rows()] == ["repo_access_grant", "repo_access_revoke"]
