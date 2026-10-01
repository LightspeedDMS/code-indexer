"""
Tests for the role, access and elevation rules of the MCP
`bulk_add_provider_index` tool.

Invariants under test, each matching the REST twin (POST .../bulk-add, which
depends on get_current_admin_user_hybrid and carries require_elevation()):

1. The tool is admin-only at the MCP dispatcher: a power_user is refused and
   the tool is not listed for them.
2. The handler itself requires the admin role, so a non-admin admitted by a
   group tool grant is refused too, with or without an elevation window, and
   no job is submitted.
3. An admin targets every golden repository, whether or not the
   access-filtering service is available.
4. The tool requires a live TOTP elevation window when elevation enforcement
   is turned on.

These tests drive the tool through the REAL MCP JSON-RPC dispatch layer
(`handle_tools_call` in `mcp/protocol.py`) -- the same front door a real MCP
client uses. Only the golden-repo registry, background-job manager, and
provider-index service are stubbed; `GroupAccessManager` and
`ElevatedSessionManager` are REAL objects backed by temporary SQLite
databases, never mocked.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server import app as real_app_module
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import repos as repos_module
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.mcp.tools import filter_tools_by_role
from code_indexer.server.services.group_access_manager import GroupAccessManager


_ELEVATION_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_ELEVATION_TOTP_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
)
_ELEVATION_ESM_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
)
_ELEVATION_SESSION_KEY = "jti-test-session-abc"
_ADMIN_ROLE_REQUIRED = "Permission denied: admin role required"


def _make_user(username: str, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


def _repo_entry(
    alias: str, repo_name: str, category: str = "general"
) -> Dict[str, Any]:
    return {
        "alias_name": alias,
        "repo_name": repo_name,
        "repo_url": f"local:///golden-repos/{repo_name}",
        "category": category,
        "index_path": f"/golden-repos/{repo_name}/.code-indexer/index",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_refresh": "2026-01-01T00:00:00+00:00",
    }


_GLOBAL_REPOS: List[Dict[str, Any]] = [
    _repo_entry("repo-a-global", "repo-a"),
    _repo_entry("repo-b-global", "repo-b"),
    _repo_entry("repo-c-global", "repo-c"),
]


class _FakeBackgroundJobManager:
    """Records every submit_job() call instead of running a real job."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def submit_job(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"job-{len(self.calls)}"


@pytest.fixture
def granted_group_manager(tmp_path: Path) -> GroupAccessManager:
    """Real GroupAccessManager with group tool-access enforcement on.

    power_user "alice" belongs to "team-a", which holds a tool grant for
    bulk_add_provider_index -- so the dispatcher admits alice by grant
    rather than by role.
    """
    db_path = tmp_path / "group_access.db"
    manager = GroupAccessManager(db_path)
    team_a = manager.create_group("team-a", "Granted the bulk provider-index tool")
    manager.grant_repo_access("repo-a", team_a.id, granted_by="test-admin")
    manager.assign_user_to_group("alice", team_a.id, assigned_by="test-admin")
    manager.set_tool_access("bulk_add_provider_index", team_a.id, True, "test-admin")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tool_access_migration_state (id, complete) VALUES (1, 1)"
        )
        conn.commit()
    return manager


def _new_elevation_manager(tmp_path: Path) -> ElevatedSessionManager:
    return ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elev.db"),
    )


def _mfa_enrolled_totp_service() -> MagicMock:
    totp_svc = MagicMock()
    totp_svc.is_mfa_enabled.return_value = True
    return totp_svc


def _parse_response(response: Dict[str, Any]) -> Dict[str, Any]:
    content = response.get("content", [])
    assert content, f"Empty MCP response: {response}"
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


async def _call_bulk_add(
    user: User,
    access_filtering_service: Any = None,
    *,
    elevation_key: Optional[str] = None,
    elevation_enforcement: bool = False,
    elevation_manager: Optional[ElevatedSessionManager] = None,
    totp_service: Any = None,
    tool_access_manager: Optional[GroupAccessManager] = None,
):
    """Drive bulk_add_provider_index through the real MCP tools/call dispatcher.

    elevation_enforcement/elevation_manager/totp_service default to values
    that keep the elevation decorator's kill switch OFF. tool_access_manager
    None keeps group tool-access enforcement off (role check only).
    """
    job_manager = _FakeBackgroundJobManager()

    with (
        patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
        patch.object(
            real_app_module.app.state,
            "group_manager",
            tool_access_manager,
            create=True,
        ),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch(
            "code_indexer.server.mcp.handlers._list_global_repos",
            return_value=[dict(r) for r in _GLOBAL_REPOS],
        ),
        patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
            side_effect=lambda alias: f"/fake/golden-repos/{alias}",
        ),
        patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
            side_effect=lambda alias: f"/fake/golden-repos/{alias}/base",
        ),
        patch(
            "code_indexer.server.mcp.handlers._append_provider_to_config",
            return_value=True,
        ),
        patch(
            "code_indexer.server.services.provider_index_service.ProviderIndexService"
        ) as mock_service_cls,
        patch(_ELEVATION_ENFORCEMENT_PATH, return_value=elevation_enforcement),
        patch(_ELEVATION_ESM_PATH, elevation_manager),
        patch(_ELEVATION_TOTP_PATH, return_value=totp_service),
    ):
        mock_app_module.app.state.access_filtering_service = access_filtering_service
        mock_app_module.background_job_manager = job_manager

        mock_service = mock_service_cls.return_value
        mock_service.validate_provider.return_value = None
        mock_service.get_provider_index_status.return_value = {}

        response = await handle_tools_call(
            {
                "name": "bulk_add_provider_index",
                "arguments": {"provider": "voyage-code-3"},
            },
            user,
            elevation_key=elevation_key,
        )

    data = _parse_response(response)
    return data, job_manager


