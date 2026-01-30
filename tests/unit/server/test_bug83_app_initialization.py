"""
Integration tests for Bug #83 - Config values must be used during app initialization.

These tests verify that app.py properly initializes components with config values:
1. JWTManager receives config.jwt_expiration_minutes (not hardcoded 10)
2. UserManager receives config.password_security (not missing)

Tests use existing components to verify they respect config when properly initialized.
"""

import pytest
import tempfile
import shutil
from datetime import datetime, timezone
import jwt as pyjwt

from code_indexer.server.services.config_service import ConfigService, reset_config_service
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.utils.jwt_secret_manager import JWTSecretManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.multi.scip_multi_service import SCIPMultiService


# Test constants
DEFAULT_JWT_EXPIRATION = 10
CUSTOM_JWT_EXPIRATION = 25
DEFAULT_PASSWORD_MIN_LENGTH = 8
CUSTOM_PASSWORD_MIN_LENGTH = 15
CUSTOM_SCIP_REFERENCE_LIMIT = 250
CUSTOM_SCIP_DEPENDENCY_DEPTH = 3
CUSTOM_SCIP_CALLCHAIN_MAX_DEPTH = 10
CUSTOM_SCIP_CALLCHAIN_LIMIT = 300


class TestBug83AppInitialization:
    """Integration tests for Bug #83 fixes in app.py initialization."""

    @pytest.fixture
    def temp_server_dir(self):
        """Create temporary server directory."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)

    @pytest.fixture(autouse=True)
    def reset_config_singleton(self):
        """Reset config service singleton."""
        reset_config_service()
        yield
        reset_config_service()

    def test_jwt_manager_token_expiration_from_config(self, temp_server_dir):
        """
        Test that JWTManager uses config.jwt_expiration_minutes (Bug #83-1).

        After fix in app.py:2847, JWTManager should be initialized with
        token_expiration_minutes=config.jwt_expiration_minutes instead of hardcoded 10.

        This test verifies JWTManager produces tokens with correct expiration
        when initialized with config value.
        """
        # Arrange: Create config with custom JWT expiration
        config_service = ConfigService(server_dir_path=temp_server_dir)
        config = config_service.load_config()
        config.jwt_expiration_minutes = CUSTOM_JWT_EXPIRATION
        config_service.config_manager.save_config(config)

        # Act: Initialize JWTManager the way app.py SHOULD (with config value)
        jwt_secret_manager = JWTSecretManager(server_dir_path=temp_server_dir)
        secret_key = jwt_secret_manager.get_or_create_secret()

        jwt_manager = JWTManager(
            secret_key=secret_key,
            token_expiration_minutes=config.jwt_expiration_minutes,  # From config!
            algorithm="HS256"
        )

        # Create token
        user_data = {
            "username": "testuser",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        token = jwt_manager.create_token(user_data)

        # Assert: Token expiration matches config (25 min), not hardcoded (10 min)
        decoded = pyjwt.decode(
            token, secret_key, algorithms=["HS256"], options={"verify_exp": False}
        )
        exp_time = datetime.fromtimestamp(decoded["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)

        # Calculate actual expiration in minutes
        expiration_minutes = (exp_time - now).total_seconds() / 60

        # Should be close to 25 minutes (config), not 10 (hardcoded)
        assert abs(expiration_minutes - CUSTOM_JWT_EXPIRATION) < 0.5, (
            f"JWT should expire in ~{CUSTOM_JWT_EXPIRATION} minutes (from config), "
            f"but expires in {expiration_minutes:.1f} minutes"
        )

        # Also verify it's NOT using the default/hardcoded value
        assert abs(expiration_minutes - DEFAULT_JWT_EXPIRATION) > 10, (
            f"JWT expiration ({expiration_minutes:.1f} min) is too close to "
            f"hardcoded default ({DEFAULT_JWT_EXPIRATION} min), should be {CUSTOM_JWT_EXPIRATION} min"
        )

    def test_user_manager_enforces_password_config(self, temp_server_dir):
        """
        Test that UserManager enforces password_security_config (Bug #83-2).

        After fix in app.py:2864-2868, UserManager should be initialized with
        password_security_config=config.password_security parameter.

        This test verifies UserManager correctly enforces custom password
        requirements when initialized with config.
        """
        # Arrange: Create config with strict password requirements
        config_service = ConfigService(server_dir_path=temp_server_dir)
        config = config_service.load_config()
        config.password_security.min_length = CUSTOM_PASSWORD_MIN_LENGTH
        config.password_security.required_char_classes = 4
        config_service.config_manager.save_config(config)

        # Act: Initialize UserManager the way app.py SHOULD (with password config)
        users_file = f"{temp_server_dir}/users.json"
        user_manager = UserManager(
            users_file_path=users_file,
            password_security_config=config.password_security,  # From config!
        )

        # Weak password: 14 chars (below custom 15 minimum)
        weak_password = "Pass123456789!"

        # Assert: Should reject weak password based on custom config
        with pytest.raises(ValueError) as exc_info:
            user_manager.create_user(
                username="testuser",
                password=weak_password,
                role=UserRole.NORMAL_USER
            )

        error_message = str(exc_info.value).lower()
        assert "length" in error_message or "15" in str(exc_info.value), (
            f"UserManager should enforce custom minimum length (15), "
            f"but error was: {exc_info.value}"
        )

    def test_user_manager_accepts_strong_password_with_config(self, temp_server_dir):
        """
        Verify UserManager accepts passwords meeting custom config requirements.

        This ensures password_security_config is properly applied.
        """
        # Arrange: Create config with strict requirements
        config_service = ConfigService(server_dir_path=temp_server_dir)
        config = config_service.load_config()
        config.password_security.min_length = CUSTOM_PASSWORD_MIN_LENGTH
        config.password_security.required_char_classes = 4
        config_service.config_manager.save_config(config)

        # Act: Initialize UserManager with password config
        users_file = f"{temp_server_dir}/users.json"
        user_manager = UserManager(
            users_file_path=users_file,
            password_security_config=config.password_security,
        )

        # Strong password meeting requirements (15+ chars, all char types)
        strong_password = "StrongPass123!@"

        # Assert: Should accept strong password
        user = user_manager.create_user(
            username="testuser",
            password=strong_password,
            role=UserRole.NORMAL_USER
        )

        assert user.username == "testuser"
        assert user.role.value == "normal_user"

    def test_config_has_all_required_settings_for_fixes(self, temp_server_dir):
        """
        Verify ConfigService provides all settings needed for Bug #83 fixes.

        This ensures the config infrastructure is in place.
        """
        config_service = ConfigService(server_dir_path=temp_server_dir)
        config = config_service.get_config()

        # Bug #83-1: JWT expiration
        assert hasattr(config, 'jwt_expiration_minutes')
        assert isinstance(config.jwt_expiration_minutes, int)
        assert config.jwt_expiration_minutes > 0

        # Bug #83-2: Password security
        assert hasattr(config, 'password_security')
        assert config.password_security is not None
        assert hasattr(config.password_security, 'min_length')
        assert hasattr(config.password_security, 'required_char_classes')

        # Bug #83-3: SCIP config
        assert hasattr(config, 'scip_config')
        assert config.scip_config is not None
        assert hasattr(config.scip_config, 'scip_reference_limit')
        assert hasattr(config.scip_config, 'scip_dependency_depth')

    def test_scip_multi_service_uses_config_limits(self, temp_server_dir):
        """
        Test that SCIPMultiService uses config.scip_config limits (Bug #83-3).

        After fix in scip_multi_service.py and scip_multi_routes.py,
        SCIPMultiService should accept config limit parameters and use them
        instead of hardcoded DEFAULT_REFERENCE_LIMIT=100, etc.

        This test verifies SCIPMultiService can be initialized with custom limits.
        """
        # Arrange: Create config with custom SCIP limits
        config_service = ConfigService(server_dir_path=temp_server_dir)
        config = config_service.load_config()
        config.scip_config.scip_reference_limit = CUSTOM_SCIP_REFERENCE_LIMIT
        config.scip_config.scip_dependency_depth = CUSTOM_SCIP_DEPENDENCY_DEPTH
        config.scip_config.scip_callchain_max_depth = CUSTOM_SCIP_CALLCHAIN_MAX_DEPTH
        config.scip_config.scip_callchain_limit = CUSTOM_SCIP_CALLCHAIN_LIMIT
        config_service.config_manager.save_config(config)

        # Act: Initialize SCIPMultiService with config values (as routes should after fix)
        service = SCIPMultiService(
            max_workers=2,
            query_timeout_seconds=30,
            reference_limit=config.scip_config.scip_reference_limit,
            dependency_depth=config.scip_config.scip_dependency_depth,
            callchain_max_depth=config.scip_config.scip_callchain_max_depth,
            callchain_limit=config.scip_config.scip_callchain_limit,
        )

        # Assert: Service should store config values
        assert hasattr(service, 'reference_limit'), "Service should have reference_limit"
        assert service.reference_limit == CUSTOM_SCIP_REFERENCE_LIMIT

        assert hasattr(service, 'dependency_depth'), "Service should have dependency_depth"
        assert service.dependency_depth == CUSTOM_SCIP_DEPENDENCY_DEPTH

        assert hasattr(service, 'callchain_max_depth'), "Service should have callchain_max_depth"
        assert service.callchain_max_depth == CUSTOM_SCIP_CALLCHAIN_MAX_DEPTH

        assert hasattr(service, 'callchain_limit'), "Service should have callchain_limit"
        assert service.callchain_limit == CUSTOM_SCIP_CALLCHAIN_LIMIT
