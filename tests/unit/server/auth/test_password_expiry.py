"""
Tests for password expiry feature (Story #565).

Non-SSO accounts must change their password every N days (configurable).
SSO accounts are exempt. Feature is disabled by default.
"""

from datetime import datetime, timezone, timedelta

import pytest

from code_indexer.server.utils.config_manager import PasswordExpiryConfig
from code_indexer.server.auth.user_manager import UserManager, UserRole


class TestPasswordExpiryConfig:
    """Test PasswordExpiryConfig dataclass defaults and values."""

    def test_default_disabled(self):
        """Password expiry should be disabled by default."""
        config = PasswordExpiryConfig()
        assert config.enabled is False

    def test_default_max_age_days(self):
        """Default max age should be 90 days."""
        config = PasswordExpiryConfig()
        assert config.max_age_days == 90

    def test_custom_values(self):
        """Custom values should be accepted."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=30)
        assert config.enabled is True
        assert config.max_age_days == 30


class TestPasswordChangedAtTracking:
    """Test that password_changed_at is tracked in the database."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Create a temporary database with schema initialized."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        path = str(tmp_path / "test.db")
        schema = DatabaseSchema(path)
        schema.initialize_database()
        return path

    @pytest.fixture
    def user_manager(self, db_path):
        """Create a UserManager with SQLite backend."""
        mgr = UserManager(use_sqlite=True, db_path=db_path)
        return mgr

    def test_create_user_sets_password_changed_at(self, user_manager):
        """Creating a user should set password_changed_at to now."""
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)
        # Retrieve via backend to check password_changed_at
        user_data = user_manager._sqlite_backend.get_user("testuser")
        assert user_data is not None
        assert "password_changed_at" in user_data
        assert user_data["password_changed_at"] is not None

    def test_change_password_updates_password_changed_at(self, user_manager):
        """Changing password should update password_changed_at."""
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)
        before = user_manager._sqlite_backend.get_user("testuser")
        before_ts = before["password_changed_at"]

        # Change password
        user_manager.change_password("testuser", "NewStr0ng!Pass456Abc")
        after = user_manager._sqlite_backend.get_user("testuser")
        after_ts = after["password_changed_at"]

        assert after_ts is not None
        assert after_ts >= before_ts


class TestPasswordExpiryEdgeCases:
    """Test edge cases for password expiry boundary conditions."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Create a temporary database with schema initialized."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        path = str(tmp_path / "test.db")
        schema = DatabaseSchema(path)
        schema.initialize_database()
        return path

    @pytest.fixture
    def user_manager(self, db_path):
        """Create a UserManager with SQLite backend."""
        mgr = UserManager(use_sqlite=True, db_path=db_path)
        return mgr

    def test_exactly_at_max_age_not_expired(self, user_manager):
        """Password changed exactly max_age_days ago should not be expired."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=90)
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)

        # Set to exactly 90 days ago (boundary -- should NOT be expired)
        boundary_time = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        user_manager._sqlite_backend.set_password_changed_at("testuser", boundary_time)

        assert user_manager.is_password_expired("testuser", config) is False

    def test_one_day_past_max_age_expired(self, user_manager):
        """Password changed max_age_days + 1 ago should be expired."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=90)
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)

        past_time = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        user_manager._sqlite_backend.set_password_changed_at("testuser", past_time)

        assert user_manager.is_password_expired("testuser", config) is True


class TestIsPasswordExpired:
    """Test the is_password_expired method on UserManager."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Create a temporary database with schema initialized."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        path = str(tmp_path / "test.db")
        schema = DatabaseSchema(path)
        schema.initialize_database()
        return path

    @pytest.fixture
    def user_manager(self, db_path):
        """Create a UserManager with SQLite backend."""
        mgr = UserManager(use_sqlite=True, db_path=db_path)
        return mgr

    def test_not_expired_when_feature_disabled(self, user_manager):
        """When password expiry is disabled, should never report expired."""
        config = PasswordExpiryConfig(enabled=False, max_age_days=90)
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)
        assert user_manager.is_password_expired("testuser", config) is False

    def test_not_expired_within_max_age(self, user_manager):
        """Password changed recently should not be expired."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=90)
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)
        # Just created, so password_changed_at is now -- not expired
        assert user_manager.is_password_expired("testuser", config) is False

    def test_expired_beyond_max_age(self, user_manager):
        """Password changed more than max_age_days ago should be expired."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=90)
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)

        # Manually set password_changed_at to 91 days ago
        old_time = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        user_manager._sqlite_backend.set_password_changed_at("testuser", old_time)

        assert user_manager.is_password_expired("testuser", config) is True

    def test_sso_user_exempt(self, user_manager):
        """SSO users should never have expired passwords."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=1)
        user_manager.create_user("ssouser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)

        # Make them an SSO user
        user_manager.set_oidc_identity(
            "ssouser",
            {
                "subject": "sso-123",
                "email": "sso@example.com",
                "linked_at": datetime.now(timezone.utc).isoformat(),
                "last_login": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Set password_changed_at to long ago
        old_time = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        user_manager._sqlite_backend.set_password_changed_at("ssouser", old_time)

        assert user_manager.is_password_expired("ssouser", config) is False

    def test_nonexistent_user_not_expired(self, user_manager):
        """Non-existent user should return False (not expired)."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=90)
        assert user_manager.is_password_expired("nouser", config) is False

    def test_no_password_changed_at_treated_as_expired(self, user_manager):
        """If password_changed_at is None (legacy data), treat as expired when enabled."""
        config = PasswordExpiryConfig(enabled=True, max_age_days=90)
        user_manager.create_user("testuser", "StrongP@ss123!Xyz", UserRole.NORMAL_USER)

        # Manually clear password_changed_at to simulate legacy data
        user_manager._sqlite_backend.set_password_changed_at("testuser", None)

        assert user_manager.is_password_expired("testuser", config) is True
