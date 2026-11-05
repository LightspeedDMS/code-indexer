"""EVO-66121: unit tests for GatewayIdentityAuthenticator.

The authenticator bridges GatewayJwtValidator (crypto/claims) and OIDCManager (JIT
provisioning + group mapping): a gateway-forwarded RS256 JWT becomes a cidx User via
the SAME match_or_create_user path interactive SSO users use, so cidx enforces its own
per-user access for that user.
"""

import pytest

from code_indexer.server.auth.gateway_jwt_validator import (
    GatewayIdentity,
    GatewayTokenError,
)
from code_indexer.server.auth.gateway_identity_authenticator import (
    GatewayIdentityAuthenticator,
)


class _FakeValidator:
    def __init__(self, *, mine=True, identity=None, raise_on_validate=None):
        self._mine = mine
        self._identity = identity
        self._raise = raise_on_validate

    def claims_this_issuer(self, token):
        return self._mine

    def validate(self, token):
        if self._raise is not None:
            raise self._raise
        return self._identity


class _FakeOidcManager:
    """Captures the OIDCUserInfo passed to match_or_create_user and returns a stub."""

    def __init__(self, return_user):
        self._return_user = return_user
        self.captured = None

    async def match_or_create_user(self, user_info):
        self.captured = user_info
        return self._return_user


_IDENT = GatewayIdentity(
    subject="user-123",
    email="dev@lightspeeddms.com",
    email_verified=True,
    username="dev",
    groups=["engineering", "cidx-users"],
)


class TestGatewayIdentityAuthenticator:
    def test_handles_delegates_to_validator(self):
        auth = GatewayIdentityAuthenticator(
            _FakeValidator(mine=True), _FakeOidcManager("u")
        )
        assert auth.handles("tok") is True
        auth2 = GatewayIdentityAuthenticator(
            _FakeValidator(mine=False), _FakeOidcManager("u")
        )
        assert auth2.handles("tok") is False

    @pytest.mark.asyncio
    async def test_authenticate_maps_claims_and_provisions(self):
        sentinel_user = object()
        mgr = _FakeOidcManager(sentinel_user)
        auth = GatewayIdentityAuthenticator(_FakeValidator(identity=_IDENT), mgr)

        user = await auth.authenticate("tok")

        assert user is sentinel_user
        # verify the identity claims were mapped onto the OIDCUserInfo unchanged
        assert mgr.captured.subject == "user-123"
        assert mgr.captured.email == "dev@lightspeeddms.com"
        assert mgr.captured.email_verified is True
        assert mgr.captured.username == "dev"
        assert mgr.captured.groups == ["engineering", "cidx-users"]

    @pytest.mark.asyncio
    async def test_empty_groups_map_to_none(self):
        mgr = _FakeOidcManager(object())
        ident = GatewayIdentity(
            subject="s", email=None, email_verified=False, username="u", groups=[]
        )
        auth = GatewayIdentityAuthenticator(_FakeValidator(identity=ident), mgr)
        await auth.authenticate("tok")
        # OIDCUserInfo semantics: no groups == None (matches OIDCProvider.get_user_info)
        assert mgr.captured.groups is None

    @pytest.mark.asyncio
    async def test_invalid_token_propagates(self):
        auth = GatewayIdentityAuthenticator(
            _FakeValidator(raise_on_validate=GatewayTokenError("bad sig")),
            _FakeOidcManager(object()),
        )
        with pytest.raises(GatewayTokenError):
            await auth.authenticate("tok")

    @pytest.mark.asyncio
    async def test_failed_provisioning_raises(self):
        # match_or_create_user returns None (JIT disabled / username collision / email
        # unverified) -> authenticator surfaces it as a token error (caller -> 401).
        auth = GatewayIdentityAuthenticator(
            _FakeValidator(identity=_IDENT), _FakeOidcManager(None)
        )
        with pytest.raises(GatewayTokenError):
            await auth.authenticate("tok")
