"""The OAuth token endpoint (service-account JWT-bearer grant).

google-auth is not yet a project dependency, so the assertion is built with
PyJWT exactly as google-auth's service_account.Credentials builds it
(RS256, kid = private_key_id, iss = client_email, aud = token_uri, scope,
iat/exp).  The real google-auth refresh test arrives with the delivery story.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.fixtures.secops_sidecar.client import (
    CHRONICLE_SCOPE,
    build_assertion,
    request_token,
)
from tests.fixtures.secops_sidecar.harness import SidecarHandle


def _other_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _assert_invalid_grant(resp: Any) -> None:
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_grant"
    assert isinstance(body["error_description"], str)


def test_valid_assertion_gets_an_opaque_bearer_token(sidecar: SidecarHandle) -> None:
    key: Dict[str, Any] = sidecar.read_key_file()
    assertion = build_assertion(key, audience=sidecar.coords.token_uri)
    resp = request_token(sidecar.coords, assertion)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert isinstance(body["access_token"], str) and len(body["access_token"]) >= 32
    assert "." not in body["access_token"]  # opaque, not a JWT


def test_wrong_grant_type_is_invalid_grant(sidecar: SidecarHandle) -> None:
    key = sidecar.read_key_file()
    assertion = build_assertion(key, audience=sidecar.coords.token_uri)
    resp = request_token(sidecar.coords, assertion, grant_type="client_credentials")
    _assert_invalid_grant(resp)


def test_assertion_signed_by_another_key_is_invalid_grant(
    sidecar: SidecarHandle,
) -> None:
    key = dict(sidecar.read_key_file())
    key["private_key"] = _other_private_key_pem()
    assertion = build_assertion(key, audience=sidecar.coords.token_uri)
    _assert_invalid_grant(request_token(sidecar.coords, assertion))


def test_wrong_audience_is_invalid_grant(sidecar: SidecarHandle) -> None:
    key = sidecar.read_key_file()
    assertion = build_assertion(key, audience="https://token.example.com/token")
    _assert_invalid_grant(request_token(sidecar.coords, assertion))


def test_google_auth_audience_is_accepted(sidecar: SidecarHandle) -> None:
    """google-auth always signs aud=Google's token endpoint, whatever
    token_uri it posts to; the sidecar must accept what the real client sends."""
    key = sidecar.read_key_file()
    assertion = build_assertion(key, audience="https://oauth2.googleapis.com/token")
    assert request_token(sidecar.coords, assertion).status_code == 200


def test_expired_assertion_is_invalid_grant(sidecar: SidecarHandle) -> None:
    key = sidecar.read_key_file()
    assertion = build_assertion(
        key, audience=sidecar.coords.token_uri, issued_at=int(time.time()) - 7200
    )
    _assert_invalid_grant(request_token(sidecar.coords, assertion))


def test_missing_scope_is_invalid_grant(sidecar: SidecarHandle) -> None:
    key = sidecar.read_key_file()
    assertion = build_assertion(key, audience=sidecar.coords.token_uri, scope="")
    _assert_invalid_grant(request_token(sidecar.coords, assertion))


def test_unexpected_issuer_is_invalid_grant(sidecar: SidecarHandle) -> None:
    key = dict(sidecar.read_key_file())
    key["client_email"] = "someone-else@example.com"  # same key, other issuer
    assertion = build_assertion(key, audience=sidecar.coords.token_uri)
    _assert_invalid_grant(request_token(sidecar.coords, assertion))


def test_unexpected_scope_is_invalid_grant(sidecar: SidecarHandle) -> None:
    key = sidecar.read_key_file()
    assertion = build_assertion(
        key,
        audience=sidecar.coords.token_uri,
        scope="https://www.googleapis.com/auth/devstorage.read_only",
    )
    _assert_invalid_grant(request_token(sidecar.coords, assertion))


def test_cloud_platform_scope_is_accepted(sidecar: SidecarHandle) -> None:
    key = sidecar.read_key_file()
    assertion = build_assertion(
        key,
        audience=sidecar.coords.token_uri,
        scope="https://www.googleapis.com/auth/cloud-platform",
    )
    assert request_token(sidecar.coords, assertion).status_code == 200


def test_chronicle_scope_constant_is_googles() -> None:
    assert CHRONICLE_SCOPE == "https://www.googleapis.com/auth/chronicle"
