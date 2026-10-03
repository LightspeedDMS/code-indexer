"""Every security-relevant door maps to catalog action types or a written reason.

The inventory is DERIVED, never hand-listed, so a new door cannot be added
without this test noticing it:

- MCP tools whose ``required_permission`` is an administrative one (or
  ``public``, the login tool);
- MCP handlers carrying the ``__mcp_requires_session_key__`` marker (the
  elevation-gated tools);
- EVERY registered mutating REST and Web route (POST/PUT/PATCH/DELETE) of the
  app as mounted at startup, including the routers the lifespan mounts, plus
  the self-authenticating GET doors.  The route table is the production
  wiring's own, built in a separate process (``_audit_route_table_probe``).
  No gate detection decides what is inventoried, so a route guarded by a
  helper nobody listed is still inventoried.

Every route is then MAPPED to catalog action types, EXEMPT with a one-line
reason, or NON-ADMIN SELF-SERVICE with a one-line reason (the caller acting
on their own resources, or a read-only search); an unclassified route fails.
The known admin / elevation gate names remain only as a cross-check: a route
behind one of them can never be filed as self-service.  The documented-gap
tables must stay empty.

A mapped door must reach the emission of every type it is mapped to: its
handler, or a same-module helper it calls (bounded depth), names the type's
audited entry point (or, for a legacy type, its literal or its audited entry
point).  Every allowlisted catalog type must be claimed by some door.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, List, Mapping, Set, Tuple

import pytest

import code_indexer
from _audit_route_classification import (
    READ_ONLY,
    ROUTE_EXEMPT,
    ROUTE_SELF_SERVICE,
    SCIP_SCRATCH_CLEANUP,
    WORKSPACE_GIT,
)
from code_indexer.server.services.audit_events import AUDIT_ACTION_CATALOG

_PROBE = Path(__file__).with_name("_audit_route_table_probe.py")
_SRC_ROOT = Path(code_indexer.__file__).resolve().parent.parent

_ADMIN_PERMISSIONS = frozenset(
    {"manage_users", "manage_golden_repos", "repository:admin", "public"}
)
_GATE_DEPENDENCIES = frozenset(
    {
        "get_current_admin_user",
        "get_current_admin_user_hybrid",
        "require_elevation.<locals>._check",
        "require_self_elevation",
        "require_admin_session",
        "_require_admin",
        "_git_credential_self_elevation",
    }
)
_BODY_GATES = frozenset({"_require_admin_session", "_check_elevation_window"})
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MAX_HELPER_DEPTH = 4

# ---------------------------------------------------------------------------
# Evidence: the names a door must reach for each action type
# ---------------------------------------------------------------------------

_ENTRY_POINTS: Dict[str, FrozenSet[str]] = {
    name: frozenset(points)
    for name, points in {
        "authentication_success": {"complete_login"},
        "authentication_failure": {"reject_login"},
        "mfa_activated": {"activate_mfa_and_issue_recovery_codes"},
        "mfa_recovery_codes_regenerated": {"regenerate_recovery_codes"},
        "mfa_disabled": {"disable_mfa"},
        "mfa_secret_regenerated_cross_user": {"regenerate_secret_cross_user"},
        "elevation_granted": {"step_up"},
        "elevation_failed": {"step_up"},
        "user_created": {"create_user_audited"},
        "user_deleted": {"delete_user_audited"},
        "user_role_changed": {"update_user_role_audited"},
        "user_password_reset_by_admin": {"change_password_audited"},
        "user_email_changed": {"update_user_email_audited"},
        "group_tool_access_granted": {"set_tool_access"},
        "group_tool_access_revoked": {"set_tool_access", "set_tool_access_all_groups"},
        "api_key_created": {"generate_key_audited"},
        "api_key_deleted": {"delete_api_key_audited"},
        "mcp_credential_created": {"generate_credential_audited"},
        "mcp_credential_revoked": {"revoke_credential_audited"},
        "ssh_key_created": {"create_key_audited"},
        "ssh_key_deleted": {"delete_key_audited"},
        "ssh_key_host_assigned": {"assign_key_to_host_audited"},
        "git_credential_configured": {"configure_credential_audited"},
        "git_credential_deleted": {"delete_credential_audited"},
        "golden_repo_added": {"add_golden_repo"},
        "golden_repo_removed": {"remove_golden_repo"},
        "golden_repo_refreshed": {"request_golden_repo_refresh"},
        "golden_repo_index_added": {
            "add_index_to_golden_repo",
            "add_indexes_to_golden_repo",
            "submit_provider_scoped_index_job",
        },
        "golden_repo_branch_changed": {"change_branch_async"},
        "provider_index_added": {"submit_provider_index_job"},
        "provider_index_recreated": {"submit_provider_index_job"},
        "provider_index_removed": {"remove_provider_index_audited"},
        "provider_index_bulk_added": {"bulk_add_provider_index_audited"},
        "config_changed": {
            "apply_audited_change",
            "update_settings_audited",
            "update_totp_elevation_audited",
            "reset_to_defaults_audited",
            "set_config",
            # SIEM trusted CA (one apply_audited_change each)
            "set_trusted_ca",
            "remove_trusted_ca",
        },
        "git_settings_changed": {"apply_git_settings_change"},
        "provider_api_key_set": {"apply_audited_change"},
        "provider_api_key_cleared": {"apply_audited_change"},
        "ci_token_set": {"save_token_audited"},
        "ci_token_deleted": {"delete_token_audited"},
        "maintenance_mode_entered": {"enter_maintenance_mode_audited"},
        "maintenance_mode_exited": {"exit_maintenance_mode_audited"},
        "server_restart_requested": {"request_server_restart"},
        "user_repo_activated_by_admin": {"activate_repository_for_user"},
        "user_repo_deactivated_by_admin": {"deactivate_repository_for_user"},
        # SIEM delivery self-reports (audited in the shared admin service).
        "siem_canary_sent": {"run_canary"},
        "siem_canary_visibility_confirmed": {"confirm_visible"},
        "siem_quarantine_requeued": {"requeue_quarantined", "record_requeue_event"},
        "siem_delivery_resumed": {"resume"},
        "siem_batch_acknowledged": {"acknowledge_batch"},
        "siem_batch_rebatched": {"rebatch_batch"},
        "siem_destination_retargeted": {"retarget_destination"},
        "siem_destination_abandoned": {"abandon_destination"},
        "siem_credential_changed": {"set_credential", "remove_credential"},
        # Legacy types whose rows now come from one audited entry point.
        "user_group_change": {"assign_user_to_group_audited"},
        "repo_access_revoke": {
            "revoke_repo_access_audited",
            "revoke_repos_access_audited",
        },
    }.items()
}


def _evidence(action_type: str) -> FrozenSet[str]:
    """Names proving a door reaches *action_type*'s emission."""
    spec = AUDIT_ACTION_CATALOG.get(action_type)
    if spec is not None and spec.details_schema is not None:
        return _ENTRY_POINTS[action_type]
    # Legacy writers name the type literally or via ``log_<type>``, or call
    # the type's audited entry point.
    return frozenset(
        {action_type, f"log_{action_type}", *_ENTRY_POINTS.get(action_type, ())}
    )


