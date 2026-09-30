"""
MCP provider-index tools follow exactly the role and elevation rules of their
REST twins.

REST rules (``routers/provider_indexes.py`` and
``routers/inline_admin_ops.py``):

- every ``/api/admin/provider-indexes`` route is admin-only;
- ``POST /add``, ``/recreate`` and ``/remove`` additionally require an
  elevation window; the GET routes (providers, status) do not;
- ``POST /api/admin/golden-repos/{alias}/indexes`` is admin-only and
  requires an elevation window.

MCP equivalents under test: ``manage_provider_indexes`` (every action
admin-only; add/recreate/remove elevation-gated; list_providers/status not)
and ``add_golden_repo_index`` (admin-only, elevation-gated).

Every call goes through the real MCP ``tools/call`` dispatcher
(``handle_tools_call``). ``ElevatedSessionManager``, ``TOTPService``,
``AccessFilteringService`` and ``GroupAccessManager`` are real objects backed
by temporary SQLite databases. Only the golden-repo filesystem helpers, the
provider-index service, the job manager and the golden-repo manager are
replaced, so no real indexing runs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pyotp
import pytest
from cryptography.fernet import Fernet

from code_indexer.server import app as real_app_module
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.mcp.tools import filter_tools_by_role
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.constants import (
    DEFAULT_GROUP_ADMINS,
    DEFAULT_GROUP_POWERUSERS,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager

_DECORATOR = "code_indexer.server.mcp.auth.elevation_decorator"
_HANDLERS = "code_indexer.server.mcp.handlers"
_SESSION_KEY = "jti-provider-index-parity"
_REPO = "repo-a-global"
_MUTATIONS = ("add", "recreate", "remove")


def _user(username: str, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


class _Recorder:
    """Records job submissions and golden-repo index requests."""

    def __init__(self) -> None:
        self.jobs: List[Dict[str, Any]] = []
        self.golden_index_calls: List[Dict[str, Any]] = []
        self.config_writes: List[str] = []
        self.config_removals: List[str] = []

    def submit_job(self, **kwargs: Any) -> str:
        self.jobs.append(kwargs)
        return f"job-{len(self.jobs)}"

    def add_index_to_golden_repo(self, **kwargs: Any) -> str:
        self.golden_index_calls.append(kwargs)
        return "golden-job-1"

    def append_provider_to_config(self, path: str, provider_name: str) -> bool:
        self.config_writes.append(path)
        return True

    def remove_provider_from_config(self, path: str, provider_name: str) -> None:
        self.config_removals.append(path)


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        groups = GroupAccessManager(tmp_path / "groups.db")
        admins = groups.get_group_by_name(DEFAULT_GROUP_ADMINS)
        powerusers = groups.get_group_by_name(DEFAULT_GROUP_POWERUSERS)
        assert admins is not None and powerusers is not None
        groups.assign_user_to_group("ops-admin", admins.id, assigned_by="test")
        groups.assign_user_to_group("builder", powerusers.id, assigned_by="test")
        groups.grant_repo_access("repo-a", powerusers.id, granted_by="test")
        self.groups = groups
        self.groups_db = tmp_path / "groups.db"
        self.powerusers_id = powerusers.id
        # Group tool-access enforcement is off unless grant_tools() is called.
        self.tool_access_manager: Optional[GroupAccessManager] = None
        self.access = AccessFilteringService(group_access_manager=groups)
        self.esm = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "elevation.db"),
        )
        self.totp = TOTPService(
            db_path=str(tmp_path / "totp.db"),
            mfa_encryption_key=Fernet.generate_key().decode(),
        )
        for name in ("ops-admin", "builder"):
            secret = self.totp.generate_secret(name)
            assert self.totp.activate_mfa(name, pyotp.TOTP(secret).now())
        self.enforcement = True
        self.recorder = _Recorder()
        self.service = MagicMock()
        self.service.validate_provider.return_value = None
        self.service.list_providers.return_value = [{"name": "voyage-ai"}]
        self.service.get_provider_index_status.return_value = {}
        self.service.remove_provider_index.return_value = {
            "removed": True,
            "message": "removed",
            "collection_name": "voyage-ai-collection",
        }

    def open_window(self, username: str) -> None:
        self.esm.create(_SESSION_KEY, username, "127.0.0.1", scope="full")

    def grant_tools_to_powerusers(self, *tools: str) -> None:
        """Turn group tool-access enforcement on and grant tools to the
        powerusers group, so the dispatcher admits its members by grant."""
        with sqlite3.connect(str(self.groups_db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
                "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO tool_access_migration_state "
                "(id, complete) VALUES (1, 1)"
            )
            conn.commit()
        for tool in tools:
            self.groups.set_tool_access(tool, self.powerusers_id, True, "test")
        self.tool_access_manager = self.groups

    async def call(
        self,
        user: User,
        tool: str,
        arguments: Dict[str, Any],
        elevation_key: Optional[str] = _SESSION_KEY,
    ) -> Dict[str, Any]:
        recorder = self.recorder
        with (
            patch(f"{_HANDLERS}.app_module") as app_module,
            patch.object(
                real_app_module.app.state,
                "group_manager",
                self.tool_access_manager,
                create=True,
            ),
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                f"{_HANDLERS}._resolve_golden_repo_path",
                side_effect=lambda alias: f"/fake/golden-repos/{alias}",
            ),
            patch(
                f"{_HANDLERS}._resolve_golden_repo_base_clone",
                side_effect=lambda alias: f"/fake/golden-repos/{alias}/base",
            ),
            patch(
                f"{_HANDLERS}._append_provider_to_config",
                side_effect=recorder.append_provider_to_config,
            ),
            patch(
                f"{_HANDLERS}._remove_provider_from_config",
                side_effect=recorder.remove_provider_from_config,
            ),
            patch(
                "code_indexer.server.services.provider_index_service.ProviderIndexService",
                return_value=self.service,
            ),
            patch(
                f"{_DECORATOR}._is_elevation_enforcement_enabled",
                return_value=self.enforcement,
            ),
            patch(f"{_DECORATOR}.elevated_session_manager", self.esm),
            patch(f"{_DECORATOR}.get_totp_service", return_value=self.totp),
        ):
            app_module.app.state.access_filtering_service = self.access
            app_module.background_job_manager = recorder
            app_module.golden_repo_manager = recorder
            response = await handle_tools_call(
                {"name": tool, "arguments": arguments},
                user,
                elevation_key=elevation_key,
            )
        return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[_Env]:
    yield _Env(tmp_path)


def _mutation_args(action: str) -> Dict[str, Any]:
    return {"action": action, "provider": "voyage-ai", "repository_alias": _REPO}


ADMIN = _user("ops-admin", UserRole.ADMIN)
POWER_USER = _user("builder", UserRole.POWER_USER)


class TestManageProviderIndexesIsAdminOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arguments",
        [
            {"action": "list_providers"},
            {"action": "status", "repository_alias": _REPO},
            _mutation_args("add"),
            _mutation_args("recreate"),
            _mutation_args("remove"),
        ],
    )
    async def test_power_user_is_refused_for_every_action(self, env, arguments):
        env.open_window(POWER_USER.username)
        with pytest.raises(ValueError, match="Permission denied"):
            await env.call(POWER_USER, "manage_provider_indexes", arguments)
        assert env.recorder.jobs == []
        assert env.recorder.config_writes == []
        assert env.recorder.config_removals == []
        env.service.remove_provider_index.assert_not_called()

    def test_tool_is_not_listed_for_power_user(self):
        names = {tool["name"] for tool in filter_tools_by_role(POWER_USER)}
        assert "manage_provider_indexes" not in names
        admin_names = {tool["name"] for tool in filter_tools_by_role(ADMIN)}
        assert "manage_provider_indexes" in admin_names


class TestManageProviderIndexesElevation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", _MUTATIONS)
    async def test_admin_without_window_gets_elevation_required(self, env, action):
        data = await env.call(ADMIN, "manage_provider_indexes", _mutation_args(action))
        assert data.get("error") == "elevation_required"
        assert env.recorder.jobs == []
        assert env.recorder.config_writes == []
        assert env.recorder.config_removals == []
        env.service.remove_provider_index.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ("add", "recreate"))
    async def test_admin_with_window_submits_build_job(self, env, action):
        env.open_window(ADMIN.username)
        data = await env.call(ADMIN, "manage_provider_indexes", _mutation_args(action))
        assert data.get("success") is True
        assert data.get("action") == action
        assert len(env.recorder.jobs) == 1
        assert env.recorder.jobs[0]["clear"] is (action == "recreate")
        assert env.recorder.jobs[0]["submitter_username"] == ADMIN.username

    @pytest.mark.asyncio
    async def test_admin_with_window_removes_index(self, env):
        env.open_window(ADMIN.username)
        data = await env.call(
            ADMIN, "manage_provider_indexes", _mutation_args("remove")
        )
        assert data.get("success") is True
        env.service.remove_provider_index.assert_called_once()
        assert env.recorder.config_removals == [f"/fake/golden-repos/{_REPO}/base"]

    @pytest.mark.asyncio
    async def test_reads_need_no_window(self, env):
        listed = await env.call(
            ADMIN, "manage_provider_indexes", {"action": "list_providers"}
        )
        assert listed.get("success") is True
        assert listed.get("count") == 1

        status = await env.call(
            ADMIN,
            "manage_provider_indexes",
            {"action": "status", "repository_alias": _REPO},
        )
        assert status.get("success") is True
        assert status.get("repository_alias") == _REPO

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", _MUTATIONS)
    async def test_enforcement_off_keeps_admin_behaviour(self, env, action):
        env.enforcement = False
        data = await env.call(
            ADMIN,
            "manage_provider_indexes",
            _mutation_args(action),
            elevation_key=None,
        )
        assert data.get("success") is True
        listed = await env.call(
            ADMIN,
            "manage_provider_indexes",
            {"action": "list_providers"},
            elevation_key=None,
        )
        assert listed.get("success") is True


_ADMIN_ROLE_REQUIRED = "Permission denied: admin role required"

_ALL_ACTIONS = [
    {"action": "list_providers"},
    {"action": "status", "repository_alias": _REPO},
    _mutation_args("add"),
    _mutation_args("recreate"),
    _mutation_args("remove"),
]


class TestGroupGrantedNonAdminIsRefused:
    """A group tool grant admits a caller without the role check at the
    dispatcher; the handlers still require the admin role, as REST does."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_window", [False, True])
    @pytest.mark.parametrize("arguments", _ALL_ACTIONS)
    async def test_manage_provider_indexes_refuses_granted_power_user(
        self, env, arguments, with_window
    ):
        env.grant_tools_to_powerusers("manage_provider_indexes")
        if with_window:
            env.open_window(POWER_USER.username)

        data = await env.call(POWER_USER, "manage_provider_indexes", arguments)

        assert data == {"success": False, "error": _ADMIN_ROLE_REQUIRED}
        assert env.recorder.jobs == []
        assert env.recorder.config_writes == []
        assert env.recorder.config_removals == []
        env.service.list_providers.assert_not_called()
        env.service.get_provider_index_status.assert_not_called()
        env.service.remove_provider_index.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_window", [False, True])
    async def test_add_golden_repo_index_refuses_granted_power_user(
        self, env, with_window
    ):
        env.grant_tools_to_powerusers("add_golden_repo_index")
        if with_window:
            env.open_window(POWER_USER.username)

        data = await env.call(
            POWER_USER,
            "add_golden_repo_index",
            {"alias": "repo-a", "index_type": "fts"},
        )

        assert data == {"success": False, "error": _ADMIN_ROLE_REQUIRED}
        assert env.recorder.golden_index_calls == []


class TestAddGoldenRepoIndexMatchesRest:
    @pytest.mark.asyncio
    async def test_power_user_is_refused(self, env):
        with pytest.raises(ValueError, match="Permission denied"):
            await env.call(
                POWER_USER,
                "add_golden_repo_index",
                {"alias": "repo-a", "index_type": "fts"},
            )
        assert env.recorder.golden_index_calls == []

    @pytest.mark.asyncio
    async def test_admin_without_window_gets_elevation_required(self, env):
        data = await env.call(
            ADMIN, "add_golden_repo_index", {"alias": "repo-a", "index_type": "fts"}
        )
        assert data.get("error") == "elevation_required"
        assert env.recorder.golden_index_calls == []

    @pytest.mark.asyncio
    async def test_admin_with_window_succeeds(self, env):
        env.open_window(ADMIN.username)
        data = await env.call(
            ADMIN, "add_golden_repo_index", {"alias": "repo-a", "index_type": "fts"}
        )
        assert data.get("success") is True
        assert data.get("job_id") == "golden-job-1"
        assert env.recorder.golden_index_calls == [
            {
                "alias": "repo-a",
                "index_type": "fts",
                "submitter_username": ADMIN.username,
            }
        ]
