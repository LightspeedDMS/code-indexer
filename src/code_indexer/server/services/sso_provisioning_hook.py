"""
SSO Auto-Provisioning Hook for CIDX Server.

Story #708: SSO Auto-Provisioning with Default Group Assignment

Provides automatic provisioning of new SSO users to the default "users" group.
This ensures new users can immediately query cidx-meta without manual intervention.

Key behaviors:
- New SSO users are auto-assigned to "users" group
- Existing users' group membership is NOT changed on re-login
- assigned_by is set to "system:sso-provisioning" for auto-provisioned users
- Errors are logged but do not block authentication

Group Mapping Support:
- External groups from SSO provider can be mapped to CIDX groups via configuration
- First matched group is used for assignment
- Falls back to "users" group if no mappings match
"""

import logging
from typing import TYPE_CHECKING, Optional, List, Dict

from .constants import DEFAULT_GROUP_USERS

if TYPE_CHECKING:
    from .group_access_manager import GroupAccessManager
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Constant for assigned_by value used in SSO auto-provisioning
SSO_PROVISIONING_ASSIGNED_BY = "system:sso-provisioning"


class SystemConfigurationError(Exception):
    """Raised when system invariants are violated.

    This exception indicates a PRECONDITION VIOLATION, not a runtime error.
    Examples: missing default groups, database not properly initialized.

    Per Anti-Fallback principle, we fail loudly rather than silently
    degrading service quality.
    """

    pass


class SSOProvisioningHook:
    """
    Hook for auto-provisioning SSO users to the default group.

    This hook is called during SSO authentication to ensure new users
    have a group membership. Existing users are not modified.
    """

    def __init__(
        self,
        group_manager: "GroupAccessManager",
        group_mappings: Optional[List[Dict[str, str]]] = None,
    ):
        """
        Initialize the SSO provisioning hook.

        Args:
            group_manager: The GroupAccessManager instance for group operations
            group_mappings: Optional list of group mapping objects with:
                - external_group_id: The external group identifier (GUID or name)
                - external_group_name: Optional display name for documentation
                - cidx_group: Target CIDX group name
        """
        self.group_manager = group_manager
        self.group_mappings = group_mappings or []

    def _determine_target_group(self, external_groups: Optional[List[str]]) -> str:
        """
        Determine target CIDX group based on external groups and configured mappings.

        Args:
            external_groups: List of external group identifiers from OIDC provider

        Returns:
            CIDX group name to assign user to (first match or DEFAULT_GROUP_USERS)
        """
        if not external_groups or not self.group_mappings:
            return DEFAULT_GROUP_USERS

        # Find first matching group mapping (new list format)
        for external_group_id in external_groups:
            for mapping in self.group_mappings:
                if mapping.get("external_group_id") == external_group_id:
                    cidx_group = mapping.get("cidx_group")
                    external_name = mapping.get(
                        "external_group_name", external_group_id
                    )
                    logger.debug(
                        f"Matched external group '{external_name}' ({external_group_id}) to CIDX group '{cidx_group}'"
                    )
                    return cidx_group

        # No matches found, use default
        logger.debug(
            f"No group mappings matched for external groups {external_groups}, using default '{DEFAULT_GROUP_USERS}'"
        )
        return DEFAULT_GROUP_USERS

    def ensure_group_membership(
        self, user_id: str, external_groups: Optional[List[str]] = None
    ) -> bool:
        """
        Ensure the user has a group membership, defaulting to users group.

        AC1: New users are assigned to "users" group with "system:sso-provisioning"
        AC3: Existing users' membership is NOT changed
        AC6: Errors are logged but do not block authentication

        Group Mapping:
        - If external_groups provided and group_mappings configured, assigns to first matched group
        - Falls back to "users" group if no match or no mappings

        Args:
            user_id: The user's unique identifier (from SSO token sub claim)
            external_groups: Optional list of external group names from OIDC provider

        Returns:
            True if user has membership (existing or newly created),
            False if provisioning failed
        """
        try:
            # Check if user already has a group membership
            existing_group = self.group_manager.get_user_group(user_id)

            if existing_group is not None:
                # AC3: User already has membership - do not modify
                logger.debug(
                    f"SSO user '{user_id}' already has group membership: {existing_group.name}"
                )
                return True

            # New user - determine target group based on mappings
            target_group_name = self._determine_target_group(external_groups)
            target_group = self.group_manager.get_group_by_name(target_group_name)

            # If target group doesn't exist, fallback to default users group
            if target_group is None:
                if target_group_name != DEFAULT_GROUP_USERS:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-174",
                            f"Configured group '{target_group_name}' not found, falling back to '{DEFAULT_GROUP_USERS}' group for user '{user_id}'",
                        )
                    )
                    target_group_name = DEFAULT_GROUP_USERS
                    target_group = self.group_manager.get_group_by_name(
                        target_group_name
                    )

                # If fallback group also doesn't exist, database is not initialized
                if target_group is None:
                    # PRECONDITION VIOLATION: Database not properly initialized
                    # Per Anti-Fallback principle, fail loudly instead of silent degradation
                    raise SystemConfigurationError(
                        f"SSO provisioning failed: '{DEFAULT_GROUP_USERS}' group not found. "
                        "Database may not be properly initialized. "
                        "Run database initialization to create default groups."
                    )

            # AC1: Assign new user to determined group
            self.group_manager.assign_user_to_group(
                user_id=user_id,
                group_id=target_group.id,
                assigned_by=SSO_PROVISIONING_ASSIGNED_BY,
            )

            # AC7 (Story #710): Log to audit trail for administrative actions
            mapping_info = (
                f" (mapped from external groups: {external_groups})"
                if external_groups and target_group_name != DEFAULT_GROUP_USERS
                else ""
            )
            self.group_manager.log_audit(
                admin_id=SSO_PROVISIONING_ASSIGNED_BY,
                action_type="user_assign",
                target_type="user",
                target_id=user_id,
                details=f"SSO auto-provisioned to '{target_group_name}' group{mapping_info}",
            )

            logger.info(
                f"SSO auto-provisioned user '{user_id}' to '{target_group_name}' group{mapping_info}"
            )
            return True

        except SystemConfigurationError:
            # PRECONDITION VIOLATION - re-raise to fail loudly
            # This is NOT a runtime error, it's a database misconfiguration
            raise
        except Exception as e:
            # AC6: Log RUNTIME errors but do not block authentication
            logger.error(
                format_error_log(
                    "MCP-GENERAL-175",
                    f"SSO provisioning failed for user '{user_id}': {e}. "
                    f"User will have fallback cidx-meta-only access.",
                )
            )
            return False


def ensure_user_group_membership(
    user_id: str,
    group_manager: "GroupAccessManager",
    external_groups: Optional[List[str]] = None,
    group_mappings: Optional[List[Dict[str, str]]] = None,
) -> bool:
    """
    Standalone function wrapper for SSO provisioning.

    Convenience function that can be called directly without
    instantiating SSOProvisioningHook.

    Args:
        user_id: The user's unique identifier (from SSO token sub claim)
        group_manager: The GroupAccessManager instance
        external_groups: Optional list of external group identifiers from OIDC provider
        group_mappings: Optional list of group mapping objects

    Returns:
        True if user has membership (existing or newly created),
        False if provisioning failed
    """
    hook = SSOProvisioningHook(group_manager, group_mappings)
    return hook.ensure_group_membership(user_id, external_groups)
