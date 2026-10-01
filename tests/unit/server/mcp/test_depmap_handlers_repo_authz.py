"""Regression tests verifying repo-level access control for the MCP tools
depmap_find_consumers and depmap_get_repo_domains.

Both tools take a single-repo-identifying ``repo_name`` parameter and return
dependency-graph data scoped to it (consumer relationships / domain
membership). Front door: the real MCP dispatcher, ``handle_tools_call()``
(``code_indexer.server.mcp.protocol``), invoked with the actual production
``TOOL_REGISTRY``/``HANDLER_REGISTRY`` entries -- the real handler functions
run, never a stub -- with a REAL ``AccessFilteringService`` backed by a REAL
``GroupAccessManager`` (temp SQLite DB) for the access-control decision
itself.

Repository names and usernames are neutral placeholders (this is a public
open-source repository).
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager


def _make_user(
    username: str,
    role: UserRole = UserRole.NORMAL_USER,
    email: Optional[str] = None,
) -> User:
    return User(
        username=username,
        password_hash="hashed_password",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        email=email,
    )


@pytest.fixture
def group_db_path() -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _build_access_service(
    db_path: Path,
    *,
    granted_username: str,
    granted_repos: list,
    admin_username: str = "admin_user",
) -> AccessFilteringService:
    gam = GroupAccessManager(db_path)
    group = gam.create_group("restricted", "test group")
    gam.assign_user_to_group(granted_username, group.id, assigned_by="test")
    for repo in granted_repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")

    admins_group = gam.get_group_by_name("admins")
    assert admins_group is not None, "bootstrap must create the 'admins' group"
    gam.assign_user_to_group(admin_username, admins_group.id, assigned_by="test")

    return AccessFilteringService(gam)


def _indexed_app_state(tmp_path: Path, repo_name: str) -> Mock:
    """A minimal but real on-disk dependency-map layout with repo_name
    indexed, read the same way the handlers read it:
    app.state.dependency_map_service.cidx_meta_read_path."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir(parents=True, exist_ok=True)
    (dep_map_dir / "_domains.json").write_text(
        json.dumps(
            [{"name": "domain-a", "participating_repos": [repo_name], "role": "core"}]
        ),
        encoding="utf-8",
    )
    state = Mock()
    state.dependency_map_service.cidx_meta_read_path = tmp_path
    return state


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return data


async def _dispatch(
    tool_name: str,
    arguments: dict,
    user: User,
    access_service: Optional[AccessFilteringService],
    app_state: Optional[Mock] = None,
) -> Dict[str, Any]:
    """Call handle_tools_call() for real -- TOOL_REGISTRY/HANDLER_REGISTRY
    are the actual production registries, never stubs."""
    if app_state is None:
        app_state = Mock()
    app_state.access_filtering_service = access_service

    with (
        patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
    ):
        mock_app_module.app.state = app_state
        params = {"name": tool_name, "arguments": arguments}
        return await handle_tools_call(params, user)


class TestDepmapFindConsumersFrontDoor:
    TOOL = "depmap_find_consumers"

    @pytest.mark.asyncio
    async def test_denied_user_raises_value_error(self, group_db_path):
        """A user without the repo's group grant is denied, and dep-map
        resolution is never reached. The ValueError raised here is exactly
        what process_jsonrpc_request's outer `except ValueError` converts
        into a JSON-RPC -32602 "Invalid params" error for every MCP tool
        call -- this dispatcher-level guard produces that error shape for
        free, the same as every other repo-scoped tool."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["other-repo"],
        )

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                self.TOOL, {"repo_name": "example-repo"}, user, access_service
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "example-repo" in error_str
        assert "example_user" in error_str

    @pytest.mark.asyncio
    async def test_granted_user_succeeds(self, group_db_path, tmp_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        state = _indexed_app_state(tmp_path, "example-repo")

        result = await _dispatch(
            self.TOOL, {"repo_name": "example-repo"}, user, access_service, state
        )

        data = _parse_response(result)
        assert data["resolution"] in ("repo_has_no_consumers", "ok")

    @pytest.mark.asyncio
    async def test_admin_bypasses_check(self, group_db_path, tmp_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        state = _indexed_app_state(tmp_path, "example-repo")

        result = await _dispatch(
            self.TOOL, {"repo_name": "example-repo"}, admin, access_service, state
        )

        data = _parse_response(result)
        assert data["resolution"] in ("repo_has_no_consumers", "ok")

    @pytest.mark.asyncio
    async def test_admin_with_acting_users_excluding_repo_is_denied(
        self, group_db_path
    ):
        """An admin scoped to acting_users whose combined access does not
        include repo_name is denied -- the same scoping the dispatcher
        applies to every other repo-scoped tool via acting_users."""
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        acting_user = _make_user("acting_user", email="acting_user@example.com")
        access_service = _build_access_service(
            group_db_path,
            granted_username="acting_user",
            granted_repos=["other-repo"],
            admin_username="admin_user",
        )

        state = Mock()
        user_manager = Mock()
        user_manager.get_user_by_email.side_effect = (
            lambda email: acting_user if email == "acting_user@example.com" else None
        )
        state.user_manager = user_manager

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                self.TOOL,
                {
                    "repo_name": "example-repo",
                    "acting_users": ["acting_user@example.com"],
                },
                admin,
                access_service,
                state,
            )

        assert "example-repo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_access_filtering_service_unavailable_fails_closed(self):
        user = _make_user("some_user")

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(self.TOOL, {"repo_name": "example-repo"}, user, None)

        assert "access control service unavailable" in str(exc_info.value).lower()


class TestDepmapGetRepoDomainsFrontDoor:
    TOOL = "depmap_get_repo_domains"

    @pytest.mark.asyncio
    async def test_denied_user_raises_value_error(self, group_db_path):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["other-repo"],
        )

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                self.TOOL, {"repo_name": "example-repo"}, user, access_service
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "example-repo" in error_str
        assert "example_user" in error_str

    @pytest.mark.asyncio
    async def test_granted_user_succeeds(self, group_db_path, tmp_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        state = _indexed_app_state(tmp_path, "example-repo")

        result = await _dispatch(
            self.TOOL, {"repo_name": "example-repo"}, user, access_service, state
        )

        data = _parse_response(result)
        assert data["resolution"] == "ok"

    @pytest.mark.asyncio
    async def test_admin_bypasses_check(self, group_db_path, tmp_path):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        state = _indexed_app_state(tmp_path, "example-repo")

        result = await _dispatch(
            self.TOOL, {"repo_name": "example-repo"}, admin, access_service, state
        )

        data = _parse_response(result)
        assert data["resolution"] == "ok"

    @pytest.mark.asyncio
    async def test_admin_with_acting_users_excluding_repo_is_denied(
        self, group_db_path
    ):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        acting_user = _make_user("acting_user", email="acting_user@example.com")
        access_service = _build_access_service(
            group_db_path,
            granted_username="acting_user",
            granted_repos=["other-repo"],
            admin_username="admin_user",
        )

        state = Mock()
        user_manager = Mock()
        user_manager.get_user_by_email.side_effect = (
            lambda email: acting_user if email == "acting_user@example.com" else None
        )
        state.user_manager = user_manager

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                self.TOOL,
                {
                    "repo_name": "example-repo",
                    "acting_users": ["acting_user@example.com"],
                },
                admin,
                access_service,
                state,
            )

        assert "example-repo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_access_filtering_service_unavailable_fails_closed(self):
        user = _make_user("some_user")

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(self.TOOL, {"repo_name": "example-repo"}, user, None)

        assert "access control service unavailable" in str(exc_info.value).lower()
