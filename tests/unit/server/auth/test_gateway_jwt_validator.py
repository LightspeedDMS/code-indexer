"""EVO-66121: unit tests for GatewayJwtValidator.

Trust the mcp-gateway as an external RS256 issuer: verify the signature against the
gateway JWKS + iss/aud/exp, extract the per-user identity claims, and — critically —
reject alg-confusion / wrong-audience / expired / wrong-issuer tokens. cidx's own
HS256 tokens must be recognised as "not ours" so they fall through untouched.
"""

import datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from code_indexer.server.auth.gateway_jwt_validator import (
    GatewayJwtValidator,
    GatewayTokenError,
)

ISSUER = "https://mcp.test.lightspeedtools.cloud"
AUDIENCE = "cidx-tools-test"


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJwksClient:
    """Stands in for jwt.PyJWKClient — returns our test public key for any token."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: D401 - mirrors PyJWKClient
        return _FakeSigningKey(self._public_key)


def _make_validator(public_key, **overrides):
    kwargs = dict(
        issuer=ISSUER,
        jwks_uri=f"{ISSUER}/oauth/jwks.json",
        audience=AUDIENCE,
        jwks_client=_FakeJwksClient(public_key),
    )
    kwargs.update(overrides)
    return GatewayJwtValidator(**kwargs)


def _rs256(priv, claims):
    return jwt.encode(claims, priv, algorithm="RS256")


def _base_claims(**overrides):
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "email": "dev@lightspeeddms.com",
        "email_verified": True,
        "preferred_username": "dev",
        "groups": ["engineering", "cidx-users"],
        "iat": now,
        "exp": now + datetime.timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


class TestGatewayJwtValidator:
    def test_valid_token_returns_identity(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        token = _rs256(priv, _base_claims())

        ident = v.validate(token)
        assert ident.subject == "user-123"
        assert ident.email == "dev@lightspeeddms.com"
        assert ident.email_verified is True
        assert ident.username == "dev"
        assert ident.groups == ["engineering", "cidx-users"]

    def test_claims_this_issuer_true_for_our_issuer(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        assert v.claims_this_issuer(_rs256(priv, _base_claims())) is True

    def test_claims_this_issuer_false_for_foreign_issuer(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        token = _rs256(priv, _base_claims(iss="https://evil.example.com"))
        assert v.claims_this_issuer(token) is False

    def test_claims_this_issuer_false_for_opaque_or_hs256(self, keypair):
        _, pub = keypair
        v = _make_validator(pub)
        # a cidx HS256 token / opaque string is not our RS256 issuer
        hs = jwt.encode({"iss": "cidx", "username": "x"}, "secret", algorithm="HS256")
        assert v.claims_this_issuer(hs) is False
        assert v.claims_this_issuer("not-a-jwt") is False

    def test_alg_confusion_hs256_rejected(self, keypair):
        """An HS256 token (the alg-confusion vector — e.g. attacker HMACs with the
        known RSA public key) must be rejected: the validator pins algorithms=["RS256"],
        so PyJWT refuses the HS256 header before any key is used. (PyJWT 2.x also blocks
        HMAC-signing with an asymmetric key at encode time — belt and suspenders.)"""
        _, pub = keypair
        v = _make_validator(pub)
        forged = jwt.encode(_base_claims(), "attacker-known-secret", algorithm="HS256")
        with pytest.raises(GatewayTokenError):
            v.validate(forged)

    def test_wrong_audience_rejected(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        token = _rs256(priv, _base_claims(aud="some-other-service"))
        with pytest.raises(GatewayTokenError):
            v.validate(token)

    def test_expired_token_rejected(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        now = datetime.datetime.now(datetime.timezone.utc)
        token = _rs256(
            priv,
            _base_claims(
                iat=now - datetime.timedelta(hours=2),
                exp=now - datetime.timedelta(hours=1),
            ),
        )
        with pytest.raises(GatewayTokenError):
            v.validate(token)

    def test_missing_sub_rejected(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        claims = _base_claims()
        del claims["sub"]
        token = _rs256(priv, claims)
        with pytest.raises(GatewayTokenError):
            v.validate(token)

    def test_groups_absent_yields_empty_list(self, keypair):
        priv, pub = keypair
        v = _make_validator(pub)
        claims = _base_claims()
        del claims["groups"]
        ident = v.validate(_rs256(priv, claims))
        assert ident.groups == []

    def test_custom_claim_names(self, keypair):
        priv, pub = keypair
        v = _make_validator(
            pub, username_claim="upn", email_claim="mail", groups_claim="roles"
        )
        claims = _base_claims()
        del claims["preferred_username"]
        del claims["email"]
        del claims["groups"]
        claims.update(
            {"upn": "dev2", "mail": "dev2@lightspeeddms.com", "roles": ["admins"]}
        )
        ident = v.validate(_rs256(priv, claims))
        assert ident.username == "dev2"
        assert ident.email == "dev2@lightspeeddms.com"
        assert ident.groups == ["admins"]