_ALL_EVIDENCE: FrozenSet[str] = frozenset(
    name for action in AUDIT_ACTION_CATALOG for name in _evidence(action)
)

# ---------------------------------------------------------------------------
# MCP doors
# ---------------------------------------------------------------------------

_LOGIN = ("authentication_success", "authentication_failure")
_ELEVATION = ("elevation_granted", "elevation_failed")

_MCP_MAPPED: Dict[str, Tuple[str, ...]] = {
    "authenticate": _LOGIN,
    "elevate_session": _ELEVATION,
    "create_user": ("user_created",),
    "create_api_key": ("api_key_created",),
    "delete_api_key": ("api_key_deleted",),
    "manage_mcp_credential": ("mcp_credential_created", "mcp_credential_revoked"),
    "manage_ssh_key": ("ssh_key_created", "ssh_key_deleted", "ssh_key_host_assigned"),
    "configure_git_credential": ("git_credential_configured",),
    "delete_git_credential": ("git_credential_deleted",),
    "create_group": ("group_create",),
    "update_group": ("group_update",),
    "delete_group": ("group_delete",),
    "manage_group_members": ("user_group_change",),
    "manage_group_repos": ("repo_access_grant", "repo_access_revoke"),
    "set_session_impersonation": (
        "impersonation_set",
        "impersonation_cleared",
        "impersonation_denied",
    ),
    "add_golden_repo": ("golden_repo_added",),
    "remove_golden_repo": ("golden_repo_removed",),
    "refresh_golden_repo": ("golden_repo_refreshed",),
    "add_golden_repo_index": ("golden_repo_index_added",),
    "change_golden_repo_branch": ("golden_repo_branch_changed",),
    "manage_provider_indexes": (
        "provider_index_added",
        "provider_index_recreated",
        "provider_index_removed",
    ),
    "bulk_add_provider_index": ("provider_index_bulk_added",),
    "set_global_config": ("config_changed",),
}

# Tools that authenticate the caller themselves (no permission gate).
_SELF_AUTHENTICATING_MCP = frozenset({"elevate_session"})

