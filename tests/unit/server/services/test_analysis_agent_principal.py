"""The analysis agents' MCP credential belongs to a least-privilege principal.

Description refresh, lifecycle and dependency-map agents reach the server's
own MCP endpoint (cidx-local) with one stored credential. That credential
must belong to a dedicated principal that can call only read/query tools
and is never auto-elevated -- never to the admin account. An existing
admin-bound credential is replaced (and revoked) automatically the next time
the credential is obtained.

Real collaborators: SQLite user store, MCP credential manager, group store,
runtime config service. The dispatcher's authorization step is the real
``_authorize_tool_call``.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest

from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.mcp.protocol import _authorize_tool_call
from code_indexer.server.mcp.tool_access import ToolAccessMemo
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.analysis_agent_principal import (
    ANALYSIS_AGENT_TOOLS,
    ANALYSIS_AGENT_USERNAME,
)
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.mcp_self_registration_service import (
    MCPSelfRegistrationService,
)
from code_indexer.server.storage.database_manager import DatabaseSchema

ADMIN_PASSWORD = "Ex4mple#Strong!Pw9QzR"

# Tools named by the agent prompts (server/prompts/lifecycle_unified.md,
# global_repos/dependency_map_analyzer.py, mcp/prompts/bidirectional_mismatch_audit.md):
# search_code (semantic + full-text), SCIP intelligence, and sibling-repo
# discovery/reading for deploy-wiring evidence.
READ_TOOLS_THE_AGENTS_USE = [
    "search_code",
    "regex_search",
    "scip_definition",
    "scip_references",
    "scip_callchain",
    "list_global_repos",
    "browse_directory",
    "get_file_content",
    "git_log",
]

ADMIN_OR_WRITE_TOOLS = [
    "create_user",
    "list_users",
    "set_global_config",
    "get_global_config",
    "add_golden_repo",
    "remove_golden_repo",
    "refresh_golden_repo",
    "create_group",
    "manage_group_members",
    "manage_mcp_credential",
    "create_api_key",
    "configure_git_credential",
    "set_session_impersonation",
    "activate_repository",
    "store_xray_pattern",
    "create_memory",
    "git_commit",
]


class _World:
    def __init__(self, tmp_path: Path) -> None:
        db = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path=db).initialize_database()
        self.users = UserManager(use_sqlite=True, db_path=db)
        self.users.create_user("admin", ADMIN_PASSWORD, UserRole.ADMIN)
        self.credentials = MCPCredentialManager(user_manager=self.users)
        self.groups = GroupAccessManager(tmp_path / "groups.db")
        self.config = ConfigService(server_dir_path=str(tmp_path / "server"))
        self.config.load_config()
        self.service = MCPSelfRegistrationService(
            config_manager=self.config, mcp_credential_manager=self.credentials
        )
        self.service.set_group_manager(self.groups)

    def store(self, client_id: str, client_secret: str) -> None:
        def _store(candidate: Any) -> None:
            candidate.mcp_self_registration.client_id = client_id
            candidate.mcp_self_registration.client_secret = client_secret

        self.config.apply_system_change(_store)


@pytest.fixture()
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def test_allowlist_names_only_registered_tools() -> None:
    assert ANALYSIS_AGENT_TOOLS <= set(TOOL_REGISTRY)
    assert set(READ_TOOLS_THE_AGENTS_USE) <= ANALYSIS_AGENT_TOOLS
    assert not set(ADMIN_OR_WRITE_TOOLS) & ANALYSIS_AGENT_TOOLS


def test_fresh_deployment_mints_the_credential_for_the_principal(world: _World) -> None:
    creds = world.service.get_or_create_credentials()
    assert creds is not None
    owner = world.credentials.verify_credential(
        creds["client_id"], creds["client_secret"]
    )
    assert owner == ANALYSIS_AGENT_USERNAME
    principal = world.users.get_user(ANALYSIS_AGENT_USERNAME)
    assert principal is not None and principal.role == UserRole.NORMAL_USER
    group = world.groups.get_user_group(ANALYSIS_AGENT_USERNAME)
    assert group is not None and group.name == "powerusers"


def test_an_existing_principal_in_admins_is_moved_to_powerusers(world: _World) -> None:
    world.users.create_user(
        ANALYSIS_AGENT_USERNAME, "Ex4mple#Strong!Pw9QzR", UserRole.NORMAL_USER
    )
    admins = world.groups.get_group_by_name("admins")
    assert admins is not None
    world.groups.assign_user_to_group(ANALYSIS_AGENT_USERNAME, admins.id, "legacy")

    assert world.service.get_or_create_credentials() is not None

    group = world.groups.get_user_group(ANALYSIS_AGENT_USERNAME)
    assert group is not None and group.name == "powerusers"


class _StaleThenRacingUsers:
    """A user store whose first lookup misses a principal a peer node has
    just created; creating it again then fails with the store's integrity
    error (sqlite3.IntegrityError here, UniqueViolation on PostgreSQL)."""

    def __init__(self, inner: UserManager) -> None:
        self._inner = inner
        self._lookups = 0

    def get_user(self, username: str) -> Any:
        self._lookups += 1
        return None if self._lookups == 1 else self._inner.get_user(username)

    def is_sso_user(self, username: str) -> bool:
        return self._inner.is_sso_user(username)

    def create_user_audited(self, *args: Any, **kwargs: Any) -> Any:
        import sqlite3

        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.username")


def test_a_peer_creating_the_principal_concurrently_is_tolerated(world: _World) -> None:
    from code_indexer.server.services.analysis_agent_principal import (
        ensure_analysis_agent_principal,
    )

    world.users.create_user(
        ANALYSIS_AGENT_USERNAME, "Ex4mple#Strong!Pw9QzR", UserRole.NORMAL_USER
    )
    ensure_analysis_agent_principal(_StaleThenRacingUsers(world.users), world.groups)
    group = world.groups.get_user_group(ANALYSIS_AGENT_USERNAME)
    assert group is not None and group.name == "powerusers"


def test_the_principal_creation_is_audited_as_system_provisioning(
    world: _World, tmp_path: Path
) -> None:
    from tests.unit.server.audit._audit_accounts_support import bound_audit_store

    (tmp_path / "audit").mkdir()
    for store in bound_audit_store(tmp_path / "audit" / "groups.db"):
        assert world.service.get_or_create_credentials() is not None
        (row,) = store.rows("user_created")
        assert row.target_id == ANALYSIS_AGENT_USERNAME
        assert row.actor_is_system == 1
        assert row.details["provisioning"] == "system"


def test_existing_admin_bound_credential_is_replaced_and_revoked(world: _World) -> None:
    legacy = world.credentials.generate_credential("admin", "cidx-local-auto")
    world.store(legacy["client_id"], legacy["client_secret"])

    creds = world.service.get_or_create_credentials()

    assert creds is not None and creds["client_id"] != legacy["client_id"]
    assert (
        world.credentials.verify_credential(
            legacy["client_id"], legacy["client_secret"]
        )
        is None
    )
    owner = world.credentials.verify_credential(
        creds["client_id"], creds["client_secret"]
    )
    assert owner == ANALYSIS_AGENT_USERNAME
    stored = world.config.load_config().mcp_self_registration
    assert stored is not None and stored.client_id == creds["client_id"]
    assert world.service.get_or_create_credentials() == creds


def _authorize(
    tool: str,
    user: Any,
    access: AccessFilteringService,
    repo: str = "example-repo-global",
) -> Optional[str]:
    """None when the dispatcher admits the call, else its refusal. The
    dispatcher judges ``<name>-global`` as golden repository ``<name>``."""
    state = SimpleNamespace(access_filtering_service=access, user_manager=None)
    app_module = SimpleNamespace(app=SimpleNamespace(state=state))
    arguments: Dict[str, Any] = {"repository_alias": repo}
    with patch("code_indexer.server.mcp.handlers.app_module", app_module):
        try:
            _authorize_tool_call(tool, arguments, user, ToolAccessMemo(None))
        except ValueError as refusal:
            return str(refusal)
    return None


def test_principal_is_refused_admin_tools_and_admitted_to_read_tools(
    world: _World,
) -> None:
    creds = world.service.get_or_create_credentials()
    assert creds is not None
    owner = world.credentials.verify_credential(
        creds["client_id"], creds["client_secret"]
    )
    assert owner == ANALYSIS_AGENT_USERNAME
    principal = world.users.get_user(owner)
    access = AccessFilteringService(world.groups)
    # Every golden repo is auto-assigned to powerusers at registration.
    world.groups.auto_assign_golden_repo("example-repo")
    admins = world.groups.get_group_by_name("admins")
    assert admins is not None
    world.groups.grant_repo_access("admins-only-repo", admins.id, "test")
    assert _authorize("search_code", principal, access, "admins-only-repo-global")

    refused = {
        tool: _authorize(tool, principal, access) for tool in ADMIN_OR_WRITE_TOOLS
    }
    admitted = {
        tool: _authorize(tool, principal, access) for tool in READ_TOOLS_THE_AGENTS_USE
    }

    assert all(refused.values()), {t: r for t, r in refused.items() if r is None}
    assert admitted == {tool: None for tool in READ_TOOLS_THE_AGENTS_USE}


class _RecordingElevations:
    def __init__(self) -> None:
        self.created: List[str] = []

    def create(
        self, *, session_key: str, username: str, elevated_from_ip: str, scope: str
    ) -> None:
        self.created.append(username)


def _basic_request(client_id: str, client_secret: str) -> Any:
    from starlette.requests import Request

    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", f"Basic {token}".encode())],
        "client": ("192.0.2.10", 5000),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_mcp_credential_login_never_pre_elevates_the_principal(
    world: _World,
) -> None:
    from code_indexer.server.auth import dependencies

    creds = world.service.get_or_create_credentials()
    assert creds is not None
    admin_cred = world.credentials.generate_credential("admin", "example-admin-client")
    elevations = _RecordingElevations()
    with (
        patch.object(dependencies, "mcp_credential_manager", world.credentials),
        patch.object(dependencies, "user_manager", world.users),
        patch.object(dependencies, "elevated_session_manager", elevations),
    ):
        agent = await dependencies.get_mcp_user_from_credentials(
            _basic_request(creds["client_id"], creds["client_secret"])
        )
        admin = await dependencies.get_mcp_user_from_credentials(
            _basic_request(admin_cred["client_id"], admin_cred["client_secret"])
        )
    assert agent is not None and agent.username == ANALYSIS_AGENT_USERNAME
    assert admin is not None and admin.username == "admin"
    assert elevations.created == ["admin"]


def test_registration_replaces_the_cli_entry(world: _World, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.argv"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/bash\necho "$*" >> {log}\nexit 0\n')
    fake.chmod(0o755)
    creds = world.service.get_or_create_credentials()
    assert creds is not None
    with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
        assert world.service.register_in_claude_code(creds) is True
    calls = log.read_text().splitlines()
    assert calls[0] == "mcp get cidx-local"
    assert calls[1] == "mcp remove cidx-local --scope user"
    assert calls[2].startswith("mcp add --transport http --header")


def test_registration_is_skipped_when_the_entry_already_holds_the_header(
    world: _World, tmp_path: Path
) -> None:
    creds = world.service.get_or_create_credentials()
    assert creds is not None
    raw = f"{creds['client_id']}:{creds['client_secret']}".encode()
    port = world.config.load_config().port
    listing = tmp_path / "listing.txt"
    listing.write_text(
        "cidx-local:\n  Scope: User config\n  Type: http\n"
        f"  URL: http://localhost:{port}/mcp\n  Headers:\n"
        f"    Authorization: Basic {base64.b64encode(raw).decode()}\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.argv"
    fake = bin_dir / "claude"
    fake.write_text(
        f'#!/bin/bash\necho "$*" >> {log}\n[ "$2" = get ] && cat {listing}\nexit 0\n'
    )
    fake.chmod(0o755)
    with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
        assert world.service.register_in_claude_code(creds) is True
    assert log.read_text().splitlines() == ["mcp get cidx-local"]


class _PeerRevokesFirst:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def revoke_credential_audited(self, *args: Any, **kwargs: Any) -> bool:
        self._inner.revoke_credential_audited(*args, **kwargs)
        return bool(self._inner.revoke_credential_audited(*args, **kwargs))


def test_a_credential_already_revoked_by_a_peer_is_not_an_error(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = world.credentials.generate_credential("admin", "cidx-local-auto")
    world.store(legacy["client_id"], legacy["client_secret"])
    world.service._mcp_credential_manager = _PeerRevokesFirst(world.credentials)
    caplog.clear()
    creds = world.service.get_or_create_credentials()
    assert creds is not None
    owner = world.credentials.verify_credential(
        creds["client_id"], creds["client_secret"]
    )
    assert owner == ANALYSIS_AGENT_USERNAME
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.fixture(autouse=True)
def _no_singleton_leak() -> Iterator[None]:
    yield
    MCPSelfRegistrationService.set_instance(None)


def _owner_of_header(world: _World, header: str) -> Optional[str]:
    assert header.startswith("Basic ")
    client_id, secret = base64.b64decode(header[len("Basic ") :]).decode().split(":")
    return world.credentials.verify_credential(client_id, secret)


def test_an_existing_installation_migrates_at_the_next_invocation(
    world: _World, tmp_path: Path
) -> None:
    import json as _json

    from code_indexer.server.services.agent_cli_isolation import (
        remove_mcp_config_file,
        try_build_mcp_config_file,
    )
    from code_indexer.server.services.codex_mcp_auth_header_provider import (
        build_codex_mcp_auth_header_provider,
    )

    legacy = world.credentials.generate_credential("admin", "cidx-local-auto")
    world.store(legacy["client_id"], legacy["client_secret"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.argv"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/bash\necho "$*" >> {log}\nexit 0\n')
    fake.chmod(0o755)
    MCPSelfRegistrationService.set_instance(world.service)

    with (
        patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}),
        patch(
            "code_indexer.server.services.agent_cli_isolation.get_config_service",
            return_value=world.config,
        ),
    ):
        assert world.service.ensure_registered() is True  # entry already present
        config_path = try_build_mcp_config_file()
        codex_header = build_codex_mcp_auth_header_provider()()
    assert config_path is not None
    try:
        document = _json.loads(Path(config_path).read_text())
    finally:
        remove_mcp_config_file(config_path)

    header = document["mcpServers"]["cidx-local"]["headers"]["Authorization"]
    assert _owner_of_header(world, header) == ANALYSIS_AGENT_USERNAME
    assert _owner_of_header(world, codex_header) == ANALYSIS_AGENT_USERNAME
    assert (
        world.credentials.verify_credential(
            legacy["client_id"], legacy["client_secret"]
        )
        is None
    )
    calls = log.read_text().splitlines()
    removed = calls.index("mcp remove cidx-local --scope user")
    added = calls[removed + 1]
    assert added.startswith("mcp add") and header in added
    assert legacy["client_id"] not in " ".join(calls[removed:])


def _assert_refused(world: _World, caplog: pytest.LogCaptureFixture) -> None:
    assert world.service.get_or_create_credentials() is None
    assert world.credentials.get_credentials(ANALYSIS_AGENT_USERNAME) == []
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_an_admin_account_with_the_reserved_name_is_refused(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    world.users.create_user(
        ANALYSIS_AGENT_USERNAME, "Ex4mple#Strong!Pw9QzR", UserRole.ADMIN
    )
    _assert_refused(world, caplog)


def test_an_sso_linked_account_with_the_reserved_name_is_refused(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    world.users.create_user(
        ANALYSIS_AGENT_USERNAME, "Ex4mple#Strong!Pw9QzR", UserRole.NORMAL_USER
    )
    world.users.set_oidc_identity(ANALYSIS_AGENT_USERNAME, {"subject": "example-sub"})
    _assert_refused(world, caplog)


def test_a_principal_in_users_is_moved_to_powerusers(world: _World) -> None:
    world.users.create_user(
        ANALYSIS_AGENT_USERNAME, "Ex4mple#Strong!Pw9QzR", UserRole.NORMAL_USER
    )
    users_group = world.groups.get_group_by_name("users")
    assert users_group is not None
    world.groups.assign_user_to_group(ANALYSIS_AGENT_USERNAME, users_group.id, "seed")
    assert world.service.get_or_create_credentials() is not None
    group = world.groups.get_user_group(ANALYSIS_AGENT_USERNAME)
    assert group is not None and group.name == "powerusers"


def test_a_principal_in_an_operator_group_is_kept(world: _World) -> None:
    world.users.create_user(
        ANALYSIS_AGENT_USERNAME, "Ex4mple#Strong!Pw9QzR", UserRole.NORMAL_USER
    )
    custom = world.groups.create_group("example-agents", "operator choice")
    world.groups.assign_user_to_group(ANALYSIS_AGENT_USERNAME, custom.id, "operator")
    assert world.service.get_or_create_credentials() is not None
    group = world.groups.get_user_group(ANALYSIS_AGENT_USERNAME)
    assert group is not None and group.name == "example-agents"


@pytest.mark.parametrize("change", ["admin_role", "email", "sso_link"])
def test_a_stored_credential_is_not_reused_for_a_changed_account(
    world: _World, caplog: pytest.LogCaptureFixture, change: str
) -> None:
    assert world.service.get_or_create_credentials() is not None
    if change == "admin_role":
        world.users.update_user_role(ANALYSIS_AGENT_USERNAME, UserRole.ADMIN)
    elif change == "email":
        world.users.update_user(ANALYSIS_AGENT_USERNAME, new_email="a@example.com")
    else:
        world.users.set_oidc_identity(
            ANALYSIS_AGENT_USERNAME, {"subject": "example-sub"}
        )
    caplog.clear()
    assert world.service.get_or_create_credentials() is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_reuse_heals_a_principal_moved_into_admins(world: _World) -> None:
    first = world.service.get_or_create_credentials()
    assert first is not None
    admins = world.groups.get_group_by_name("admins")
    assert admins is not None
    world.groups.assign_user_to_group(ANALYSIS_AGENT_USERNAME, admins.id, "operator")
    again = world.service.get_or_create_credentials()
    assert again is not None and again["client_id"] == first["client_id"]
    group = world.groups.get_user_group(ANALYSIS_AGENT_USERNAME)
    assert group is not None and group.name == "powerusers"
