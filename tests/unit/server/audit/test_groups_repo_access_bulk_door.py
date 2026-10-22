"""POST /admin/groups/repo-access/bulk through the real Web front door.

Real session cookie, real signed CSRF cookie, real GroupAccessManager,
GoldenRepoManager and bound audit store (``front_door_env``).  Elevation is
enforced for real in the refusal test.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest
from fastapi import Response

from _audit_front_doors import ACTING_ADMIN, front_door_env
from _audit_repos_support import register_repo
from code_indexer.server.auth.user_manager import UserRole
from tests.unit.server.self_service_elevation_harness import enforcement

_PATH = "/admin/groups/repo-access/bulk"
_CSRF = "example-csrf-token"
_REPOS = ["example-alpha", "example-beta", "example-gamma"]
_MEMBER = "example-member"

# ^ perf harness first-touch app build (~15 s under load) + PG migrations exceed the default per-test timeout
pytestmark = pytest.mark.timeout(60)


def _signed_csrf_cookie(token: str) -> str:
    from code_indexer.server.web.routes import CSRF_COOKIE_NAME, set_csrf_cookie

    resp = Response()
    set_csrf_cookie(resp, token)
    header = str(resp.headers["set-cookie"])
    prefix = f"{CSRF_COOKIE_NAME}="
    assert header.startswith(prefix), header
    return header[len(prefix) :].split(";", 1)[0]


@pytest.fixture()
def door(tmp_path: Path, monkeypatch) -> Iterator[SimpleNamespace]:
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.web.group_repo_access_routes import (
        group_repo_access_web_router,
    )
    from code_indexer.server.web.routes import CSRF_COOKIE_NAME

    for env in front_door_env(tmp_path, monkeypatch):
        env.client.app.include_router(group_repo_access_web_router, prefix="/admin")
        for alias in _REPOS:
            register_repo(env.manager, alias)
            app_module.app.state.backend_registry.global_repos.register_repo(
                alias_name=f"{alias}-global",
                repo_name=alias,
                repo_url="https://git.example.com/org/example.git",
                index_path=str(tmp_path / alias),
            )
        groups = app_module.app.state.group_manager
        group = groups.create_group("example-team", "")
        member = env.stack.user_manager.create_user(
            _MEMBER, "Quiet-Harbor-Lantern-42!", UserRole.NORMAL_USER
        )
        groups.assign_user_to_group(_MEMBER, group.id, "setup")
        cookie = _signed_csrf_cookie(_CSRF)

        def post(body: Dict[str, Any], token: str = _CSRF, session: Any = None) -> Any:
            env.client.cookies.set(CSRF_COOKIE_NAME, cookie)
            headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": token}
            if session is None:
                return env.web("POST", _PATH, json=body, headers=headers)
            env.client.cookies.set("session", session)
            try:
                return env.client.post(_PATH, json=body, headers=headers)
            finally:
                env.client.cookies.clear()

        yield SimpleNamespace(
            env=env, groups=groups, group=group, member=member, post=post
        )


def _body(door, action: str, names: List[str], group_id: Any = None) -> Dict:
    gid = door.group.id if group_id is None else group_id
    return {"group_id": gid, "action": action, "repo_names": names}


def _granted(door) -> List[str]:
    """Stored grants by name; get_group_repos always prepends cidx-meta."""
    names: List[str] = list(door.groups.get_group_repos(door.group.id)[1:])
    return names


def _visible_to_member(door) -> List[str]:
    from code_indexer.server.mcp.protocol import process_jsonrpc_request

    response = asyncio.run(
        process_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_global_repos", "arguments": {}},
            },
            door.member,
            session_id=None,
        )
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["success"] is True, payload
    names: List[str] = sorted(str(r["repo_name"]) for r in payload["repos"])
    return names


class TestBulkGrantAndRevoke:
    def test_grant_reports_changed_rejected_and_audits_each(self, door) -> None:
        names = ["example-alpha", "example-beta", "example-alpha", "cidx-meta"]
        resp = door.post(_body(door, "grant", names + ["example-unknown"]))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "success": True,
            "changed": ["example-alpha", "example-beta"],
            "rejected": [
                {"repo_name": "cidx-meta", "reason": "always accessible"},
                {
                    "repo_name": "example-unknown",
                    "reason": "not a registered repository",
                },
            ],
            "unchanged": [],
        }
        assert _granted(door) == ["example-alpha", "example-beta"]
        rows = door.env.rows("repo_access_grant")
        assert [(r.actor, r.source, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, "web", "success", alias)
            for alias in ("example-alpha", "example-beta")
        ]
        assert _visible_to_member(door) == ["example-alpha", "example-beta"]

    def test_regrant_changes_nothing_and_writes_no_row(self, door) -> None:
        assert door.post(_body(door, "grant", _REPOS)).status_code == 200
        before = len(door.env.rows("repo_access_grant"))
        resp = door.post(_body(door, "grant", _REPOS))
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] == []
        assert resp.json()["unchanged"] == _REPOS
        assert len(door.env.rows("repo_access_grant")) == before

    def test_revoke_mirrors_grant(self, door) -> None:
        assert door.post(_body(door, "grant", _REPOS)).status_code == 200
        resp = door.post(_body(door, "revoke", ["example-alpha", "example-gamma"]))
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] == ["example-alpha", "example-gamma"]
        assert _granted(door) == ["example-beta"]
        rows = door.env.rows("repo_access_revoke")
        assert [(r.source, r.outcome, r.target_id) for r in rows] == [
            ("web", "success", "example-alpha"),
            ("web", "success", "example-gamma"),
        ]
        assert _visible_to_member(door) == ["example-beta"]

    def test_no_size_cap(self, door) -> None:
        unknown = [f"example-missing-{n}" for n in range(1200)]
        resp = door.post(_body(door, "grant", unknown + _REPOS))
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] == _REPOS
        assert len(resp.json()["rejected"]) == 1200


class TestRefusals:
    def _refused(self, door, resp, status: int) -> None:
        assert resp.status_code == status, resp.text
        assert _granted(door) == []
        assert door.env.rows("repo_access_grant") == []

    def test_admins_group_is_refused(self, door) -> None:
        admins = door.groups.get_group_by_name("admins")
        self._refused(door, door.post(_body(door, "grant", _REPOS, admins.id)), 400)

    def test_unknown_group_is_not_found(self, door) -> None:
        self._refused(door, door.post(_body(door, "grant", _REPOS, 987654)), 404)

    def test_group_deleted_mid_request_is_not_found(self, door, monkeypatch) -> None:
        groups = door.groups
        original = groups.grant_repos_access_audited

        def delete_then_grant(names, group_id, *, actor):  # type: ignore[no-untyped-def]
            # The group disappears after the route's own lookup.
            groups.remove_user_from_group(_MEMBER, group_id)
            groups.delete_group(group_id)
            return original(names, group_id, actor=actor)

        monkeypatch.setattr(groups, "grant_repos_access_audited", delete_then_grant)
        self._refused(door, door.post(_body(door, "grant", _REPOS)), 404)

    @pytest.mark.parametrize(
        "body",
        [
            {"group_id": 1, "action": "delete", "repo_names": _REPOS},
            {"group_id": "1", "action": "grant", "repo_names": _REPOS},
            {"group_id": 1, "action": "grant", "repo_names": "example-alpha"},
            {"group_id": 1, "action": "grant", "repo_names": [7]},
        ],
    )
    def test_malformed_body_is_refused(self, door, body) -> None:
        self._refused(door, door.post(body), 400)

    def test_invalid_csrf_token_is_refused(self, door) -> None:
        self._refused(door, door.post(_body(door, "grant", _REPOS), token="x"), 403)

    def test_non_admin_session_is_refused(self, door) -> None:
        session = door.env.stack.session_cookie(door.member)
        resp = door.post(_body(door, "grant", _REPOS), session=session)
        assert resp.status_code in (401, 403), resp.text
        assert _granted(door) == []

    def test_missing_elevation_is_refused(self, door) -> None:
        with enforcement(True):
            resp = door.post(_body(door, "grant", _REPOS))
        self._refused(door, resp, 403)


def _sqlite_poison(door, event: str, row: str) -> None:
    """A real trigger aborting the statement that touches example-gamma."""
    import sqlite3

    conn = sqlite3.connect(str(door.groups.db_path))
    try:
        conn.execute(
            f"CREATE TRIGGER poison BEFORE {event} ON repo_group_access "
            f"WHEN {row}.repo_name = 'example-gamma' "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        conn.commit()
    finally:
        conn.close()


class TestDatabaseFailure:
    def test_failed_bulk_grant_changes_nothing_and_writes_no_row(self, door) -> None:
        _sqlite_poison(door, "INSERT", "NEW")
        resp = door.post(_body(door, "grant", _REPOS))
        assert resp.status_code == 500, resp.text
        assert _granted(door) == []
        assert door.env.rows("repo_access_grant") == []

    def test_failed_bulk_revoke_changes_nothing_and_writes_no_row(self, door) -> None:
        for alias in _REPOS:
            door.groups.grant_repo_access(alias, door.group.id, "setup")
        _sqlite_poison(door, "DELETE", "OLD")
        resp = door.post(_body(door, "revoke", _REPOS))
        assert resp.status_code == 500, resp.text
        assert _granted(door) == _REPOS
        assert door.env.rows("repo_access_revoke") == []


_PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture()
def pg_door(door, monkeypatch) -> Iterator[SimpleNamespace]:
    """The same front door with the groups store on a fresh PostgreSQL DB."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg_pool import ConnectionPool

    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )
    from code_indexer.server.services.group_access_manager import GroupAccessManager
    from code_indexer.server.storage.postgres.groups_backend import (
        GroupsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"bulk_door_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(_PG_DSN)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    pool = None
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True)
        groups = GroupAccessManager(
            Path(str(door.env.server_dir)) / "unused.db",
            storage_backend=GroupsPostgresBackend(pool),
        )
        state = app_module.app.state
        monkeypatch.setattr(state, "group_manager", groups)
        monkeypatch.setattr(
            state, "access_filtering_service", AccessFilteringService(groups)
        )
        group = groups.create_group("example-team", "")
        groups.assign_user_to_group(_MEMBER, group.id, "setup")
        yield SimpleNamespace(
            env=door.env,
            groups=groups,
            group=group,
            member=door.member,
            post=door.post,
            dsn=dsn,
        )
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(_PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.mark.skipif(not _PG_DSN, reason="set TEST_POSTGRES_DSN to enable")
class TestPostgresGroupsBackend:
    def test_grant_regrant_and_revoke(self, pg_door) -> None:
        door = pg_door
        resp = door.post(_body(door, "grant", _REPOS + ["cidx-meta"]))
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] == _REPOS
        again = door.post(_body(door, "grant", _REPOS))
        assert again.json()["changed"] == [] and again.json()["unchanged"] == _REPOS
        grant_rows = door.env.rows("repo_access_grant")
        assert [r.target_id for r in grant_rows] == _REPOS
        assert _visible_to_member(door) == _REPOS
        resp = door.post(_body(door, "revoke", _REPOS[:2]))
        assert resp.json()["changed"] == _REPOS[:2]
        assert _granted(door) == _REPOS[2:]
        assert _visible_to_member(door) == _REPOS[2:]

    def test_database_failure_changes_nothing(self, pg_door) -> None:
        import psycopg

        door = pg_door
        with psycopg.connect(door.dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE FUNCTION poison() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.repo_name = 'example-gamma' THEN "
                "RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$"
            )
            conn.execute(
                "CREATE TRIGGER poison BEFORE INSERT ON repo_group_access "
                "FOR EACH ROW EXECUTE FUNCTION poison()"
            )
        resp = door.post(_body(door, "grant", _REPOS))
        assert resp.status_code == 500, resp.text
        assert _granted(door) == []
        assert door.env.rows("repo_access_grant") == []
