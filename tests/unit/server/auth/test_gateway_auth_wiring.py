"""EVO-66121: unit tests for build_gateway_authenticator (lifespan wiring seam).

Covers the enabled / disabled / fail-safe branches that turn TrustedGatewayConfig into
the live authenticator the /mcp dispatch consumes, and asserts the gateway uses an
ISOLATED identity-link store (distinct from interactive SSO) -- the MR !49 security fix.
"""

import os

from code_indexer.server.auth.gateway_auth_wiring import build_gateway_authenticator
from code_indexer.server.auth.gateway_identity_authenticator import (
    GatewayIdentityAuthenticator,
)
from code_indexer.server.utils.config_manager import TrustedGatewayConfig


def _enabled_cfg(**overrides):
    kwargs = dict(
        enabled=True,
        issuer="https://mcp.test.lightspeedtools.cloud",
        jwks_uri="https://mcp.test.lightspeedtools.cloud/oauth/jwks.json",
        audience="cidx-tools-test",
    )
    kwargs.update(overrides)
    return TrustedGatewayConfig(**kwargs)


async def test_enabled_builds_authenticator_with_isolated_store(tmp_path):
    db_path = str(tmp_path / "gateway_oauth.db")
    auth = await build_gateway_authenticator(
        _enabled_cfg(),
        user_manager=object(),
        jwt_manager=object(),
        oidc_db_path=db_path,
    )
    assert isinstance(auth, GatewayIdentityAuthenticator)
    # OIDCManager.initialize() created the isolated identity-link store at OUR path,
    # not the shared ~/.cidx-server/oauth.db.
    assert os.path.exists(db_path)


async def test_isolated_store_is_not_the_shared_oauth_db(tmp_path):
    db_path = str(tmp_path / "gateway_oauth.db")
    auth = await build_gateway_authenticator(
        _enabled_cfg(),
        user_manager=object(),
        jwt_manager=object(),
        oidc_db_path=db_path,
    )
    # The wrapped OIDCManager points at the distinct store -> gateway subjects can never
    # collide with the interactive-SSO IdP's subjects in a shared table.
    assert auth._oidc_manager.db_path == db_path
    assert not auth._oidc_manager.db_path.endswith("oauth.db") or "gateway" in db_path


async def test_disabled_config_returns_none():
    assert (
        await build_gateway_authenticator(
            _enabled_cfg(enabled=False), user_manager=object(), jwt_manager=object()
        )
        is None
    )


async def test_none_config_returns_none():
    assert (
        await build_gateway_authenticator(
            None, user_manager=object(), jwt_manager=object()
        )
        is None
    )


async def test_misconfigured_enabled_fails_safe_to_none():
    # enabled but issuer empty -> GatewayJwtValidator raises ValueError -> caught -> None.
    # Never a partially-wired authenticator.
    bad = TrustedGatewayConfig(enabled=True, issuer="", jwks_uri="x", audience="y")
    assert (
        await build_gateway_authenticator(
            bad, user_manager=object(), jwt_manager=object()
        )
        is None
    )
