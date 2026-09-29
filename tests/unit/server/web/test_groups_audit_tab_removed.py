"""The Group Management page no longer has its own audit tab.

Audit reading lives on the Audit Logs page (``/admin/audit-logs``), MCP
``query_audit_logs`` and REST ``GET /api/v1/audit-logs``, all through one
shared read function.  One real app per module: a fresh server data
directory, the real lifespan and a real Web login.

The Groups page resolves its ``GroupAccessManager`` and
``GoldenRepoManager`` from the module-level ``code_indexer.server.app.app``
(the object uvicorn serves in production), not from ``request.app``.  A
``create_app()`` instance is a different object, so the fixture points the
module app's ``group_manager`` and ``golden_repo_manager`` at the managers
the real lifespan built, and restores them afterwards.  The module app is
resolved BEFORE the fixture's own ``create_app()`` under the patched data
directory: resolving it lazily afterwards would run a second
``create_app()`` against the same data directory and wait on the primary
instance lock the first app already holds.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request


_MODULE_STATE_MANAGERS = ("group_manager", "golden_repo_manager")


@pytest.fixture(scope="module")
def page_app(tmp_path_factory) -> Iterator[Any]:
    import code_indexer.server.app as app_module

    # Resolve the module app first, under the session's own data directory.
    module_state = app_module.app.state
    data_dir = tmp_path_factory.mktemp("groups_tab_server")
    env = {
        "CIDX_SERVER_DATA_DIR": str(data_dir),
        "CIDX_DATA_DIR": str(data_dir / "cidx"),
    }
    with patch.dict("os.environ", env):
        from code_indexer.server.services.config_service import reset_config_service

        reset_config_service()
        app = app_module.create_app()
        with TestClient(app, follow_redirects=False) as client:
            sentinel = object()
            previous = {
                name: getattr(module_state, name, sentinel)
                for name in _MODULE_STATE_MANAGERS
            }
            for name in _MODULE_STATE_MANAGERS:
                setattr(module_state, name, getattr(app.state, name))
            try:
                yield app, client
            finally:
                for name, value in previous.items():
                    if value is sentinel:
                        delattr(module_state, name)
                    else:
                        setattr(module_state, name, value)
        reset_config_service()


@pytest.fixture(scope="module")
def admin_client(page_app) -> TestClient:
    _, client = page_app
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    login = client.post(
        "/login",
        data={"username": "admin", "password": "admin", "csrf_token": match.group(1)},
    )
    assert login.status_code == 303
    client_: TestClient = client
    return client_


def test_groups_page_has_no_audit_tab_section_or_handlers(admin_client):
    page = admin_client.get("/admin/groups")
    assert page.status_code == 200
    html = page.text
    for gone in (
        'id="tab-audit"',
        'id="content-audit"',
        "switchTab('audit')",
        "groups-audit-logs",
        "filterAuditLogs",
        "clearAuditFilters",
        "audit-logs-section",
        "audit-filter-form",
    ):
        assert gone not in html, gone
    for kept in ('id="content-groups"', 'id="content-users"', 'id="content-repos"'):
        assert kept in html, kept


def test_old_audit_partial_url_is_gone(admin_client):
    assert admin_client.get("/admin/partials/groups-audit-logs").status_code == 404


def test_old_audit_partial_template_is_deleted():
    from code_indexer.server.web import routes

    templates_dir = Path(routes.__file__).parent / "templates" / "partials"
    assert templates_dir.is_dir()
    assert (templates_dir / "audit_logs_table.html").exists()  # the new page's
    assert not (templates_dir / "groups_audit_logs.html").exists()


def test_a_stale_audit_tab_renders_the_groups_tab(page_app):
    from code_indexer.server.web.auth import SessionData
    from code_indexer.server.web.routes import _create_groups_page_response

    app, _ = page_app
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/groups",
            "headers": [],
            "query_string": b"",
            "app": app,
        }
    )
    session = SessionData(
        username="admin", role="admin", csrf_token="x", created_at=time.time()
    )
    response = _create_groups_page_response(request, session, active_tab="audit")
    html = bytes(response.body).decode("utf-8")
    section = re.search(r'<section id="content-groups" class="([^"]*)"', html)
    assert section and "hidden" not in section.group(1).split()
    groups_link = re.search(r'<a[^>]*id="tab-groups"[^>]*class="([^"]*)"', html, re.S)
    assert groups_link and "active" in groups_link.group(1).split()
    assert 'id="content-audit"' not in html


def test_old_read_methods_are_removed():
    from code_indexer.server.services.audit_log_service import AuditLogService
    from code_indexer.server.services.group_access_manager import GroupAccessManager
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.protocols.audit_log_backend import (
        AuditLogBackend,
    )

    assert not hasattr(GroupAccessManager, "get_audit_logs")
    for cls in (AuditLogService, AuditLogPostgresBackend, AuditLogBackend):
        assert not hasattr(cls, "query"), cls
