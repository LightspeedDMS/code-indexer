"""
Tests for repository-access scoping and elevation on the MCP
`bulk_add_provider_index` tool.

Invariants under test:

1. bulk provider-index add only targets golden repositories the caller can
   access -- filtered through `AccessFilteringService`, matching the
   existing pattern in the sibling `handle_list_global_repos` /
   `_append_global_repos_to_status` handlers in the same file (`repos.py`).
   The ADMIN role bypasses the filter unconditionally.
2. When the access-filtering service is unavailable, the tool fails closed
   for non-admin callers (no repos listed, no jobs submitted) rather than
   treating "unavailable" as "nothing to filter". Admin callers are
   unaffected, since they never depend on this service for this tool.
3. The tool requires a live TOTP elevation window when elevation
   enforcement is turned on, matching its REST twin (POST .../bulk-add
   carries require_elevation()).

These tests drive the tool through the REAL MCP JSON-RPC dispatch layer
(`handle_tools_call` in `mcp/protocol.py`) -- the same front door a real MCP
client uses -- rather than calling the handler function directly. Only the
golden-repo registry, background-job manager, and provider-index service are
stubbed; `AccessFilteringService`, `GroupAccessManager`, and
`ElevatedSessionManager` are REAL objects backed by temporary SQLite
databases, never mocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server import app as real_app_module
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import repos as repos_module
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
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
def access_filtering_service(tmp_path: Path) -> AccessFilteringService:
    """Real AccessFilteringService + GroupAccessManager backed by a temp SQLite DB.

    Grants a "team-a" group access to repo-a only, and assigns power_user
    "alice" to that group. Nothing in the authorization path is mocked.
    """
    db_path = tmp_path / "group_access.db"
    manager = GroupAccessManager(db_path)
    team_a = manager.create_group("team-a", "Has access to repo-a only")
    manager.grant_repo_access("repo-a", team_a.id, granted_by="test-admin")
    manager.assign_user_to_group("alice", team_a.id, assigned_by="test-admin")
    return AccessFilteringService(group_access_manager=manager)


def _parse_response(response: Dict[str, Any]) -> Dict[str, Any]:
    content = response.get("content", [])
    assert content, f"Empty MCP response: {response}"
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


async def _call_bulk_add(
    user: User,
    access_filtering_service,
    *,
    elevation_key=None,
    elevation_enforcement: bool = False,
    elevation_manager=None,
    totp_service=None,
):
    """Drive bulk_add_provider_index through the real MCP tools/call dispatcher.

    elevation_enforcement/elevation_manager/totp_service default to values
    that keep the elevation decorator's kill switch OFF, so callers that
    don't care about elevation (the repo-access-scope tests) are unaffected.
    """
    job_manager = _FakeBackgroundJobManager()

    with (
        patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
        patch.object(real_app_module.app.state, "group_manager", None, create=True),
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


class TestBulkAddProviderIndexRepoAccessScope:
    """bulk provider-index add only targets repositories the caller can
    access."""

    @pytest.mark.asyncio
    async def test_power_user_without_group_grant_only_sees_own_repo(
        self, access_filtering_service
    ):
        """A power_user (repository:write, not admin) with a group grant on
        repo-a only sees repo-a in the jobs output, and no background job
        is submitted for repo-b or repo-c."""
        user = _make_user("alice", UserRole.POWER_USER)

        data, job_manager = await _call_bulk_add(user, access_filtering_service)

        assert data["success"] is True
        job_aliases = {job["alias"] for job in data["jobs"]}
        assert job_aliases == {"repo-a-global"}
        assert "repo-b-global" not in data["skipped"]
        assert "repo-c-global" not in data["skipped"]

        submitted_aliases = {call["repo_alias"] for call in job_manager.calls}
        assert submitted_aliases == {"repo-a-global"}

    @pytest.mark.asyncio
    async def test_admin_role_still_sees_every_repo(self, access_filtering_service):
        """Admin behaviour is unchanged: same repos, same order, same shape."""
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(user, access_filtering_service)

        assert data["success"] is True
        job_aliases = [job["alias"] for job in data["jobs"]]
        assert job_aliases == ["repo-a-global", "repo-b-global", "repo-c-global"]

        submitted_aliases = [call["repo_alias"] for call in job_manager.calls]
        assert submitted_aliases == [
            "repo-a-global",
            "repo-b-global",
            "repo-c-global",
        ]


class TestBulkAddProviderIndexFailsClosedWithoutAccessService:
    """bulk provider-index add fails closed for non-admin callers when the
    access-filtering service is unavailable: no repos are listed and no
    jobs are submitted. Admin callers are unaffected, since they never
    depend on the access-filtering service for this tool."""

    @pytest.mark.asyncio
    async def test_power_user_gets_error_and_no_jobs_when_service_unavailable(self):
        user = _make_user("alice", UserRole.POWER_USER)

        data, job_manager = await _call_bulk_add(user, None)

        assert "error" in data
        assert data.get("jobs") in (None, [])
        assert job_manager.calls == []

    @pytest.mark.asyncio
    async def test_admin_still_proceeds_when_service_unavailable(self):
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(user, None)

        assert data["success"] is True
        job_aliases = {job["alias"] for job in data["jobs"]}
        assert job_aliases == {"repo-a-global", "repo-b-global", "repo-c-global"}
        assert len(job_manager.calls) == 3


class TestBulkAddProviderIndexElevation:
    """bulk provider-index add requires a live TOTP elevation window, exactly
    like its REST twin (POST .../bulk-add carries require_elevation()), when
    elevation enforcement is turned on."""

    def test_declares_session_key_marker_for_dispatcher_injection(self):
        """protocol.py's Case B session_key injection relies on this marker,
        set by require_mcp_elevation() itself (mirrors handle_manage_ssh_key's
        _create/_assign_host: a directly-decorated handler needs no separate
        manual assignment)."""
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
        manager = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "elev.db"),
        )
        totp_svc = MagicMock()
        totp_svc.is_mfa_enabled.return_value = True
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(
            user,
            None,
            elevation_key=_ELEVATION_SESSION_KEY,
            elevation_enforcement=True,
            elevation_manager=manager,
            totp_service=totp_svc,
        )

        assert data.get("error") == "elevation_required"
        assert job_manager.calls == []

    @pytest.mark.asyncio
    async def test_enforcement_on_with_active_window_proceeds(self, tmp_path):
        manager = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "elev.db"),
        )
        user = _make_user("root-admin", UserRole.ADMIN)
        manager.create(_ELEVATION_SESSION_KEY, user.username, "127.0.0.1", scope="full")
        totp_svc = MagicMock()
        totp_svc.is_mfa_enabled.return_value = True

        data, job_manager = await _call_bulk_add(
            user,
            None,
            elevation_key=_ELEVATION_SESSION_KEY,
            elevation_enforcement=True,
            elevation_manager=manager,
            totp_service=totp_svc,
        )

        assert data["success"] is True
        assert len(job_manager.calls) == 3

    @pytest.mark.asyncio
    async def test_enforcement_off_proceeds_as_today(self):
        user = _make_user("root-admin", UserRole.ADMIN)

        data, job_manager = await _call_bulk_add(
            user, None, elevation_enforcement=False
        )

        assert data["success"] is True
        assert len(job_manager.calls) == 3
