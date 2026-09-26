"""Shared repo-level access-control guard for REST/MCP front doors.

Every REST route (and MCP tool) that resolves a caller-supplied golden-repo
alias (or list of aliases) must apply the same group-based repo
access-control model that mcp/protocol.py's _check_repository_access() and
routers/inline_query.py enforce.

This module does NOT reimplement group-membership traversal. It calls
AccessFilteringService.is_admin_user() / get_accessible_repos() -- the
same methods _check_repository_access() and inline_query.py already use
-- and applies the identical '-global' suffix normalisation and admin
bypass semantics, so every caller of this helper gets the SAME access
decision as the MCP dispatcher's central guard.

Framework-agnostic on purpose (no FastAPI import): REST callers catch
RepoAccessDeniedError / AccessFilteringServiceUnavailableError and shape
their own HTTPException; MCP callers catch them and shape their own
JSON-RPC / tool-response error.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Union


def normalize_repo_alias(alias: str) -> str:
    """Strip a trailing '-global' suffix to match stored (bare) repo names.

    Mirrors mcp/protocol.py's _check_repository_access() nested
    _normalize() helper and AccessFilteringService._get_repo_alias()'s
    identical suffix-stripping behaviour -- kept in exact sync with both;
    do not diverge.
    """
    if alias.endswith("-global"):
        return alias[: -len("-global")]
    return alias


class RepoAccessDeniedError(Exception):
    """Raised when the caller lacks access to a requested repository alias.

    ``alias`` is the RAW (pre-normalisation) alias as supplied by the
    caller. The message format matches mcp/protocol.py's
    _check_repository_access()._deny_single() exactly, so REST and MCP
    callers using this guard surface an identical message to the one the
    MCP dispatcher's own tools already produce.
    """

    def __init__(self, alias: str, username: str):
        self.alias = alias
        self.username = username
        super().__init__(
            f"Access denied: repository '{alias}' is not accessible to user "
            f"'{username}'"
        )


class AccessFilteringServiceUnavailableError(Exception):
    """Raised when access_filtering_service is not wired on app.state.

    Fail-closed (mirrors the Story #331 AC9 convention already used by
    mcp/protocol.py's handle_tools_call): a repo-scoped capability must
    never silently skip the access check just because the service was not
    constructed for this process -- callers MUST turn this into a 403/500,
    never proceed as if the caller had access.
    """


def require_repo_access(
    access_filtering_service: Optional[object],
    username: str,
    aliases: Union[str, Iterable[str], None],
) -> None:
    """Enforce repository-level access for one or more aliases.

    Accepts a single alias string or an iterable of alias strings (the
    "omni"/multi-repo form). Empty/None entries and an empty/None/missing
    ``aliases`` value are treated as "nothing to check" (matching
    _check_repository_access()'s "no repo param present -- nothing to
    check" behaviour), NOT as a denial.

    Admin users (access_filtering_service.is_admin_user()) bypass the
    check entirely, matching every other front door.

    Checks every alias in iteration order and raises on the FIRST one the
    caller cannot access -- callers must never silently drop unauthorized
    aliases from a multi-repo request and proceed with the rest (no
    silent partial results).

    Raises:
        AccessFilteringServiceUnavailableError: access_filtering_service is
            None -- callers MUST fail closed (403/500), never proceed as if
            the caller had access. Checked FIRST, unconditionally, before
            any early-return for empty/missing aliases: a caller
            passing an explicit empty alias
            list (e.g. ``repository_alias: []``) together with a missing
            access service must never resolve to "nothing to check, proceed"
            -- that would be a fail-OPEN path disguised as a no-op.
        RepoAccessDeniedError: the first alias (in iteration order) the
            caller cannot access.
    """
    if access_filtering_service is None:
        raise AccessFilteringServiceUnavailableError(
            "access_filtering_service unavailable -- repository access "
            "cannot be verified; failing closed"
        )

    if aliases is None:
        return

    if isinstance(aliases, str):
        alias_list: List[str] = [aliases]
    else:
        alias_list = list(aliases)

    non_empty = [a for a in alias_list if isinstance(a, str) and a]
    if not non_empty:
        return

    if access_filtering_service.is_admin_user(username):  # type: ignore[attr-defined]
        return

    accessible = access_filtering_service.get_accessible_repos(username)  # type: ignore[attr-defined]
    for raw_alias in non_empty:
        if normalize_repo_alias(raw_alias) not in accessible:
            raise RepoAccessDeniedError(raw_alias, username)