_READ_ONLY = READ_ONLY
_WORKSPACE_GIT = WORKSPACE_GIT

_MCP_EXEMPT: Dict[str, str] = {
    "admin_embedding_stats_query": _READ_ONLY,
    "admin_logs_export": _READ_ONLY,
    "admin_logs_query": _READ_ONLY,
    "get_group": _READ_ONLY,
    "get_memory_governor_stats": _READ_ONLY,
    "get_provider_health": _READ_ONLY,
    "list_groups": _READ_ONLY,
    "list_mcp_credentials": _READ_ONLY,
    "list_ssh_keys": _READ_ONLY,
    "list_users": _READ_ONLY,
    "query_audit_logs": _READ_ONLY,
    "scip_cleanup_history": _READ_ONLY,
    "scip_cleanup_status": _READ_ONLY,
    "scip_pr_history": _READ_ONLY,
    "git_branch_delete": _WORKSPACE_GIT,
    "git_clean": _WORKSPACE_GIT,
    "git_reset": _WORKSPACE_GIT,
    "scip_cleanup_workspaces": SCIP_SCRATCH_CLEANUP,
    "trigger_dependency_analysis": (
        "starts a dependency-map analysis job; rewrites generated domain "
        "documents, not access or configuration"
    ),
}

_MCP_GAPS: Dict[str, str] = {}

# ---------------------------------------------------------------------------
# REST and Web doors (keys are "METHOD /path" as mounted)
# ---------------------------------------------------------------------------

_USER_GROUP = ("user_group_change",)
_SSH_CREATE = ("ssh_key_created",)
_SSH_DELETE = ("ssh_key_deleted",)
_SSH_ASSIGN = ("ssh_key_host_assigned",)
_GIT_SET = ("git_credential_configured",)
_GIT_DELETE = ("git_credential_deleted",)
_CONFIG = ("config_changed",)
_KEY_SET = ("provider_api_key_set",)
_KEY_CLEARED = ("provider_api_key_cleared",)
_USER_REPO_REMOVED = ("user_repo_deactivated_by_admin",)

# Doors that authenticate the caller themselves: no admin/elevation gate.
_SELF_AUTHENTICATING_ROUTES: Dict[str, Tuple[str, ...]] = {
    "POST /auth/login": _LOGIN,
    "POST /auth/mfa/verify": _LOGIN,
    "POST /login": _LOGIN,
    "POST /admin/mfa/challenge/verify": _LOGIN,
    "GET /auth/sso/callback": _LOGIN,
    "POST /oauth/authorize": _LOGIN,
    "POST /oauth/mfa/verify": _LOGIN,
    "POST /auth/register": ("user_created",),
    "POST /auth/elevate": _ELEVATION,
    "POST /auth/elevate-form": _ELEVATION,
    "POST /auth/elevate-ajax": _ELEVATION,
    "POST /admin/mfa/verify": ("mfa_activated",),
    "POST /user/mfa/verify": ("mfa_activated",),
    "GET /admin/mfa/recovery-codes": ("mfa_recovery_codes_regenerated",),
    "GET /user/mfa/recovery-codes": ("mfa_recovery_codes_regenerated",),
    "POST /user/mfa/disable": ("mfa_disabled",),
    "GET /admin/mfa/setup": ("mfa_secret_regenerated_cross_user",),
    "PUT /api/users/change-password": (
        "password_change_success",
        "password_change_failure",
    ),
}

