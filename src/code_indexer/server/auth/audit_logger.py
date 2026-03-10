"""
Comprehensive audit logging for password change attempts.

Implements secure audit logging with IP addresses and timestamps.
Following CLAUDE.md principles: NO MOCKS - Real audit logging implementation.

Story #399: When audit_service (AuditLogService) is provided, all events are
written to the audit_logs SQLite table instead of a flat file.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log, get_log_extra
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from code_indexer.server.services.audit_log_service import AuditLogService

# Module-level logger for authentication events
logger = logging.getLogger(__name__)


class PasswordChangeAuditLogger:
    """
    Comprehensive audit logger for password change attempts.

    Security requirements:
    - Log all password change attempts (successful and failed)
    - Include IP addresses, timestamps, and usernames
    - Structured logging format for analysis
    - Separate audit log file for security monitoring (legacy path)
    - SQLite via AuditLogService (Story #399 preferred path)
    """

    def __init__(
        self,
        log_file_path: Optional[str] = None,
        audit_service: Optional["AuditLogService"] = None,
    ):
        """
        Initialize audit logger.

        Args:
            log_file_path:  Optional custom path for audit log file (legacy).
            audit_service:  AuditLogService instance.  When provided, all events
                            are written to the audit_logs SQLite table and no
                            file handler is created (Story #399).
        """
        self._audit_service = audit_service

        if audit_service is not None:
            # SQLite path: no file handler needed
            self.audit_logger = None
            self.log_file_path = None
            return

        # Legacy flat-file path
        if log_file_path:
            self.log_file_path = log_file_path
        else:
            # Default audit log location
            server_dir = Path.home() / ".cidx-server"
            server_dir.mkdir(exist_ok=True)
            self.log_file_path = str(server_dir / "password_audit.log")

        # Configure audit logger with unique name based on file path
        # This prevents multiple instances from interfering with each other
        logger_name = f"password_audit_{hash(self.log_file_path)}"
        self.audit_logger = logging.getLogger(logger_name)
        self.audit_logger.setLevel(logging.INFO)

        # Remove any existing handlers to avoid duplicates
        for handler in self.audit_logger.handlers[:]:
            self.audit_logger.removeHandler(handler)

        # Create file handler for audit log
        file_handler = logging.FileHandler(self.log_file_path)
        file_handler.setLevel(logging.INFO)

        # Create formatter for structured logging
        formatter = logging.Formatter(
            fmt="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S UTC",
        )
        file_handler.setFormatter(formatter)

        self.audit_logger.addHandler(file_handler)
        self.audit_logger.propagate = False  # Don't propagate to root logger

    def set_audit_service(self, audit_service: "AuditLogService") -> None:
        """Switch from flat-file to SQLite mode (Story #399).

        Closes and removes the file handler to avoid resource leaks.
        """
        self._audit_service = audit_service
        if self.audit_logger is not None:
            for handler in self.audit_logger.handlers[:]:
                handler.close()
                self.audit_logger.removeHandler(handler)
            self.audit_logger = None
        self.log_file_path = None

    # ------------------------------------------------------------------
    # Internal: SQLite routing helper (Story #399)
    # ------------------------------------------------------------------

    def _log_to_service(
        self,
        event_type: str,
        actor: str,
        target_id: str,
        event_dict: dict,
    ) -> None:
        """
        Write one event to AuditLogService (Story #399 AC2).

        Field mapping:
            admin_id    <- actor  (username, 'system', etc.)
            action_type <- event_type
            target_type <- "auth"  (all PasswordChangeAuditLogger events)
            target_id   <- target_id (username / repo_alias / repo_path)
            details     <- JSON-encoded event_dict

        Note: ALL PasswordChangeAuditLogger events use target_type="auth", including
        PR creation and git cleanup events. This is intentional — these events are
        distinguished by action_type, not target_type. The Groups UI uses
        exclude_target_type="auth" to filter out all PasswordChangeAuditLogger events
        from the group management view.
        """
        if self._audit_service is None:
            return
        self._audit_service.log(
            admin_id=actor,
            action_type=event_type,
            target_type="auth",
            target_id=target_id,
            details=json.dumps(event_dict),
        )

    def log_password_change_success(
        self,
        username: str,
        ip_address: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log successful password change attempt.

        Args:
            username: Username that changed password
            ip_address: IP address of the request
            user_agent: User agent string from request headers
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "password_change_success",
            "username": username,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("password_change_success", username, username, log_entry)
            return

        self.audit_logger.info(
            f"PASSWORD_CHANGE_SUCCESS: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_password_change_failure(
        self,
        username: str,
        ip_address: str,
        reason: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log failed password change attempt.

        Args:
            username: Username that attempted to change password
            ip_address: IP address of the request
            reason: Reason for failure (e.g., "Invalid old password", "Rate limited")
            user_agent: User agent string from request headers
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "password_change_failure",
            "username": username,
            "ip_address": ip_address,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("password_change_failure", username, username, log_entry)
            return

        self.audit_logger.warning(
            f"PASSWORD_CHANGE_FAILURE: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_rate_limit_triggered(
        self,
        username: str,
        ip_address: str,
        attempt_count: int,
        user_agent: Optional[str] = None,
    ) -> None:
        """
        Log rate limit being triggered.

        Args:
            username: Username that triggered rate limit
            ip_address: IP address of the request
            attempt_count: Number of failed attempts that triggered rate limit
            user_agent: User agent string from request headers
        """
        log_entry = {
            "event_type": "password_change_rate_limit",
            "username": username,
            "ip_address": ip_address,
            "attempt_count": attempt_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
        }

        if self._audit_service is not None:
            self._log_to_service("password_change_rate_limit", username, username, log_entry)
            return

        self.audit_logger.warning(
            f"PASSWORD_CHANGE_RATE_LIMIT: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_concurrent_change_conflict(
        self, username: str, ip_address: str, user_agent: Optional[str] = None
    ) -> None:
        """
        Log concurrent password change conflict.

        Args:
            username: Username that experienced concurrent change conflict
            ip_address: IP address of the request
            user_agent: User agent string from request headers
        """
        log_entry = {
            "event_type": "password_change_concurrent_conflict",
            "username": username,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
        }

        if self._audit_service is not None:
            self._log_to_service("password_change_concurrent_conflict", username, username, log_entry)
            return

        self.audit_logger.warning(
            f"PASSWORD_CHANGE_CONCURRENT_CONFLICT: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_token_refresh_success(
        self,
        username: str,
        ip_address: str,
        family_id: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log successful token refresh attempt.

        Args:
            username: Username that refreshed tokens
            ip_address: IP address of the request
            family_id: Token family ID for security tracking
            user_agent: User agent string from request headers
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "token_refresh_success",
            "username": username,
            "ip_address": ip_address,
            "family_id": family_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("token_refresh_success", username, username, log_entry)
            return

        self.audit_logger.info(
            f"TOKEN_REFRESH_SUCCESS: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_token_refresh_failure(
        self,
        username: str,
        ip_address: str,
        reason: str,
        security_incident: bool = False,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log failed token refresh attempt.

        Args:
            username: Username that attempted to refresh tokens
            ip_address: IP address of the request
            reason: Reason for failure (e.g., "Invalid refresh token", "Token expired")
            security_incident: Whether this represents a security incident
            user_agent: User agent string from request headers
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "token_refresh_failure",
            "username": username,
            "ip_address": ip_address,
            "reason": reason,
            "security_incident": security_incident,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("token_refresh_failure", username, username, log_entry)
            return

        # Log as warning for security incidents, info for normal failures
        log_level = (
            self.audit_logger.warning if security_incident else self.audit_logger.info
        )
        log_level(f"TOKEN_REFRESH_FAILURE: {json.dumps(log_entry)}")

    def log_security_incident(
        self,
        username: str,
        incident_type: str,
        ip_address: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log security incident related to token management.

        Args:
            username: Username involved in incident
            incident_type: Type of incident (e.g., "token_replay_attack", "family_revoked")
            ip_address: IP address of the request
            user_agent: User agent string from request headers
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "security_incident",
            "incident_type": incident_type,
            "username": username,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("security_incident", username, username, log_entry)
            return

        self.audit_logger.warning(
            f"SECURITY_INCIDENT: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_authentication_failure(
        self,
        username: str,
        error_type: str,
        message: str,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log authentication failure attempt.

        Args:
            username: Username that failed authentication
            error_type: Type of authentication error
            message: Detailed failure message
            additional_context: Additional context information (IP, user agent, etc.)
        """
        log_entry = {
            "event_type": "authentication_failure",
            "username": username,
            "error_type": error_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("authentication_failure", username, username, log_entry)
            return

        self.audit_logger.warning(
            f"AUTHENTICATION_FAILURE: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_registration_attempt(
        self,
        email: str,
        success: bool,
        message: str,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log registration attempt.

        Args:
            email: Email address used for registration
            success: Whether registration was successful
            message: Descriptive message about the attempt
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "registration_attempt",
            "email": email,
            "success": success,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        log_message = f"REGISTRATION_{'SUCCESS' if success else 'ATTEMPT'}: {json.dumps(log_entry)}"

        if self._audit_service is not None:
            self._log_to_service("registration_attempt", email, email, log_entry)
            return

        if success:
            self.audit_logger.info(
                log_message, extra={"correlation_id": get_correlation_id()}
            )
        else:
            self.audit_logger.warning(
                format_error_log("AUTH-AUDIT-010", log_message),
                extra=get_log_extra("AUTH-AUDIT-010"),
            )

    def log_password_reset_attempt(
        self,
        email: str,
        success: bool,
        message: str,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log password reset attempt.

        Args:
            email: Email address for password reset
            success: Whether the email corresponds to an existing account
            message: Descriptive message about the attempt
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "password_reset_attempt",
            "email": email,
            "account_exists": success,  # For password reset, success means account exists
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("password_reset_attempt", email, email, log_entry)
            return

        self.audit_logger.info(
            f"PASSWORD_RESET_ATTEMPT: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_oauth_client_registration(
        self,
        client_id,
        client_name,
        ip_address,
        user_agent=None,
        additional_context=None,
    ):
        """Log OAuth client registration."""
        log_entry = {
            "event_type": "oauth_client_registration",
            "client_id": client_id,
            "client_name": client_name,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }
        if self._audit_service is not None:
            self._log_to_service("oauth_client_registration", client_id, client_id, log_entry)
            return

        self.audit_logger.info(
            f"OAUTH_CLIENT_REGISTRATION: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_oauth_authorization(
        self, username, client_id, ip_address, user_agent=None, additional_context=None
    ):
        """Log OAuth authorization."""
        log_entry = {
            "event_type": "oauth_authorization",
            "username": username,
            "client_id": client_id,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("oauth_authorization", username, username, log_entry)
            return

        self.audit_logger.info(
            f"OAUTH_AUTHORIZATION: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_oauth_token_exchange(
        self,
        username,
        client_id,
        grant_type,
        ip_address,
        user_agent=None,
        additional_context=None,
    ):
        """Log OAuth token exchange."""
        log_entry = {
            "event_type": "oauth_token_exchange",
            "username": username,
            "client_id": client_id,
            "grant_type": grant_type,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("oauth_token_exchange", username, username, log_entry)
            return

        self.audit_logger.info(
            f"OAUTH_TOKEN_EXCHANGE: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_oauth_token_revocation(
        self, username, token_type, ip_address, user_agent=None, additional_context=None
    ):
        """Log OAuth token revocation."""
        log_entry = {
            "event_type": "oauth_token_revocation",
            "username": username,
            "token_type": token_type,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("oauth_token_revocation", username, username, log_entry)
            return

        self.audit_logger.info(
            f"OAUTH_TOKEN_REVOCATION: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_pr_creation_success(
        self,
        job_id: str,
        repo_alias: str,
        branch_name: str,
        pr_url: str,
        commit_hash: str,
        files_modified: list,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log successful PR creation for SCIP self-healing.

        Args:
            job_id: Unique identifier for SCIP fix job
            repo_alias: Repository alias
            branch_name: Name of fix branch created
            pr_url: URL of created pull request
            commit_hash: Git commit hash of the fix
            files_modified: List of file paths that were modified
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "pr_creation_success",
            "job_id": job_id,
            "repo_alias": repo_alias,
            "branch_name": branch_name,
            "pr_url": pr_url,
            "commit_hash": commit_hash,
            "files_modified": files_modified,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("pr_creation_success", "system", repo_alias, log_entry)
            return

        self.audit_logger.info(
            f"PR_CREATION_SUCCESS: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_pr_creation_failure(
        self,
        job_id: str,
        repo_alias: str,
        reason: str,
        branch_name: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log failed PR creation attempt.

        Args:
            job_id: Unique identifier for SCIP fix job
            repo_alias: Repository alias
            reason: Reason for PR creation failure
            branch_name: Name of fix branch (if created before failure)
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "pr_creation_failure",
            "job_id": job_id,
            "repo_alias": repo_alias,
            "reason": reason,
            "branch_name": branch_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("pr_creation_failure", "system", repo_alias, log_entry)
            return

        self.audit_logger.warning(
            f"PR_CREATION_FAILURE: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_pr_creation_disabled(
        self,
        job_id: str,
        repo_alias: str,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log that PR creation was skipped due to configuration.

        Args:
            job_id: Unique identifier for SCIP fix job
            repo_alias: Repository alias
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "pr_creation_disabled",
            "job_id": job_id,
            "repo_alias": repo_alias,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("pr_creation_disabled", "system", repo_alias, log_entry)
            return

        self.audit_logger.info(
            f"PR_CREATION_DISABLED: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_cleanup(
        self,
        repo_path: str,
        files_cleared: list,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log git repository cleanup operation.

        Args:
            repo_path: Path to repository that was cleaned
            files_cleared: List of files that were cleared/reset
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "git_cleanup",
            "repo_path": repo_path,
            "files_cleared": files_cleared,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("git_cleanup", "system", repo_path, log_entry)
            return

        self.audit_logger.info(
            f"GIT_CLEANUP: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_impersonation_set(
        self,
        actor_username: str,
        target_username: str,
        session_id: str,
        ip_address: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log when an admin begins impersonating another user.

        Story #722: Session Impersonation for Delegated Queries

        Args:
            actor_username: The admin user who initiated impersonation
            target_username: The user being impersonated
            session_id: MCP session identifier
            ip_address: Client IP address
            user_agent: Client user agent string (optional)
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "impersonation_set",
            "actor_username": actor_username,
            "target_username": target_username,
            "session_id": session_id,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("impersonation_set", actor_username, target_username, log_entry)
            return

        self.audit_logger.info(
            f"IMPERSONATION_SET: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_impersonation_cleared(
        self,
        actor_username: str,
        previous_target: str,
        session_id: str,
        ip_address: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log when an admin stops impersonating another user.

        Story #722: Session Impersonation for Delegated Queries

        Args:
            actor_username: The admin user who cleared impersonation
            previous_target: The user who was being impersonated
            session_id: MCP session identifier
            ip_address: Client IP address
            user_agent: Client user agent string (optional)
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "impersonation_cleared",
            "actor_username": actor_username,
            "previous_target": previous_target,
            "session_id": session_id,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("impersonation_cleared", actor_username, previous_target, log_entry)
            return

        self.audit_logger.info(
            f"IMPERSONATION_CLEARED: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def log_impersonation_denied(
        self,
        actor_username: str,
        target_username: str,
        reason: str,
        session_id: str,
        ip_address: str,
        user_agent: Optional[str] = None,
        additional_context: Optional[dict] = None,
    ) -> None:
        """
        Log when an impersonation attempt is denied (non-admin user).

        Story #722: Session Impersonation for Delegated Queries

        This is a security event logged at WARNING level.

        Args:
            actor_username: The user who attempted impersonation
            target_username: The user they attempted to impersonate
            reason: Reason for denial (e.g., "Impersonation requires ADMIN role")
            session_id: MCP session identifier
            ip_address: Client IP address
            user_agent: Client user agent string (optional)
            additional_context: Additional context information
        """
        log_entry = {
            "event_type": "impersonation_denied",
            "actor_username": actor_username,
            "target_username": target_username,
            "reason": reason,
            "session_id": session_id,
            "ip_address": ip_address,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_agent": user_agent,
            "additional_context": additional_context or {},
        }

        if self._audit_service is not None:
            self._log_to_service("impersonation_denied", actor_username, target_username, log_entry)
            return

        self.audit_logger.warning(
            f"IMPERSONATION_DENIED: {json.dumps(log_entry)}",
            extra={"correlation_id": get_correlation_id()},
        )

    def get_pr_logs(
        self,
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list:
        """
        Query PR creation audit logs with filtering and pagination.

        When an AuditLogService is injected, delegates to it.
        Otherwise falls back to parsing the flat log file.

        Args:
            repo_alias: Filter by repository alias (optional)
            limit: Maximum number of records to return
            offset: Number of records to skip

        Returns:
            List of PR creation log entries (dicts)
        """
        if self._audit_service is not None:
            return self._audit_service.get_pr_logs(
                repo_alias=repo_alias, limit=limit, offset=offset
            )

        logs = self._parse_logs_by_prefix("PR_CREATION")

        # Filter by repo_alias if provided
        if repo_alias:
            logs = [log for log in logs if log.get("repo_alias") == repo_alias]

        # Apply pagination
        return logs[offset : offset + limit]

    def get_cleanup_logs(
        self,
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list:
        """
        Query git cleanup audit logs with filtering and pagination.

        When an AuditLogService is injected, delegates to it.
        Otherwise falls back to parsing the flat log file.

        Args:
            repo_path: Filter by repository path (optional)
            limit: Maximum number of records to return
            offset: Number of records to skip

        Returns:
            List of git cleanup log entries (dicts)
        """
        if self._audit_service is not None:
            return self._audit_service.get_cleanup_logs(
                repo_path=repo_path, limit=limit, offset=offset
            )

        logs = self._parse_logs_by_prefix("GIT_CLEANUP")

        # Filter by repo_path if provided
        if repo_path:
            logs = [log for log in logs if log.get("repo_path") == repo_path]

        # Apply pagination
        return logs[offset : offset + limit]

    def _parse_logs_by_prefix(self, prefix: str) -> list:
        """
        Parse log file and extract entries matching given prefix.

        Args:
            prefix: Log prefix to filter by (e.g., "PR_CREATION", "GIT_CLEANUP")

        Returns:
            List of parsed log entries (dicts) in reverse chronological order
        """
        if self.log_file_path is None:
            return []  # SQLite mode: flat file not available

        log_entries = []
        log_file = Path(self.log_file_path)

        if not log_file.exists():
            return []

        try:
            with open(log_file, "r") as f:
                for line in f:
                    if prefix in line:
                        # Extract JSON from log line
                        # Format: "timestamp - level - PREFIX: {json}"
                        try:
                            json_start = line.index("{")
                            json_str = line[json_start:]
                            log_entry = json.loads(json_str)
                            log_entries.append(log_entry)
                        except (ValueError, json.JSONDecodeError):
                            # Skip malformed log lines
                            continue

            # Return in reverse chronological order (newest first)
            return list(reversed(log_entries))

        except Exception as e:
            # Log the error for debugging, but return empty list for graceful degradation
            logger.warning(
                format_error_log("AUTH-MIGRATE-009", f"Failed to parse log file: {e}"),
                extra=get_log_extra("AUTH-MIGRATE-009"),
            )
            return []


# Global audit logger instance
password_audit_logger = PasswordChangeAuditLogger()
