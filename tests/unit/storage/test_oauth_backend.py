"""
Tests for OAuthBackend Protocol and OAuthSqliteBackend implementation (Story #514).

TDD approach: tests written BEFORE implementation.

Covers:
- AC1: OAuthBackend Protocol is runtime-checkable
- AC2: OAuthSqliteBackend satisfies Protocol and implements all methods correctly
- AC3: BackendRegistry has oauth field; StorageFactory creates it in SQLite mode
"""

import base64
import hashlib
import secrets
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# AC1: Protocol
# ---------------------------------------------------------------------------


class TestOAuthBackendProtocol:
    """Tests for the OAuthBackend Protocol definition (AC1)."""

    def test_oauth_backend_protocol_is_runtime_checkable(self):
        """OAuthBackend must be decorated with @runtime_checkable."""
        from code_indexer.server.storage.protocols import OAuthBackend

        assert hasattr(OAuthBackend, "__protocol_attrs__") or hasattr(
            OAuthBackend, "_is_protocol"
        ), "OAuthBackend must be a Protocol"

        class NotABackend:
            pass

        try:
            isinstance(NotABackend(), OAuthBackend)
        except TypeError:
            pytest.fail(
                "isinstance() raised TypeError — OAuthBackend is not @runtime_checkable"
            )

    def test_oauth_backend_protocol_has_required_methods(self):
        """OAuthBackend Protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import OAuthBackend

        protocol_methods = dir(OAuthBackend)

        required = [
            "register_client",
            "get_client",
            "generate_authorization_code",
            "exchange_code_for_token",
            "validate_token",
            "extend_token_on_activity",
            "refresh_access_token",
            "revoke_token",
            "handle_client_credentials_grant",
            "link_oidc_identity",
            "get_oidc_identity",
            "close",
        ]
        for method in required:
            assert method in protocol_methods, f"OAuthBackend must have {method}()"


# ---------------------------------------------------------------------------
# AC2: SQLite Backend Implementation
# ---------------------------------------------------------------------------


class TestOAuthSqliteBackend:
    """Tests for OAuthSqliteBackend implementation (AC2)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Provide a temp directory path for the test database."""
        return str(tmp_path / "test_oauth.db")

    @pytest.fixture
    def backend(self, db_path):
        """Create a fresh OAuthSqliteBackend for each test."""
        from code_indexer.server.storage.sqlite_backends import OAuthSqliteBackend

        b = OAuthSqliteBackend(db_path)
        yield b
        b.close()

    def test_backend_satisfies_protocol(self, backend):
        """OAuthSqliteBackend must satisfy the OAuthBackend protocol."""
        from code_indexer.server.storage.protocols import OAuthBackend

        assert isinstance(backend, OAuthBackend)

    def test_register_client_and_get_client_round_trip(self, backend):
        """register_client followed by get_client must return the same data."""
        result = backend.register_client(
            client_name="Test Client",
            redirect_uris=["http://localhost/callback"],
        )
        assert "client_id" in result
        client_id = result["client_id"]

        retrieved = backend.get_client(client_id)
        assert retrieved is not None
        assert retrieved["client_id"] == client_id
        assert retrieved["client_name"] == "Test Client"
        assert "http://localhost/callback" in retrieved["redirect_uris"]

    def test_get_client_returns_none_for_unknown(self, backend):
        """get_client must return None for an unknown client_id."""
        result = backend.get_client("nonexistent-client-id")
        assert result is None

    def test_generate_code_and_exchange_token_round_trip(self, backend):
        """generate_authorization_code + exchange_code_for_token must work end-to-end."""
        # Register client
        reg = backend.register_client(
            client_name="PKCE Client",
            redirect_uris=["http://localhost/callback"],
        )
        client_id = reg["client_id"]

        # Build PKCE challenge
        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        # Generate authorization code
        code = backend.generate_authorization_code(
            client_id=client_id,
            user_id="user1",
            code_challenge=code_challenge,
            redirect_uri="http://localhost/callback",
            state="state123",
        )
        assert isinstance(code, str) and len(code) > 0

        # Exchange for token
        token_response = backend.exchange_code_for_token(
            code=code,
            code_verifier=code_verifier,
            client_id=client_id,
        )
        assert "access_token" in token_response
        assert "refresh_token" in token_response
        assert token_response["token_type"] == "Bearer"
        assert token_response["expires_in"] > 0

    def test_exchange_code_fails_on_reuse(self, backend):
        """exchange_code_for_token must reject a code that has already been used."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        reg = backend.register_client(
            client_name="Reuse Test",
            redirect_uris=["http://localhost/cb"],
        )
        client_id = reg["client_id"]

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        code = backend.generate_authorization_code(
            client_id=client_id,
            user_id="user2",
            code_challenge=code_challenge,
            redirect_uri="http://localhost/cb",
            state="s",
        )

        # First exchange succeeds
        backend.exchange_code_for_token(
            code=code, code_verifier=code_verifier, client_id=client_id
        )

        # Second exchange must fail
        with pytest.raises(OAuthError):
            backend.exchange_code_for_token(
                code=code, code_verifier=code_verifier, client_id=client_id
            )

    def test_validate_token_returns_correct_data(self, backend):
        """validate_token must return user/client info for a valid token."""
        reg = backend.register_client(
            client_name="Validate Test",
            redirect_uris=["http://localhost/cb"],
        )
        client_id = reg["client_id"]

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        code = backend.generate_authorization_code(
            client_id=client_id,
            user_id="user3",
            code_challenge=code_challenge,
            redirect_uri="http://localhost/cb",
            state="s",
        )
        token_resp = backend.exchange_code_for_token(
            code=code, code_verifier=code_verifier, client_id=client_id
        )
        access_token = token_resp["access_token"]

        info = backend.validate_token(access_token)
        assert info is not None
        assert info["user_id"] == "user3"
        assert info["client_id"] == client_id

    def test_validate_token_returns_none_for_unknown(self, backend):
        """validate_token must return None for an unknown token."""
        result = backend.validate_token("totally-unknown-token")
        assert result is None

    def test_revoke_token_removes_access(self, backend):
        """revoke_token must prevent subsequent validate_token from succeeding."""
        reg = backend.register_client(
            client_name="Revoke Test",
            redirect_uris=["http://localhost/cb"],
        )
        client_id = reg["client_id"]

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        code = backend.generate_authorization_code(
            client_id=client_id,
            user_id="user4",
            code_challenge=code_challenge,
            redirect_uri="http://localhost/cb",
            state="s",
        )
        token_resp = backend.exchange_code_for_token(
            code=code, code_verifier=code_verifier, client_id=client_id
        )
        access_token = token_resp["access_token"]

        # Token is valid before revocation
        assert backend.validate_token(access_token) is not None

        # Revoke
        result = backend.revoke_token(access_token, token_type_hint="access_token")
        assert result["username"] == "user4"

        # Token is invalid after revocation
        assert backend.validate_token(access_token) is None

    def test_link_oidc_identity_and_get_oidc_identity_round_trip(self, backend):
        """link_oidc_identity + get_oidc_identity must persist and retrieve identity."""
        backend.link_oidc_identity(
            username="alice",
            subject="sub-abc123",
            email="alice@example.com",
        )

        identity = backend.get_oidc_identity("sub-abc123")
        assert identity is not None
        assert identity["username"] == "alice"
        assert identity["subject"] == "sub-abc123"
        assert identity["email"] == "alice@example.com"

    def test_get_oidc_identity_returns_none_for_unknown(self, backend):
        """get_oidc_identity must return None for an unknown subject."""
        result = backend.get_oidc_identity("nonexistent-subject")
        assert result is None

    def test_refresh_access_token_issues_new_tokens(self, backend):
        """refresh_access_token must return new access and refresh tokens."""
        reg = backend.register_client(
            client_name="Refresh Test",
            redirect_uris=["http://localhost/cb"],
        )
        client_id = reg["client_id"]

        code_verifier = secrets.token_urlsafe(32)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        code = backend.generate_authorization_code(
            client_id=client_id,
            user_id="user5",
            code_challenge=code_challenge,
            redirect_uri="http://localhost/cb",
            state="s",
        )
        token_resp = backend.exchange_code_for_token(
            code=code, code_verifier=code_verifier, client_id=client_id
        )
        old_refresh_token = token_resp["refresh_token"]
        old_access_token = token_resp["access_token"]

        new_resp = backend.refresh_access_token(
            refresh_token=old_refresh_token, client_id=client_id
        )
        assert "access_token" in new_resp
        assert "refresh_token" in new_resp
        assert new_resp["access_token"] != old_access_token
        assert new_resp["refresh_token"] != old_refresh_token

    def test_extend_token_on_activity_returns_bool(self, backend):
        """extend_token_on_activity must return a boolean."""
        result = backend.extend_token_on_activity("nonexistent-token")
        assert isinstance(result, bool)
        assert result is False


# ---------------------------------------------------------------------------
# AC3: BackendRegistry and StorageFactory
# ---------------------------------------------------------------------------


class TestBackendRegistryOAuth:
    """Tests that BackendRegistry has the oauth field (AC3)."""

    def test_backend_registry_has_oauth_field(self):
        """BackendRegistry dataclass must declare an 'oauth' field."""
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "oauth" in fields, "BackendRegistry must have an 'oauth' field"

    def test_storage_factory_sqlite_creates_oauth_backend(self, tmp_path):
        """StorageFactory._create_sqlite_backends must populate registry.oauth."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import OAuthBackend

        data_dir = str(tmp_path / "data")
        Path(data_dir).mkdir(parents=True)
        (Path(tmp_path) / "groups.db").touch()

        registry = StorageFactory._create_sqlite_backends(data_dir)
        assert hasattr(registry, "oauth"), "BackendRegistry must have oauth attribute"
        assert isinstance(
            registry.oauth, OAuthBackend
        ), "registry.oauth must satisfy OAuthBackend protocol"