_ROUTE_MAPPED: Dict[str, Tuple[str, ...]] = {
    **_SELF_AUTHENTICATING_ROUTES,
    # Users
    "POST /api/admin/users": ("user_created",),
    "PUT /api/admin/users/{username}": ("user_role_changed",),
    "DELETE /api/admin/users/{username}": ("user_deleted",),
    "PUT /api/admin/users/{username}/change-password": (
        "user_password_reset_by_admin",
    ),
    "POST /admin/users/create": ("user_created",),
    "POST /admin/users/{username}/role": ("user_role_changed",),
    "POST /admin/users/{username}/password": ("user_password_reset_by_admin",),
    "POST /admin/users/{username}/email": ("user_email_changed",),
    "POST /admin/users/{username}/delete": ("user_deleted",),
    "POST /admin/mfa/disable": ("mfa_disabled",),
    # Groups and permissions
    "POST /api/v1/groups": ("group_create",),
    "PUT /api/v1/groups/{group_id}": ("group_update",),
    "DELETE /api/v1/groups/{group_id}": ("group_delete",),
    "PUT /api/v1/users/{user_id}/group": _USER_GROUP,
    "POST /api/v1/groups/{group_id}/members": _USER_GROUP,
    "POST /api/v1/groups/{group_id}/repos": ("repo_access_grant",),
    "DELETE /api/v1/groups/{group_id}/repos": ("repo_access_revoke",),
    "DELETE /api/v1/groups/{group_id}/repos/{repo_name}": ("repo_access_revoke",),
    "POST /api/v1/groups/tool-access/{group_id}/{tool_name}": (
        "group_tool_access_granted",
    ),
    "DELETE /api/v1/groups/tool-access/{group_id}/{tool_name}": (
        "group_tool_access_revoked",
    ),
    "POST /api/v1/groups/tool-access/{tool_name}/bulk-disable": (
        "group_tool_access_revoked",
    ),
    "POST /admin/groups/create": ("group_create",),
    "POST /admin/groups/{group_id}/update": ("group_update",),
    "POST /admin/groups/{group_id}/delete": ("group_delete",),
    "POST /admin/groups/users/{user_id:path}/assign": _USER_GROUP,
    "POST /admin/groups/repo-access/grant": ("repo_access_grant",),
    "POST /admin/groups/repo-access/revoke": ("repo_access_revoke",),
    # Credentials
    "POST /api/keys": ("api_key_created",),
    "DELETE /api/keys/{key_id}": ("api_key_deleted",),
    "POST /api/mcp-credentials": ("mcp_credential_created",),
    "DELETE /api/mcp-credentials/{credential_id}": ("mcp_credential_revoked",),
    "POST /api/admin/users/{username}/mcp-credentials": ("mcp_credential_created",),
    "DELETE /api/admin/users/{username}/mcp-credentials/{credential_id}": (
        "mcp_credential_revoked",
    ),
    "POST /api/ssh-keys": _SSH_CREATE,
    "DELETE /api/ssh-keys/{name}": _SSH_DELETE,
    "POST /api/ssh-keys/{name}/hosts": _SSH_ASSIGN,
    "POST /admin/ssh-keys/create": _SSH_CREATE,
    "POST /admin/ssh-keys/delete": _SSH_DELETE,
    "POST /admin/ssh-keys/assign-host": _SSH_ASSIGN,
    "POST /admin/git-credentials": _GIT_SET,
    "DELETE /admin/git-credentials/{credential_id}": _GIT_DELETE,
    "POST /user/git-credentials": _GIT_SET,
    "DELETE /user/git-credentials/{credential_id}": _GIT_DELETE,
    # Golden repositories and provider indexes
    "POST /api/admin/golden-repos": ("golden_repo_added",),
    "DELETE /api/admin/golden-repos/{alias}": ("golden_repo_removed",),
    "POST /api/admin/golden-repos/{alias}/refresh": ("golden_repo_refreshed",),
    "POST /api/admin/golden-repos/{alias}/indexes": ("golden_repo_index_added",),
    "POST /admin/golden-repos/add": ("golden_repo_added",),
    "POST /admin/golden-repos/batch-create": ("golden_repo_added",),
    "POST /admin/golden-repos/{alias}/delete": ("golden_repo_removed",),
    "POST /admin/golden-repos/{alias}/refresh": ("golden_repo_refreshed",),
    "POST /admin/golden-repos/{alias}/force-resync": ("golden_repo_refreshed",),
    "POST /admin/golden-repos/{alias}/change-branch": ("golden_repo_branch_changed",),
    "POST /api/admin/provider-indexes/add": ("provider_index_added",),
    "POST /api/admin/provider-indexes/recreate": ("provider_index_recreated",),
    "POST /api/admin/provider-indexes/remove": ("provider_index_removed",),
    "POST /api/admin/provider-indexes/bulk-add": ("provider_index_bulk_added",),
    # Configuration and server secrets
    "PUT /global/config": _CONFIG,
    "PUT /api/settings/git": ("git_settings_changed",),
    "POST /api/llm-creds/save-config": _CONFIG,
    "POST /api/api-keys/anthropic": _KEY_SET,
    "POST /api/api-keys/voyageai": _KEY_SET,
    "POST /api/api-keys/cohere": _KEY_SET,
    "DELETE /api/api-keys/anthropic": _KEY_CLEARED,
    "DELETE /api/api-keys/voyageai": _KEY_CLEARED,
    "DELETE /api/api-keys/cohere": _KEY_CLEARED,
    "POST /admin/config/{section}": _CONFIG,
    "POST /admin/config/reset": _CONFIG,
    "POST /admin/config/langfuse_pull": _CONFIG,
    "POST /admin/config/cidx_meta_backup": _CONFIG,
    "POST /admin/config/siem_delivery/trusted_ca": _CONFIG,
    "POST /admin/config/siem_delivery/trusted_ca/remove": _CONFIG,
    "POST /admin/config/siem_delivery/credential": ("siem_credential_changed",),
    "POST /admin/config/siem_delivery/credential/remove": ("siem_credential_changed",),
    "POST /admin/self-monitoring": _CONFIG,
    "POST /admin/config/api-keys/{platform}": ("ci_token_set",),
    "DELETE /admin/config/api-keys/{platform}": ("ci_token_deleted",),
    # SIEM delivery admin actions
    "POST /api/admin/siem-delivery/canary": ("siem_canary_sent",),
    "POST /api/admin/siem-delivery/canary/confirm-visible": (
        "siem_canary_visibility_confirmed",
    ),
    "POST /api/admin/siem-delivery/resume": ("siem_delivery_resumed",),
    "POST /api/admin/siem-delivery/quarantine/requeue": ("siem_quarantine_requeued",),
    "POST /api/admin/siem-delivery/batches/{batch_id}/acknowledge": (
        "siem_batch_acknowledged",
    ),
    "POST /api/admin/siem-delivery/batches/{batch_id}/rebatch": (
        "siem_batch_rebatched",
    ),
    "POST /api/admin/siem-delivery/destinations/{destination_key}/retarget": (
        "siem_destination_retargeted",
    ),
    "POST /api/admin/siem-delivery/destinations/{destination_key}/abandon": (
        "siem_destination_abandoned",
    ),
    # The same SIEM actions through the Web UI (same shared service, same types)
    "POST /admin/siem-delivery/canary": ("siem_canary_sent",),
    "POST /admin/siem-delivery/canary/confirm-visible": (
        "siem_canary_visibility_confirmed",
    ),
    "POST /admin/siem-delivery/resume": ("siem_delivery_resumed",),
    "POST /admin/siem-delivery/quarantine/requeue": ("siem_quarantine_requeued",),
    "POST /admin/siem-delivery/batches/{batch_id}/acknowledge": (
        "siem_batch_acknowledged",
    ),
    "POST /admin/siem-delivery/batches/{batch_id}/rebatch": ("siem_batch_rebatched",),
    "POST /admin/siem-delivery/destinations/{destination_key}/retarget": (
        "siem_destination_retargeted",
    ),
    "POST /admin/siem-delivery/destinations/{destination_key}/abandon": (
        "siem_destination_abandoned",
    ),
    # Server operations
    "POST /api/admin/maintenance/enter": ("maintenance_mode_entered",),
    "POST /api/admin/maintenance/exit": ("maintenance_mode_exited",),
    "POST /admin/restart": ("server_restart_requested",),
    # Activated repositories an admin manages for another user
    "POST /admin/golden-repos/activate": ("user_repo_activated_by_admin",),
    "DELETE /api/admin/activated-repos/{username}/{user_alias}": _USER_REPO_REMOVED,
    "POST /admin/repos/{username}/{user_alias}/deactivate": _USER_REPO_REMOVED,
    # Token and OAuth flows that write their existing (legacy) rows
    "POST /api/auth/refresh": ("token_refresh_success", "token_refresh_failure"),
    "POST /oauth/authorize/consent": ("oauth_authorization",),
    "POST /oauth/register": ("oauth_client_registration",),
    "POST /oauth/revoke": ("oauth_token_revocation",),
    "POST /oauth/token": ("oauth_token_exchange",),
}

