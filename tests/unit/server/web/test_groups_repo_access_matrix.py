"""Groups > Repository Access matrix: masked clone-URL hover and column controls.

Renders the real groups page and repo-access partial over a real
GroupAccessManager and a real GoldenRepoManager.  Only the admin-session
lookup is replaced (the session boundary is covered by the route tests).
"""

from __future__ import annotations

import html
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.services.group_access_manager import GroupAccessManager

_TOKEN = "example-token-0123456789"
_REPOS = {
    "billing": "https://git.example.com/team-a/billing.git",
    "billing-2": "https://git.example.com/team-b/billing.git",
    "ledger": f"https://user:{_TOKEN}@git.example.com/team-a/ledger.git",
}
_MASKED_LEDGER = "https://***@git.example.com/team-a/ledger.git"
_PAGES = ("/admin/groups", "/admin/partials/groups-repo-access")


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[Any]:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )
    from code_indexer.server.web.routes import web_router

    groups = GroupAccessManager(tmp_path / "groups.db")
    groups.create_group("billing-team", "")
    golden = GoldenRepoManager(data_dir=str(tmp_path / "golden-data"))
    for alias, url in _REPOS.items():
        golden._sqlite_backend.add_repo(
            alias=alias,
            repo_url=url,
            default_branch="main",
            clone_path=str(tmp_path / alias),
            created_at=datetime.now(timezone.utc).isoformat(),
            enable_temporal=False,
            temporal_options=None,
        )
    from code_indexer.server.web.auth import init_session_manager

    # A real session manager: set_csrf_cookie signs the page's CSRF cookie.
    init_session_manager(
        secret_key=secrets.token_hex(32),
        config=SimpleNamespace(host="127.0.0.1"),
        web_security_config=SimpleNamespace(
            web_session_timeout_seconds=3600, admin_session_timeout_seconds=3600
        ),
    )
    session = MagicMock(username="example-admin", role="admin")
    app = FastAPI()
    app.include_router(web_router, prefix="/admin")
    with (
        patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=session,
        ),
        patch("code_indexer.server.web.routes._get_group_manager", return_value=groups),
        patch(
            "code_indexer.server.web.routes._get_golden_repo_manager",
            return_value=golden,
        ),
    ):
        yield TestClient(app), groups


def _row(page: str, alias: str) -> str:
    match = re.search(
        rf'<tr data-repo-alias="{re.escape(alias)}".*?</tr>', page, re.DOTALL
    )
    assert match, f"no row for {alias}"
    return match.group(0)


@pytest.mark.parametrize("path", _PAGES)
def test_repository_cell_hovers_the_masked_clone_url(client, path) -> None:
    test_client, _groups = client
    resp = test_client.get(path)
    assert resp.status_code == 200, resp.text[:300]
    page = html.unescape(resp.text)
    assert _TOKEN not in page
    for alias, url in (
        ("billing", _REPOS["billing"]),
        ("billing-2", _REPOS["billing-2"]),
        ("ledger", _MASKED_LEDGER),
    ):
        row = _row(page, alias)
        assert f'data-repo-url="{url}"' in row, row
        assert f'title="{url}"' in row, row


@pytest.mark.parametrize("path", _PAGES)
def test_every_group_but_admins_has_bulk_controls(client, path) -> None:
    test_client, groups = client
    page = test_client.get(path).text
    for group in groups.get_all_groups():
        controls = re.findall(
            rf'<button[^>]*class="bulk-btn"[^>]*data-group="{group.id}"[^>]*>',
            page,
        )
        if group.name == "admins":
            assert controls == []
            continue
        actions = sorted(re.findall(r'data-action="(\w+)"', " ".join(controls)))
        assert actions == ["grant", "revoke"], (group.name, controls)
        assert all(f'data-group-name="{group.name}"' in c for c in controls)
    assert ">All Yes<" in page and ">All No<" in page


def test_matrix_has_filter_box_count_and_script(client) -> None:
    test_client, _groups = client
    page = test_client.get("/admin/groups").text
    assert 'id="repo-access-filter"' in page
    assert 'id="repo-access-count"' in page
    assert 'id="repo-access-status"' in page
    assert "/admin/static/js/groups_repo_access.js" in page