class TestBulkAddProviderIndexIsAdminOnly:
    """bulk provider-index add is admin-only, exactly like its REST twin."""

    @pytest.mark.asyncio
    async def test_power_user_is_refused_through_tools_call(self):
        user = _make_user("alice", UserRole.POWER_USER)

        with pytest.raises(ValueError, match="Permission denied"):
            await _call_bulk_add(user)

    def test_tool_is_listed_for_admin_only(self):
        power_names = {
            tool["name"]
            for tool in filter_tools_by_role(_make_user("alice", UserRole.POWER_USER))
        }
        admin_names = {
            tool["name"]
            for tool in filter_tools_by_role(_make_user("root-admin", UserRole.ADMIN))
        }
        assert "bulk_add_provider_index" not in power_names
        assert "bulk_add_provider_index" in admin_names

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_window", [False, True])
    async def test_group_granted_power_user_is_refused(
        self, tmp_path, granted_group_manager, with_window
    ):
        """A group tool grant admits alice past the dispatcher's role check;
        the handler still requires the admin role."""
        user = _make_user("alice", UserRole.POWER_USER)
        manager = _new_elevation_manager(tmp_path)
        if with_window:
            manager.create(
                _ELEVATION_SESSION_KEY, user.username, "127.0.0.1", scope="full"
            )

        data, job_manager = await _call_bulk_add(
            user,
            elevation_key=_ELEVATION_SESSION_KEY,
            elevation_enforcement=True,
            elevation_manager=manager,
            totp_service=_mfa_enrolled_totp_service(),
            tool_access_manager=granted_group_manager,
        )

        assert data == {"success": False, "error": _ADMIN_ROLE_REQUIRED}
        assert job_manager.calls == []


class TestBulkAddProviderIndexAdminTargetsEveryRepo:
    """Admin behaviour is unchanged: every golden repository, same order."""

    @pytest.mark.asyncio
    async def test_admin_targets_every_repo(self):
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(user, MagicMock())

        assert data["success"] is True
        job_aliases = [job["alias"] for job in data["jobs"]]
        assert job_aliases == ["repo-a-global", "repo-b-global", "repo-c-global"]
        submitted_aliases = [call["repo_alias"] for call in job_manager.calls]
        assert submitted_aliases == job_aliases

    @pytest.mark.asyncio
    async def test_admin_proceeds_when_access_service_unavailable(self):
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(user, None)

        assert data["success"] is True
        assert len(job_manager.calls) == 3


class TestBulkAddProviderIndexElevation:
    """bulk provider-index add requires a live TOTP elevation window, exactly
    like its REST twin, when elevation enforcement is turned on."""

    def test_declares_session_key_marker_for_dispatcher_injection(self):
        """protocol.py's Case B session_key injection relies on this marker."""
        assert (
            getattr(
                repos_module.bulk_add_provider_index,
                "__mcp_requires_session_key__",
                False,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_enforcement_on_without_window_returns_elevation_required(
        self, tmp_path
    ):
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(
            user,
            elevation_key=_ELEVATION_SESSION_KEY,
            elevation_enforcement=True,
            elevation_manager=_new_elevation_manager(tmp_path),
            totp_service=_mfa_enrolled_totp_service(),
        )

        assert data.get("error") == "elevation_required"
        assert job_manager.calls == []

    @pytest.mark.asyncio
    async def test_enforcement_on_with_active_window_proceeds(self, tmp_path):
        manager = _new_elevation_manager(tmp_path)
        user = _make_user("root-admin", UserRole.ADMIN)
        manager.create(_ELEVATION_SESSION_KEY, user.username, "127.0.0.1", scope="full")

        data, job_manager = await _call_bulk_add(
            user,
            elevation_key=_ELEVATION_SESSION_KEY,
            elevation_enforcement=True,
            elevation_manager=manager,
            totp_service=_mfa_enrolled_totp_service(),
        )

        assert data["success"] is True
        assert len(job_manager.calls) == 3

    @pytest.mark.asyncio
    async def test_enforcement_off_proceeds_as_today(self):
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(user, elevation_enforcement=False)

        assert data["success"] is True
        assert len(job_manager.calls) == 3