# The written reasons live in ``_audit_route_classification``.
_ROUTE_EXEMPT: Dict[str, str] = ROUTE_EXEMPT

_ROUTE_SELF_SERVICE: Dict[str, str] = ROUTE_SELF_SERVICE

# Doors with security weight and NO audit row: must stay empty.
_ROUTE_GAPS: Dict[str, str] = {}

_ROUTE_TABLES = (_ROUTE_MAPPED, _ROUTE_EXEMPT, _ROUTE_SELF_SERVICE, _ROUTE_GAPS)

# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

RouteEntry = Mapping[str, object]


def _method(key: str) -> str:
    return key.split(" ", 1)[0]


def _body_calls(source: str) -> Set[str]:
    called: Set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


def _is_gated_route(entry: RouteEntry) -> bool:
    """A mutating route behind an admin / elevation gate."""
    if _method(str(entry["key"])) not in _MUTATING:
        return False
    dependencies = set(entry["dependencies"])  # type: ignore[call-overload]
    permissions = set(entry["permissions"])  # type: ignore[call-overload]
    return bool(
        dependencies & _GATE_DEPENDENCIES
        or permissions & _ADMIN_PERMISSIONS
        or _body_calls(str(entry["source"])) & _BODY_GATES
    )


def route_inventory(entries: Iterable[RouteEntry]) -> Set[str]:
    """EVERY registered mutating route, plus the self-authenticating doors present.

    No gate detection decides membership: a route guarded by a helper that
    no list names is inventoried all the same.
    """
    by_key = {str(e["key"]) for e in entries}
    mutating = {key for key in by_key if _method(key) in _MUTATING}
    return mutating | (set(_SELF_AUTHENTICATING_ROUTES) & by_key)


