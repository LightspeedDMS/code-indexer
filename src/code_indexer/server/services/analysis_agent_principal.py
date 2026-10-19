"""The least-privilege principal behind the analysis agents' MCP credential.

Description refresh, lifecycle and dependency-map agents call this server's
own MCP endpoint (cidx-local) while reading untrusted repository content, so
their credential belongs to a dedicated account, not to an administrator:

- role normal_user, in the powerusers GROUP, to which every golden
  repository is auto-assigned at registration, so its searches see every
  golden repository without any admins-group bypass (an operator revoking
  a repository from powerusers revokes it from the agents too);
- may call only the read/query tools in :data:`ANALYSIS_AGENT_TOOLS`,
  whatever its role or group says (enforced by the MCP dispatcher);
- usable only through its MCP credential on /mcp: never at a password or
  SSO door, never with a bearer token, cookie or API key, never
  auto-elevated.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

ANALYSIS_AGENT_USERNAME = "cidx-analysis-agent"

_PASSWORD_ENTROPY_BYTES = 48
_PASSWORD_CLASS_SUFFIX = "aA1!"

_PROVISIONING_ACTOR = "system:mcp-self-registration"

logger = logging.getLogger(__name__)


class PrincipalMismatchError(RuntimeError):
    """An account with the principal's name is not the expected principal."""


def ensure_analysis_agent_principal(user_manager: Any, group_manager: Any) -> None:
    """Create the principal when missing; idempotent, cluster-safe.

    The account gets a random password nobody learns. A peer node creating
    it first (any store error on create, followed by a re-read that finds
    the account) is not an error. An existing account with the name must
    look like the principal (role normal_user, no email, no SSO link);
    otherwise :class:`PrincipalMismatchError` is raised and no credential
    may be minted. The account is put in powerusers while it has no group or
    is in admins (earlier releases) or users (where seeding drops group-less
    accounts); any other group is an operator choice and is kept. Raises
    when the account cannot exist.
    """
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.services.audit_events import SystemComponent
    from code_indexer.server.services.constants import (
        DEFAULT_GROUP_ADMINS,
        DEFAULT_GROUP_POWERUSERS,
        DEFAULT_GROUP_USERS,
    )

    if user_manager.get_user(ANALYSIS_AGENT_USERNAME) is None:
        password = (
            secrets.token_urlsafe(_PASSWORD_ENTROPY_BYTES) + _PASSWORD_CLASS_SUFFIX
        )
        try:
            user_manager.create_user_audited(
                ANALYSIS_AGENT_USERNAME,
                password,
                UserRole.NORMAL_USER,
                actor=SystemComponent.MCP_SELF_REGISTRATION,
            )
        except Exception:
            if user_manager.get_user(ANALYSIS_AGENT_USERNAME) is None:
                raise
    account = user_manager.get_user(ANALYSIS_AGENT_USERNAME)
    if (
        account.role != UserRole.NORMAL_USER
        or account.email
        or user_manager.is_sso_user(ANALYSIS_AGENT_USERNAME)
    ):
        logger.error(
            "An account named %r exists but is not the analysis agents' "
            "principal (role, email or SSO link differ); no MCP credential "
            "is minted for it",
            ANALYSIS_AGENT_USERNAME,
        )
        raise PrincipalMismatchError(ANALYSIS_AGENT_USERNAME)
    current = group_manager.get_user_group(ANALYSIS_AGENT_USERNAME)
    if current is not None and current.name not in (
        DEFAULT_GROUP_ADMINS,
        DEFAULT_GROUP_USERS,
    ):
        logger.info(
            "Analysis agents' principal kept in operator-chosen group %r",
            current.name,
        )
        return
    powerusers = group_manager.get_group_by_name(DEFAULT_GROUP_POWERUSERS)
    if powerusers is None:
        raise RuntimeError(f"group {DEFAULT_GROUP_POWERUSERS!r} does not exist")
    group_manager.assign_user_to_group_audited(
        ANALYSIS_AGENT_USERNAME,
        powerusers.id,
        actor=_PROVISIONING_ACTOR,
        account_exists=lambda name: user_manager.get_user(name) is not None,
    )


def analysis_agent_tool_refusal(username: str, tool_name: str) -> Optional[str]:
    """The refusal for the principal calling a tool outside its allowlist."""
    if username != ANALYSIS_AGENT_USERNAME or tool_name in ANALYSIS_AGENT_TOOLS:
        return None
    return f"Permission denied: tool {tool_name} is not available to {username}"


ANALYSIS_AGENT_TOOLS = frozenset(
    {
        "search_code",
        "regex_search",
        "poll_search_job",
        "cidx_fetch_cached_payload",
        "get_cached_content",
        "scip_definition",
        "scip_references",
        "scip_dependencies",
        "scip_dependents",
        "scip_impact",
        "scip_callchain",
        "scip_context",
        "list_global_repos",
        "list_repositories",
        "list_repo_categories",
        "repository_status",
        "get_index_status",
        "get_branches",
        "browse_directory",
        "directory_tree",
        "list_files",
        "get_file_content",
        "git_log",
        "git_file_history",
        "git_show_commit",
        "git_blame",
        "git_diff",
        "git_file_at_revision",
        "git_search_commits",
        "git_search_diffs",
        "depmap_find_consumers",
        "depmap_get_cross_domain_graph",
        "depmap_get_domain_summary",
        "depmap_get_hub_domains",
        "depmap_get_repo_domains",
        "depmap_get_stale_domains",
        "cidx_quick_reference",
        "first_time_user_guide",
        "get_tool_categories",
    }
)
