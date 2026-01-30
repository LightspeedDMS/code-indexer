"""
Committer Resolution Service (Story #641).

Resolves git committer email by testing SSH keys against remote hostnames.
Implements automatic email discovery with fallback to default email.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log, get_log_extra

import logging
from pathlib import Path
from typing import Optional, Tuple

from .ssh_key_manager import SSHKeyManager
from .key_to_remote_tester import KeyToRemoteTester
from .remote_discovery_service import RemoteDiscoveryService


def _create_default_ssh_key_manager() -> SSHKeyManager:
    """Create SSHKeyManager with SQLite backend (Story #702 migration)."""
    from .config_service import get_config_service

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    metadata_dir = server_dir / "data" / "ssh_keys"

    return SSHKeyManager(
        metadata_dir=metadata_dir,
        use_sqlite=True,
        db_path=db_path,
    )


class CommitterResolutionService:
    """
    Service for resolving git committer email based on SSH key authentication.

    Implements the algorithm:
    1. Extract hostname from golden repo URL
    2. Get all managed SSH keys
    3. Test each key against hostname until one succeeds
    4. Return working key's email, or default email if none work
    """

    def __init__(
        self,
        ssh_key_manager: Optional[SSHKeyManager] = None,
        key_to_remote_tester: Optional[KeyToRemoteTester] = None,
        remote_discovery_service: Optional[RemoteDiscoveryService] = None,
    ):
        """
        Initialize CommitterResolutionService.

        Args:
            ssh_key_manager: SSH key manager instance (creates default if None)
            key_to_remote_tester: Key tester instance (creates default if None)
            remote_discovery_service: Remote discovery instance (creates default if None)
        """
        self.logger = logging.getLogger(__name__)

        # Initialize dependencies with defaults if not provided
        self.ssh_key_manager = ssh_key_manager or _create_default_ssh_key_manager()
        self.key_to_remote_tester = key_to_remote_tester or KeyToRemoteTester()
        self.remote_discovery_service = (
            remote_discovery_service or RemoteDiscoveryService()
        )

    def resolve_committer_email(
        self,
        golden_repo_url: str,
        default_email: str,
    ) -> Tuple[str, Optional[str]]:
        """
        Resolve committer email by testing SSH keys against remote hostname.

        Implements the algorithm from Story #641:
        1. Extract hostname from golden repo's push remote URL
        2. Get all managed SSH keys with their metadata
        3. Test each key against hostname until one succeeds
        4. Return working key's email, or default if none work

        Args:
            golden_repo_url: Golden repository push URL (e.g., git@github.com:user/repo.git)
            default_email: Fallback email to use if no SSH key works

        Returns:
            Tuple of (email, key_name) where:
            - email: Resolved email (from SSH key or default)
            - key_name: Name of working SSH key, or None if using default
        """
        # Step 1: Extract hostname from golden repo's push remote URL
        hostname = self.remote_discovery_service.extract_hostname(golden_repo_url)
        if hostname is None:
            logger.warning(
                format_error_log("SVC-MIGRATE-001", "Cannot extract hostname from URL, using default email",
                                 golden_repo_url=golden_repo_url),
                extra=get_log_extra("SVC-MIGRATE-001")
            )
            return default_email, None

        self.logger.debug(
            f"Extracted hostname '{hostname}' from golden repo URL",
            extra={"correlation_id": get_correlation_id()},
        )

        # Step 2: Get all managed SSH keys with their metadata
        key_list_result = self.ssh_key_manager.list_keys()
        managed_keys = key_list_result.managed

        if not managed_keys:
            logger.warning(
                format_error_log("SVC-MIGRATE-002", "No managed SSH keys found, using default email"),
                extra=get_log_extra("SVC-MIGRATE-002")
            )
            return default_email, None

        self.logger.debug(
            f"Found {len(managed_keys)} managed SSH keys to test",
            extra={"correlation_id": get_correlation_id()},
        )

        # Step 3: Test each key against hostname until one succeeds
        for key_metadata in managed_keys:
            self.logger.debug(
                f"Testing SSH key '{key_metadata.name}' against hostname '{hostname}'",
                extra={"correlation_id": get_correlation_id()},
            )

            test_result = self.key_to_remote_tester.test_key_against_host(
                key_path=Path(key_metadata.private_path),
                hostname=hostname,
            )

            if test_result.success:
                # Key authenticated successfully
                if key_metadata.email:
                    self.logger.info(
                        f"SSH key '{key_metadata.name}' authenticated successfully, "
                        f"using email '{key_metadata.email}'",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return key_metadata.email, key_metadata.name
                else:
                    logger.warning(
                        format_error_log("SVC-MIGRATE-003", "Working key has no email configured, using default email",
                                         key_name=key_metadata.name),
                        extra=get_log_extra("SVC-MIGRATE-003")
                    )
                    return default_email, key_metadata.name

        # Step 4: No key worked, use default
        logger.warning(
            format_error_log("SVC-MIGRATE-004", "No SSH key authenticated to hostname, using default email",
                             hostname=hostname, default_email=default_email),
            extra=get_log_extra("SVC-MIGRATE-004")
        )
        return default_email, None