def self_service_behind_a_gate(
    entries: Iterable[RouteEntry], self_service: Mapping[str, str]
) -> List[str]:
    """Self-service classifications of routes behind a known admin gate."""
    return sorted(
        str(e["key"])
        for e in entries
        if str(e["key"]) in self_service and _is_gated_route(e)
    )


def mcp_inventory(
    tool_registry: Mapping[str, Mapping[str, object]],
    handler_registry: Mapping[str, Callable[..., object]],
) -> Set[str]:
    """Admin-permission tools, elevation-marked handlers, self-auth tools."""
    by_permission = {
        name
        for name, spec in tool_registry.items()
        if spec.get("required_permission") in _ADMIN_PERMISSIONS
    }
    marked = {
        name
        for name, handler in handler_registry.items()
        if getattr(handler, "__mcp_requires_session_key__", False)
    }
    return by_permission | marked | (_SELF_AUTHENTICATING_MCP & set(handler_registry))


def unclassified(inventory: Set[str], *tables: Mapping[str, object]) -> List[str]:
    classified: Set[str] = set()
    for table in tables:
        classified |= set(table)
    return sorted(inventory - classified)


# ---------------------------------------------------------------------------
# Reachability: what a door's handler (and its same-module helpers) names
# ---------------------------------------------------------------------------


def _identifiers(node: ast.AST) -> Set[str]:
    found: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value)
    return found


@lru_cache(maxsize=None)
def _module_functions(module_path: str) -> Dict[str, Tuple[ast.AST, ...]]:
    table: Dict[str, List[ast.AST]] = {}
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            table.setdefault(node.name, []).append(node)
    return {name: tuple(nodes) for name, nodes in table.items()}


def reachable_names(source: str, module_path: str) -> Set[str]:
    """Names the handler source and its same-module helpers mention.

    Helpers are followed by name to a bounded depth, so the walk always
    terminates.
    """
    functions = _module_functions(module_path)
    names = _identifiers(ast.parse(source))
    frontier = set(names)
    visited: Set[str] = set()
    for _depth in range(_MAX_HELPER_DEPTH):
        next_frontier: Set[str] = set()
        for name in sorted((frontier & set(functions)) - visited):
            visited.add(name)
            for node in functions[name]:
                next_frontier |= _identifiers(node)
        next_frontier -= names
        if not next_frontier:
            break
        names |= next_frontier
        frontier = next_frontier
    return names


