"""
Regression test for Bug #1671.

AuthErrorHandler.__init__ constructs its own private PasswordChangeAuditLogger()
instance (src/code_indexer/server/auth/auth_error_handler.py). That private
instance never receives set_audit_service() -- only the SEPARATE module-level
`password_audit_logger` singleton (src/code_indexer/server/auth/audit_logger.py)
gets wired, in startup/lifespan.py's real startup sequence:

    password_audit_logger.set_audit_service(audit_service)

Because AuthErrorHandler's own instance is a structurally different object,
every authentication-failure audit write it attempts silently no-ops
(PasswordChangeAuditLogger._log_to_service returns early when
self._audit_service is None) -- real authentication failures never reach the
audit_logs table.

These tests reproduce the gap through the REAL production objects (the module
singletons imported by inline_auth.py) rather than through mocks, per
CLAUDE.md Foundation #1 (Anti-Mock).
"""

from code_indexer.server.auth.auth_error_handler import (
    AuthErrorHandler,
    AuthErrorType,
    auth_error_handler,
)
from code_indexer.server.auth.audit_logger import password_audit_logger
from code_indexer.server.services.audit_log_service import AuditLogService


class TestAuthErrorHandlerSharesWiredAuditLogger:
    """Structural assertion: AuthErrorHandler must route through the SAME
    object lifespan.py wires via password_audit_logger.set_audit_service(),
    not a private, permanently-unwired PasswordChangeAuditLogger() instance.
    """

    def test_module_singleton_audit_logger_is_the_shared_password_audit_logger(self):
        """The production `auth_error_handler` module-level singleton's
        audit_logger attribute must be IDENTICAL to the shared
        `password_audit_logger` singleton -- the only object lifespan.py's
        real startup wiring (startup/lifespan.py:1128) ever touches.
        """
        assert auth_error_handler.audit_logger is password_audit_logger


class TestRealAuthenticationFailureReachesAuditLogsTable:
    """Behavioral reproduction: after the REAL lifespan.py wiring sequence
    runs, a genuine authentication failure through the REAL
    AuthErrorHandler.create_error_response() code path (the same method
    inline_auth.py's /auth/login route calls on invalid credentials) must
    produce a real row in the audit_logs table.
    """

    def test_authentication_failure_writes_audit_logs_row(self, tmp_path):
        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)

        # Save state to restore afterward -- password_audit_logger is a
        # process-wide singleton shared with every other test in this
        # session.
        original_audit_service = password_audit_logger._audit_service
        original_logger = password_audit_logger.audit_logger
        original_log_file_path = password_audit_logger.log_file_path

        try:
            # Mirror the EXACT real wiring statement from
            # startup/lifespan.py:1128.
            password_audit_logger.set_audit_service(audit_service)

            # A freshly constructed AuthErrorHandler -- exactly like the
            # module-level `auth_error_handler` singleton inline_auth.py's
            # real /auth/login route uses on every invalid-credentials
            # response.
            handler = AuthErrorHandler(minimum_response_time_ms=1)

            handler.create_error_response(
                error_type=AuthErrorType.INVALID_CREDENTIALS,
                user_context="attacker",
                internal_message="Invalid password for attacker",
                ip_address="10.0.0.9",
                user_agent="pytest-regression-1671",
            )

            logs, total = audit_service.query()

            assert total == 1, (
                "Expected exactly one audit_logs row for the authentication "
                f"failure, found {total}. Rows: {logs}"
            )
            assert logs[0]["action_type"] == "authentication_failure"
            assert logs[0]["target_type"] == "auth"
            assert logs[0]["admin_id"] == "attacker"
            assert logs[0]["target_id"] == "attacker"
        finally:
            password_audit_logger._audit_service = original_audit_service
            password_audit_logger.audit_logger = original_logger
            password_audit_logger.log_file_path = original_log_file_path
