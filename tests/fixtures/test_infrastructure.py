"""
Real Component Test Infrastructure for MESSI Rule #1 Compliance.

This infrastructure enables testing real security components without mocks,
following the elite-software-architect's Option A recommendation.

ZERO MOCKS - REAL COMPONENTS ONLY
- Real FastAPI app with real database
- Real UserManager with real password hashing
- Real JWT through real login flows
- Real rate limiting with proper state management
- Real audit logging with real file operations
"""

import tempfile
import shutil
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional, List, cast
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import logging

from code_indexer.server.app import create_app
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.rate_limiter import PasswordChangeRateLimiter
from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger
from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager
from code_indexer.server.auth import dependencies


class RealComponentTestInfrastructure:
    """
    Real component test infrastructure for security testing.

    Provides real FastAPI app, real database operations, and real security components.
    Each test gets isolated environment with proper cleanup.
    """

    def __init__(self):
        """Initialize test infrastructure with isolated environment."""
        self.temp_dir: Optional[Path] = None
        self.app: Optional[FastAPI] = None
        self.client: Optional[TestClient] = None
        self.user_manager: Optional[UserManager] = None
        self.jwt_manager: Optional[JWTManager] = None
        self.rate_limiter: Optional[PasswordChangeRateLimiter] = None
        self.audit_logger: Optional[PasswordChangeAuditLogger] = None
        self.refresh_token_manager: Optional[RefreshTokenManager] = None
        self._original_cidx_server_dir: Optional[str] = None
        self._original_dependencies: Optional[Dict[str, Any]] = None
        self._original_real_audit_service: Optional[Any] = None
        self._original_real_audit_log_file_path: Optional[str] = None

    def setup(self) -> None:
        """
        Set up isolated test environment with real components.

        Creates:
        - Temporary directory for test data
        - Real FastAPI app with real database
        - Real security components with proper isolation
        """
        # Create isolated temporary directory
        self.temp_dir = Path(tempfile.mkdtemp(prefix="cidx_test_"))

        # Override CIDX_SERVER_DATA_DIR to use our temp directory
        self._original_cidx_server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
        os.environ["CIDX_SERVER_DATA_DIR"] = str(self.temp_dir)

        # Create necessary subdirectories
        (self.temp_dir / "users").mkdir(exist_ok=True)
        (self.temp_dir / "audit").mkdir(exist_ok=True)
        (self.temp_dir / "tokens").mkdir(exist_ok=True)

        # Create real FastAPI app (this will initialize all security components)
        self.app = self.create_test_app()
        self.client = TestClient(self.app)

    def create_test_app(self) -> FastAPI:
        """
        Create real FastAPI app with real security components.

        Returns:
            Real FastAPI application with all security middleware enabled

        CRITICAL: NO MOCKS - Uses real components throughout

        NOTE (#1698): user_manager and password_audit_logger are captured
        as closures over their create_app()-time instances by the routes
        extracted into inline_auth.py/inline_admin_users.py (Story #409)
        -- reassigning the `dependencies.user_manager` / `audit_logger.
        password_audit_logger` module attributes AFTER create_app() has
        ZERO effect on already-registered route closures. The previous
        implementation built a completely separate JSON-backed UserManager
        and a brand new PasswordChangeAuditLogger and reassigned module
        attributes to them -- invisible to every real request, so every
        login through this infrastructure 401'd regardless of test order
        (confirmed live via direct experimentation). Fixed by deriving
        user_manager from the app's own dependencies post-create_app(),
        and by mutating the REAL password_audit_logger singleton's
        handler in place (mirrors the established #1681 fix pattern)
        instead of building a disconnected replacement.
        """
        assert self.temp_dir is not None, (
            "temp_dir must be set before creating test app"
        )

        # Isolate the REAL password_audit_logger singleton's handler to
        # this test's own tempdir -- restored via cleanup()'s
        # _restore_password_audit_logger().
        from code_indexer.server.auth import audit_logger as audit_module

        real_audit_logger = audit_module.password_audit_logger
        self._original_real_audit_service = real_audit_logger._audit_service
        self._original_real_audit_log_file_path = real_audit_logger.log_file_path

        # The create_app() function will create its own managers using our
        # test environment. Since we set CIDX_SERVER_DATA_DIR, it will use
        # our temp directory. Route closures (login, change-password, etc.)
        # are fixed to whatever these managers are AT THIS POINT.
        app = cast(FastAPI, create_app())

        # Derive user_manager from the app's own dependencies so test
        # users are created in the SAME instance the route closures
        # captured (#1698).
        self.user_manager = dependencies.user_manager

        # Isolate the REAL audit logger singleton's handler in place --
        # creating a brand new PasswordChangeAuditLogger and reassigning
        # module attributes (the old approach) is invisible to
        # inline_admin_users.py's name-bound `password_audit_logger`
        # import.
        audit_log_path = self.temp_dir / "audit" / "password_changes.log"
        isolated_audit_logger = PasswordChangeAuditLogger(str(audit_log_path))
        real_audit_logger._audit_service = None
        real_audit_logger.audit_logger = isolated_audit_logger.audit_logger
        real_audit_logger.log_file_path = isolated_audit_logger.log_file_path
        self.audit_logger = real_audit_logger

        # Store the managers for our tests to use
        self.jwt_manager = dependencies.jwt_manager

        return app

    def create_test_user(
        self,
        username: str = "testuser",
        password: str = "TestPassword123!",
        role: UserRole = UserRole.NORMAL_USER,
    ) -> Dict[str, Any]:
        """
        Create real user through real UserManager.

        Args:
            username: Username for the test user
            password: Password for the test user (must meet complexity requirements)
            role: Role for the test user

        Returns:
            Dictionary with user information (excludes password)

        CRITICAL: NO MOCKS - Uses real password hashing and validation
        """
        if not self.user_manager:
            raise RuntimeError(
                "Test infrastructure not initialized. Call setup() first."
            )

        # Create user through real UserManager (which does real password validation)
        user = self.user_manager.create_user(username, password, role)

        return {
            "username": user.username,
            "role": user.role.value,
            "created_at": user.created_at.isoformat(),
            "password": password,  # Only for testing - never in production
        }

    def get_auth_token(self, username: str, password: str) -> Dict[str, Any]:
        """
        Get real JWT token through real login flow.

        Args:
            username: Username for authentication
            password: Password for authentication

        Returns:
            Dictionary with access_token and token_type

        CRITICAL: NO MOCKS - Uses real authentication flow with real JWT generation
        """
        if not self.client:
            raise RuntimeError(
                "Test infrastructure not initialized. Call setup() first."
            )

        # Perform real login through real API endpoint
        response = self.client.post(
            "/auth/login", json={"username": username, "password": password}
        )

        if response.status_code != 200:
            raise ValueError(f"Login failed: {response.status_code} - {response.text}")

        token_data: Dict[str, Any] = response.json()

        # Verify token structure
        if "access_token" not in token_data:
            raise ValueError(f"Invalid token response: {token_data}")

        return token_data

    def authenticate_request(self, token: str) -> Dict[str, str]:
        """
        Create authentication headers for real API requests.

        Args:
            token: JWT access token

        Returns:
            Dictionary with Authorization header
        """
        return {"Authorization": f"Bearer {token}"}

    def verify_rate_limiting(
        self,
        endpoint: str,
        max_attempts: int = 5,
        method: str = "POST",
        **request_kwargs,
    ) -> Dict[str, Any]:
        """
        Verify real rate limiting behavior.

        Args:
            endpoint: API endpoint to test
            max_attempts: Expected maximum attempts before rate limiting
            method: HTTP method to use (POST, PUT, GET, etc.)
            **request_kwargs: Additional arguments for request

        Returns:
            Dictionary with rate limiting test results

        CRITICAL: NO MOCKS - Tests real rate limiter with real state
        """
        if not self.client:
            raise RuntimeError(
                "Test infrastructure not initialized. Call setup() first."
            )

        results: Dict[str, Any] = {
            "successful_attempts": 0,
            "rate_limited_at": None,
            "responses": [],
        }

        # Test real rate limiting by making actual requests
        for attempt in range(max_attempts + 2):  # Try beyond the limit
            # Use the appropriate HTTP method
            if method.upper() == "POST":
                response = self.client.post(endpoint, **request_kwargs)
            elif method.upper() == "PUT":
                response = self.client.put(endpoint, **request_kwargs)
            elif method.upper() == "GET":
                response = self.client.get(endpoint, **request_kwargs)
            elif method.upper() == "DELETE":
                response = self.client.delete(endpoint, **request_kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            results["responses"].append(
                {
                    "attempt": attempt + 1,
                    "status_code": response.status_code,
                    "response_data": response.json() if response.content else None,
                }
            )

            if response.status_code == 429:  # Rate limited
                results["rate_limited_at"] = attempt + 1
                break
            elif response.status_code in [
                200,
                400,
                401,
            ]:  # Valid response (success or expected failure)
                results["successful_attempts"] = attempt + 1
            else:
                # Unexpected response - stop testing
                break

        return results

    def get_audit_logs(self, action_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieve real audit logs from real audit logger.

        Args:
            action_type: Optional filter for specific action types

        Returns:
            List of audit log entries

        CRITICAL: NO MOCKS - Reads real audit logs from real files
        """
        if not self.audit_logger:
            raise RuntimeError(
                "Test infrastructure not initialized. Call setup() first."
            )

        # Read real audit logs from real files
        assert self.temp_dir is not None, (
            "temp_dir must be set before reading audit logs"
        )
        audit_dir = self.temp_dir / "audit"
        if not audit_dir.exists():
            return []

        logs = []
        for log_file in audit_dir.glob("*.log"):
            try:
                with open(log_file, "r") as f:
                    for line in f:
                        if line.strip():
                            try:
                                log_entry = json.loads(line.strip())
                                if (
                                    action_type is None
                                    or log_entry.get("action") == action_type
                                ):
                                    logs.append(log_entry)
                            except json.JSONDecodeError:
                                # Skip invalid JSON lines
                                continue
            except Exception:
                # Skip files that can't be read
                continue

        # Sort by timestamp
        logs.sort(key=lambda x: x.get("timestamp", ""))
        return logs

    def cleanup(self) -> None:
        """
        Clean up test environment and restore original state.

        CRITICAL: Proper cleanup to prevent test interference
        """
        # Close test client
        if self.client:
            self.client.close()

        # Restore the REAL password_audit_logger singleton to a genuinely
        # working state (#1698) -- reassigning stale object references
        # does not reopen handlers set_audit_service() closed in place;
        # mirrors the established #1681 fix pattern.
        self._restore_password_audit_logger()

        # Restore original environment
        if self._original_cidx_server_dir is not None:
            os.environ["CIDX_SERVER_DATA_DIR"] = self._original_cidx_server_dir
        else:
            os.environ.pop("CIDX_SERVER_DATA_DIR", None)

        # Remove temporary directory
        if self.temp_dir and self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir)
            except Exception as e:
                # Log warning but don't fail test
                logging.warning(
                    f"Failed to cleanup temp directory {self.temp_dir}: {e}"
                )

        # Reset all references
        self.temp_dir = None
        self.app = None
        self.client = None
        self.user_manager = None
        self.jwt_manager = None
        self.rate_limiter = None
        self.audit_logger = None
        self.refresh_token_manager = None
        self._original_cidx_server_dir = None

    def _restore_password_audit_logger(self) -> None:
        """Restore the REAL password_audit_logger singleton to a genuinely
        WORKING state after create_test_app() isolated its handler to this
        test's own tempdir.

        set_audit_service() (audit_logger.py) closes and removes handlers
        on the shared, name-cached Logger object IN PLACE -- restoring
        only the `.audit_logger`/`.log_file_path` references afterward
        would re-point at that SAME now-handler-less Logger, permanently
        breaking every later real-component test in the same pytest
        session that logs through this singleton (see #1681 / #1698).
        Reopen a real handler instead.
        """
        from code_indexer.server.auth.audit_logger import (
            password_audit_logger as real_audit_logger,
            PasswordChangeAuditLogger,
        )

        for handler in real_audit_logger.audit_logger.handlers[:]:
            handler.close()
            real_audit_logger.audit_logger.removeHandler(handler)
        if self._original_real_audit_service is not None:
            real_audit_logger.set_audit_service(self._original_real_audit_service)
        elif self._original_real_audit_log_file_path:
            restored = PasswordChangeAuditLogger(
                log_file_path=self._original_real_audit_log_file_path
            )
            real_audit_logger._audit_service = None
            real_audit_logger.audit_logger = restored.audit_logger
            real_audit_logger.log_file_path = restored.log_file_path
        self._original_real_audit_service = None
        self._original_real_audit_log_file_path = None


@pytest.fixture
def test_infrastructure():
    """
    Pytest fixture for real component test infrastructure.

    Provides complete setup and cleanup for each test.

    Usage:
        def test_real_authentication(test_infrastructure):
            # Create real user
            user = test_infrastructure.create_test_user()

            # Get real JWT token
            token_data = test_infrastructure.get_auth_token(
                user["username"], user["password"]
            )

            # Make authenticated request with real token
            headers = test_infrastructure.authenticate_request(token_data["access_token"])
            response = test_infrastructure.client.get("/protected", headers=headers)

            # Verify real security behavior
            assert response.status_code == 200
    """
    infrastructure = RealComponentTestInfrastructure()
    infrastructure.setup()

    try:
        yield infrastructure
    finally:
        infrastructure.cleanup()