def missing_emissions(
    mapped: Mapping[str, Tuple[str, ...]], reached: Mapping[str, Set[str]]
) -> List[str]:
    """Mapped (door, action type) pairs whose emission the door never names."""
    return sorted(
        f"{door} -> {action_type}"
        for door, action_types in mapped.items()
        for action_type in action_types
        if not (_evidence(action_type) & reached[door])
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def route_entries(tmp_path_factory: pytest.TempPathFactory) -> List[RouteEntry]:
    """The production route table, built in its own process."""
    base = tmp_path_factory.mktemp("route-table")
    (base / "server").mkdir()
    out = base / "routes.json"
    env = dict(os.environ)
    env["CIDX_SERVER_DATA_DIR"] = str(base / "server")
    env["CIDX_DATA_DIR"] = str(base / "data")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_SRC_ROOT), env.get("PYTHONPATH", "")) if p
    )
    result = subprocess.run(
        [sys.executable, str(_PROBE), str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    entries: List[RouteEntry] = json.loads(out.read_text(encoding="utf-8"))
    assert entries, "the route table is empty"
    return entries


@pytest.fixture(scope="module")
def mcp_registries() -> Tuple[Dict[str, Dict[str, object]], Dict[str, Callable]]:
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    return TOOL_REGISTRY, HANDLER_REGISTRY


def _route_reach(entries: List[RouteEntry]) -> Dict[str, Set[str]]:
    reached: Dict[str, Set[str]] = {}
    for entry in entries:
        key = str(entry["key"])
        names = reachable_names(str(entry["source"]), str(entry["file"]))
        reached[key] = reached.get(key, set()) | names
    return reached


def _mcp_reach(handlers: Mapping[str, Callable]) -> Dict[str, Set[str]]:
    reached: Dict[str, Set[str]] = {}
    for name, handler in handlers.items():
        target = inspect.unwrap(handler)
        source = inspect.getsource(target)
        reached[name] = reachable_names(
            textwrap.dedent(source), str(inspect.getsourcefile(target))
        )
    return reached


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_mcp_door_is_mapped_exempt_or_a_known_gap(mcp_registries) -> None:
    tools, handlers = mcp_registries
    inventory = mcp_inventory(tools, handlers)
    assert unclassified(inventory, _MCP_MAPPED, _MCP_EXEMPT, _MCP_GAPS) == []


def test_every_mutating_route_is_mapped_exempt_or_self_service(route_entries) -> None:
    inventory = route_inventory(route_entries)
    assert unclassified(inventory, *_ROUTE_TABLES) == []


def test_the_conditionally_mounted_fault_injection_router_is_inventoried(
    route_entries,
) -> None:
    """wire_fault_injection mounts it only when enabled; it is inventoried anyway."""
    from code_indexer.server.fault_injection.router import router as fault_router

    expected = {
        f"{method} {getattr(route, 'path', '')}"
        for route in fault_router.routes
        for method in getattr(route, "methods", ())
        if method in _MUTATING
    }
    assert len(expected) == 7
    assert expected <= route_inventory(route_entries)


def test_no_self_service_route_sits_behind_an_admin_gate(route_entries) -> None:
    assert self_service_behind_a_gate(route_entries, _ROUTE_SELF_SERVICE) == []


def test_no_door_is_left_as_a_known_gap() -> None:
    assert (_ROUTE_GAPS, _MCP_GAPS) == ({}, {})


def test_no_classification_names_a_door_outside_the_inventory(
    mcp_registries, route_entries
) -> None:
    """Stale entries (a removed or renamed door) are reported."""
    tools, handlers = mcp_registries
    mcp_doors = mcp_inventory(tools, handlers)
    route_doors = route_inventory(route_entries)
    stale = [
        f"mcp:{name}"
        for table in (_MCP_MAPPED, _MCP_EXEMPT, _MCP_GAPS)
        for name in table
        if name not in mcp_doors
    ] + [key for table in _ROUTE_TABLES for key in table if key not in route_doors]
    assert stale == []


def test_the_classifications_are_disjoint() -> None:
    for tables in ((_MCP_MAPPED, _MCP_EXEMPT, _MCP_GAPS), _ROUTE_TABLES):
        seen: List[str] = [key for table in tables for key in table]
        assert len(seen) == len(set(seen))


def test_every_mapped_action_type_is_in_the_catalog() -> None:
    mapped = {
        action
        for table in (_MCP_MAPPED, _ROUTE_MAPPED)
        for actions in table.values()
        for action in actions
    }
    assert sorted(mapped - set(AUDIT_ACTION_CATALOG)) == []
    assert sorted(set(_ENTRY_POINTS) - set(AUDIT_ACTION_CATALOG)) == []


def test_every_exemption_self_service_and_gap_has_a_reason() -> None:
    for table in (
        _MCP_EXEMPT,
        _MCP_GAPS,
        _ROUTE_EXEMPT,
        _ROUTE_SELF_SERVICE,
        _ROUTE_GAPS,
    ):
        assert all(reason.strip() for reason in table.values())


def test_every_mapped_mcp_door_reaches_its_emission(mcp_registries) -> None:
    _tools, handlers = mcp_registries
    reached = _mcp_reach({name: handlers[name] for name in _MCP_MAPPED})
    assert missing_emissions(_MCP_MAPPED, reached) == []


def test_every_mapped_route_door_reaches_its_emission(route_entries) -> None:
    assert missing_emissions(_ROUTE_MAPPED, _route_reach(route_entries)) == []


def test_every_allowlisted_catalog_type_is_claimed_by_a_door() -> None:
    claimed = {
        action
        for table in (_MCP_MAPPED, _ROUTE_MAPPED)
        for actions in table.values()
        for action in actions
    }
    allowlisted = {
        name
        for name, spec in AUDIT_ACTION_CATALOG.items()
        if spec.details_schema is not None
    }
    assert sorted(allowlisted - claimed) == []


def test_known_gaps_still_reach_no_audit_emission(
    mcp_registries, route_entries
) -> None:
    """A gap that gains an emission must move to the mapped table."""
    _tools, handlers = mcp_registries
    route_reach = _route_reach(route_entries)
    mcp_reach = _mcp_reach({name: handlers[name] for name in _MCP_GAPS})
    emitting = [key for key in _ROUTE_GAPS if route_reach[key] & _ALL_EVIDENCE] + [
        f"mcp:{name}" for name in _MCP_GAPS if mcp_reach[name] & _ALL_EVIDENCE
    ]
    assert emitting == []


# ---------------------------------------------------------------------------
# Negative controls
# ---------------------------------------------------------------------------


def _dummy_handler(args, user):  # type: ignore[no-untyped-def]
    return {}


def test_an_unmapped_admin_tool_is_reported(mcp_registries) -> None:
    tools, handlers = mcp_registries
    tools = {**tools, "example_dummy_tool": {"required_permission": "manage_users"}}
    handlers = {**handlers, "example_dummy_tool": _dummy_handler}
    inventory = mcp_inventory(tools, handlers)
    assert unclassified(inventory, _MCP_MAPPED, _MCP_EXEMPT, _MCP_GAPS) == [
        "example_dummy_tool"
    ]


def test_an_unmapped_elevation_marked_handler_is_reported() -> None:
    def marked(args, user):  # type: ignore[no-untyped-def]
        return {}

    marked.__mcp_requires_session_key__ = True  # type: ignore[attr-defined]
    tools = {"example_marked": {"required_permission": "query_repos"}}
    inventory = mcp_inventory(tools, {"example_marked": marked})
    assert unclassified(inventory, _MCP_MAPPED) == ["example_marked"]


def test_an_unmapped_gated_route_is_reported(route_entries) -> None:
    dummy = {
        "key": "POST /api/admin/example-dummy",
        "dependencies": ["get_current_admin_user_hybrid"],
        "permissions": [],
        "source": "def example(): pass\n",
    }
    body_gated = {
        "key": "POST /admin/example-body-gated",
        "dependencies": [],
        "permissions": [],
        "source": "def example(request):\n    _require_admin_session(request)\n",
    }
    inventory = route_inventory([*route_entries, dummy, body_gated])
    assert unclassified(inventory, *_ROUTE_TABLES) == [
        "POST /admin/example-body-gated",
        "POST /api/admin/example-dummy",
    ]


def test_a_new_route_with_an_unknown_admin_helper_is_reported(route_entries) -> None:
    """A gate the name lists do not know still cannot hide a mutating route."""
    unknown_helper = {
        "key": "POST /api/admin/example-unknown-helper",
        "dependencies": ["_example_unrecognised_admin_guard"],
        "permissions": [],
        "source": "def example(request):\n    _example_verify_admin(request)\n",
    }
    assert _is_gated_route(unknown_helper) is False  # the name lists miss it
    inventory = route_inventory([*route_entries, unknown_helper])
    assert unclassified(inventory, *_ROUTE_TABLES) == [
        "POST /api/admin/example-unknown-helper"
    ]


def test_a_read_route_is_not_inventoried_but_any_mutating_route_is() -> None:
    entries = [
        {
            "key": "GET /api/admin/example",
            "dependencies": ["get_current_admin_user"],
            "permissions": [],
            "source": "def example(): pass\n",
        },
        {
            "key": "POST /api/example",
            "dependencies": ["get_current_user"],
            "permissions": ["repository:write"],
            "source": "def example(): pass\n",
        },
        {
            "key": "PATCH /api/example/public",
            "dependencies": [],
            "permissions": [],
            "source": "def example(): pass\n",
        },
    ]
    assert route_inventory(entries) == {
        "POST /api/example",
        "PATCH /api/example/public",
    }


def test_a_self_service_entry_behind_an_admin_gate_is_reported() -> None:
    gated = {
        "key": "POST /api/example-admin-only",
        "dependencies": ["get_current_admin_user"],
        "permissions": [],
        "source": "def example(): pass\n",
    }
    ungated = {**gated, "key": "POST /api/example-own", "dependencies": []}
    self_service = {
        "POST /api/example-admin-only": "reason",
        "POST /api/example-own": "reason",
    }
    assert self_service_behind_a_gate([gated, ungated], self_service) == [
        "POST /api/example-admin-only"
    ]


def test_a_mapped_door_without_its_emission_is_reported() -> None:
    mapped = {"DELETE /api/example/{name}": ("user_deleted", "group_delete")}
    reached = {"DELETE /api/example/{name}": {"delete_user", "group_delete"}}
    assert missing_emissions(mapped, reached) == [
        "DELETE /api/example/{name} -> user_deleted"
    ]


def test_reachability_follows_same_module_helpers(tmp_path: Path) -> None:
    module = tmp_path / "example_door.py"
    module.write_text(
        "def door(m):\n    return _helper(m)\n\n"
        "def _helper(m):\n    return m.delete_user_audited('x', actor='a')\n"
        "def unrelated(m):\n    return m.create_user_audited()\n",
        encoding="utf-8",
    )
    names = reachable_names("def door(m):\n    return _helper(m)\n", str(module))
    assert "delete_user_audited" in names
    assert "create_user_audited" not in names
