"""Front-door regression tests verifying the MCP dispatcher authorizes
EVERY recognized repo-identifying parameter present in a tool call, not
only the first one it happens to scan.

A tool call may carry more than one parameter name the dispatcher
recognizes as identifying a repository (golden_repo_alias(es),
repository_alias, alias, user_alias, repo_alias, plus repo_name for the
two depmap tools) even when the tool's OWN schema declares only one of
them -- the dispatcher does not validate arguments against the tool's
schema. Naming an accessible repo under one recognized parameter must
never excuse an inaccessible repo named under a different recognized
parameter present in the SAME call.

Front door: the real `handle_tools_call()` dispatch (real TOOL_REGISTRY/
HANDLER_REGISTRY entries, the actual production handler functions --
never a stub) with a REAL AccessFilteringService backed by a REAL
GroupAccessManager (temp SQLite DB) for the access-control decision.

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


def _indexed_dep_map_state(tmp_path: Path, repo_name: str) -> Mock:
    """A minimal but real on-disk dependency-map layout with repo_name
    indexed, read the same way depmap handlers read it."""
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


class TestDecoyParameterDenied:
    """A decoy value under an earlier-scanned recognized parameter must
    never excuse an inaccessible value under a different recognized
    parameter present in the SAME call -- for a representative read tool,
    a write tool, and both depmap tools."""

    @pytest.mark.asyncio
    async def test_search_code_decoy_under_golden_repo_alias(self, group_db_path):
        """search_code's real handler reads repository_alias; a decoy
        golden_repo_alias naming an accessible repo must not excuse an
        inaccessible repository_alias present in the same call."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["granted-repo"],
        )

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                "search_code",
                {
                    "golden_repo_alias": "granted-repo",
                    "repository_alias": "secret-repo",
                    "query_text": "anything",
                },
                user,
                access_service,
            )

        assert "secret-repo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_write_tool_decoy_under_golden_repo_alias(self, group_db_path):
        """create_file (repository:write) reads repository_alias; same
        decoy shape as the read-tool case, on a POWER_USER write tool."""
        user = _make_user("example_user", role=UserRole.POWER_USER)
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["granted-repo"],
        )

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                "create_file",
                {
                    "golden_repo_alias": "granted-repo",
                    "repository_alias": "secret-repo",
                    "file_path": "foo.txt",
                    "content": "hello",
                },
                user,
                access_service,
            )

        assert "secret-repo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_depmap_find_consumers_decoy_under_alias(self, group_db_path):
        """depmap_find_consumers's real handler reads repo_name (scanned
        LAST); a decoy 'alias', naming an accessible repo, must not
        excuse an inaccessible repo_name present in the same call."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["granted-repo"],
        )

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                "depmap_find_consumers",
                {"alias": "granted-repo", "repo_name": "secret-repo"},
                user,
                access_service,
            )

        assert "secret-repo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_depmap_get_repo_domains_decoy_under_alias(self, group_db_path):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["granted-repo"],
        )

        with pytest.raises(ValueError) as exc_info:
            await _dispatch(
                "depmap_get_repo_domains",
                {"alias": "granted-repo", "repo_name": "secret-repo"},
                user,
                access_service,
            )

        assert "secret-repo" in str(exc_info.value)


class TestLegitimateCallsStillPass:
    """The multi-parameter fix must not regress any legitimate single- or
    multi-parameter call."""

    @pytest.mark.asyncio
    async def test_legitimate_single_param_call_passes(self, group_db_path, tmp_path):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        state = _indexed_dep_map_state(tmp_path, "example-repo")

        result = await _dispatch(
            "depmap_find_consumers",
            {"repo_name": "example-repo"},
            user,
            access_service,
            state,
        )

        data = json.loads(result["content"][0]["text"])
        assert data["resolution"] in ("repo_has_no_consumers", "ok")

    @pytest.mark.asyncio
    async def test_activate_repository_with_granted_golden_alias_passes(
        self, group_db_path
    ):
        """activate_repository's user_alias is the NEW alias being
        created -- it must never be checked, so a granted golden_repo_alias
        plus an arbitrary new user_alias still succeeds."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["granted-golden"],
        )

        state = Mock()
        state.access_filtering_service = access_service
        mock_arm = Mock()
        mock_arm.activate_repository.return_value = "job-123"

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
        ):
            mock_app_module.app.state = state
            mock_app_module.activated_repo_manager = mock_arm
            params = {
                "name": "activate_repository",
                "arguments": {
                    "golden_repo_alias": "granted-golden",
                    "user_alias": "my-brand-new-alias",
                },
            }
            result = await handle_tools_call(params, user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["job_id"] == "job-123"
        mock_arm.activate_repository.assert_called_once()

    @pytest.mark.asyncio
    async def test_owner_enforced_tool_passes(self, group_db_path):
        """deactivate_repository's user_alias names a user-OWNED
        activation, enforced at the manager layer -- it is never checked
        against the caller's golden-repo grants, so a call with NO
        matching golden grant at all still succeeds."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=[],  # deliberately empty -- would block if user_alias were checked
        )

        state = Mock()
        state.access_filtering_service = access_service
        mock_arm = Mock()
        mock_arm.deactivate_repository.return_value = "job-456"

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
        ):
            mock_app_module.app.state = state
            mock_app_module.activated_repo_manager = mock_arm
            params = {
                "name": "deactivate_repository",
                "arguments": {"user_alias": "my-own-activated-repo"},
            }
            result = await handle_tools_call(params, user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["job_id"] == "job-456"

    @pytest.mark.asyncio
    async def test_acting_users_scoping_still_holds(self, group_db_path):
        """An admin scoped to acting_users whose combined access excludes
        the target repo is still denied -- the multi-parameter fix must
        not weaken acting_users scoping."""
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
                "depmap_find_consumers",
                {
                    "repo_name": "example-repo",
                    "acting_users": ["acting_user@example.com"],
                },
                admin,
                access_service,
                state,
            )

        assert "example-repo" in str(exc_info.value)
