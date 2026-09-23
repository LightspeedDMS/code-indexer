"""GroupsBackend Protocol (GroupAccessManager storage interface, Story #410).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class GroupsBackend(Protocol):
    """Protocol for group access management storage (GroupAccessManager interface)."""

    def get_all_groups(self) -> list: ...

    def get_group(self, group_id: int) -> Optional[Any]: ...

    def get_group_by_name(self, name: str) -> Optional[Any]: ...

    def create_group(self, name: str, description: str) -> Any: ...

    def update_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Any]: ...

    def delete_group(self, group_id: int) -> bool: ...

    def assign_user_to_group(
        self, user_id: str, group_id: int, assigned_by: str
    ) -> None: ...

    def remove_user_from_group(self, user_id: str, group_id: int) -> bool: ...

    def get_user_group(self, user_id: str) -> Optional[Any]: ...

    def get_user_membership(self, user_id: str) -> Optional[Any]: ...

    def get_users_in_group(self, group_id: int) -> List[str]: ...

    def get_user_count_in_group(self, group_id: int) -> int: ...

    def grant_repo_access(
        self, repo_name: str, group_id: int, granted_by: str
    ) -> bool: ...

    def revoke_repo_access(self, repo_name: str, group_id: int) -> bool: ...

    def get_group_repos(self, group_id: int) -> List[str]: ...

    def get_repo_groups(self, repo_name: str) -> list: ...

    def get_repo_access(self, repo_name: str, group_id: int) -> Optional[Any]: ...

    def set_tool_access(
        self, tool_name: str, group_id: int, allowed: bool, granted_by: str
    ) -> bool: ...

    def get_group_tools(self, group_id: int) -> List[str]: ...

    def get_tool_groups(self, tool_name: str) -> list: ...

    def is_tool_allowed(self, tool_name: str, group_id: int) -> bool: ...

    def set_tool_access_all_groups(
        self, tool_name: str, allowed: bool, granted_by: str
    ) -> List[int]: ...

    def is_tool_access_enforcement_ready(self) -> bool: ...

    def auto_assign_golden_repo(self, repo_name: str) -> None: ...

    def get_all_users_with_groups(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> tuple: ...

    def user_exists(self, user_id: str) -> bool: ...

    def log_audit(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None: ...

    def get_audit_logs(
        self,
        action_type: Optional[str] = None,
        target_type: Optional[str] = None,
        admin_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        exclude_target_type: Optional[str] = None,
    ) -> tuple: ...
