"""
Tests for Story #558: TOTP MFA Core Engine.

Verifies secret generation, TOTP verification with replay prevention,
recovery codes, QR code generation, and MFA lifecycle.
"""

import os
import tempfile

import pyotp
import pytest

from code_indexer.server.auth.totp_service import TOTPService


@pytest.fixture
def totp_service():
    """Create TOTPService with temp database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    service = TOTPService(db_path=db_path, mfa_encryption_key=key)
    yield service
    if os.path.exists(db_path):
        os.unlink(db_path)


class TestSecretGeneration:
    """Secret generation and storage."""

    def test_generate_secret_returns_base32(self, totp_service):
        """Secret must be a valid base32 string."""
        secret = totp_service.generate_secret("alice")
        assert len(secret) == 32
        totp = pyotp.TOTP(secret)
        assert totp.now()

    def test_generate_secret_stores_encrypted(self, totp_service):
        """Secret must be stored encrypted, not plaintext."""
        secret = totp_service.generate_secret("alice")
        conn = totp_service._get_conn()
        row = conn.execute(
            "SELECT encrypted_secret FROM user_mfa WHERE user_id = 'alice'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["encrypted_secret"] != secret

    def test_generate_secret_mfa_not_enabled(self, totp_service):
        """mfa_enabled must remain False after secret generation."""
        totp_service.generate_secret("alice")
        assert totp_service.is_mfa_enabled("alice") is False

    def test_generate_secret_overwrites_existing(self, totp_service):
        """Generating a new secret replaces the old one."""
        secret1 = totp_service.generate_secret("alice")
        secret2 = totp_service.generate_secret("alice")
        assert secret1 != secret2


class TestProvisioningURI:
    """Provisioning URI generation."""

    def test_returns_otpauth_uri(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        assert uri is not None
        assert uri.startswith("otpauth://totp/")

    def test_uri_contains_issuer(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        assert "issuer=CIDX" in uri

    def test_uri_contains_username(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        assert "alice" in uri

    def test_returns_none_for_unknown_user(self, totp_service):
        assert totp_service.get_provisioning_uri("unknown") is None


class TestQRCode:
    """QR code generation."""

    def test_generates_png_bytes(self, totp_service):
        totp_service.generate_secret("alice")
        uri = totp_service.get_provisioning_uri("alice")
        qr_bytes = totp_service.generate_qr_code(uri)
        assert isinstance(qr_bytes, bytes)
        assert len(qr_bytes) > 100
        assert qr_bytes[:4] == b"\x89PNG"


class TestCodeVerification:
    """TOTP code verification."""

    def test_valid_code_accepted(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service.verify_code("alice", code) is True

    def test_invalid_code_rejected(self, totp_service):
        totp_service.generate_secret("alice")
        assert totp_service.verify_code("alice", "000000") is False

    def test_replay_rejected(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service.verify_code("alice", code) is True
        assert totp_service.verify_code("alice", code) is False

    def test_unknown_user_rejected(self, totp_service):
        assert totp_service.verify_code("unknown", "123456") is False


class TestRecoveryCodes:
    """Recovery code generation and verification."""

    def test_generates_correct_count(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        assert len(codes) == 10

    def test_code_format(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        for code in codes:
            parts = code.split("-")
            assert len(parts) == 4
            assert all(len(p) == 4 for p in parts)

    def test_valid_code_accepted(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        assert totp_service.verify_recovery_code("alice", codes[0]) is True

    def test_used_code_rejected(self, totp_service):
        totp_service.generate_secret("alice")
        codes = totp_service.generate_recovery_codes("alice")
        assert totp_service.verify_recovery_code("alice", codes[0]) is True
        assert totp_service.verify_recovery_code("alice", codes[0]) is False

    def test_invalid_code_rejected(self, totp_service):
        totp_service.generate_secret("alice")
        totp_service.generate_recovery_codes("alice")
        assert (
            totp_service.verify_recovery_code("alice", "XXXX-XXXX-XXXX-XXXX") is False
        )

    def test_regenerate_invalidates_old_codes(self, totp_service):
        totp_service.generate_secret("alice")
        old_codes = totp_service.generate_recovery_codes("alice")
        new_codes = totp_service.regenerate_recovery_codes("alice")
        assert totp_service.verify_recovery_code("alice", old_codes[0]) is False
        assert totp_service.verify_recovery_code("alice", new_codes[0]) is True


class TestMFALifecycle:
    """MFA activation and deactivation."""

    def test_activate_with_valid_code(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert totp_service.activate_mfa("alice", code) is True
        assert totp_service.is_mfa_enabled("alice") is True

    def test_activate_with_invalid_code_fails(self, totp_service):
        totp_service.generate_secret("alice")
        assert totp_service.activate_mfa("alice", "000000") is False
        assert totp_service.is_mfa_enabled("alice") is False

    def test_disable_removes_all_data(self, totp_service):
        secret = totp_service.generate_secret("alice")
        totp_service.generate_recovery_codes("alice")
        totp = pyotp.TOTP(secret)
        totp_service.activate_mfa("alice", totp.now())
        totp_service.disable_mfa("alice")
        assert totp_service.is_mfa_enabled("alice") is False
        assert totp_service.get_provisioning_uri("alice") is None

    def test_is_mfa_enabled_false_by_default(self, totp_service):
        assert totp_service.is_mfa_enabled("alice") is False


class TestMfaConfig:
    """MfaConfig dataclass in config_manager."""

    def test_mfa_config_exists(self):
        from code_indexer.server.utils.config_manager import MfaConfig

        config = MfaConfig()
        assert config.mfa_enabled is False
        assert config.totp_window_tolerance == 1
        assert config.recovery_code_count == 10

    def test_server_config_has_mfa_config(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config, "mfa_config")
        assert config.mfa_config is not None
        assert config.mfa_config.mfa_enabled is False
