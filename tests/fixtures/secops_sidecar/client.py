"""A minimal Chronicle-style client for tests: mint a token, post a batch.

The assertion mirrors google-auth's service_account.Credentials JWT-bearer
grant (RS256; header kid = private_key_id; claims iss = client_email,
aud = token_uri, scope, iat, exp).  It never logs or returns key material.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, Optional

import httpx
import jwt

if TYPE_CHECKING:
    from .harness import SidecarCoordinates

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CHRONICLE_SCOPE = "https://www.googleapis.com/auth/chronicle"
ASSERTION_LIFETIME_SECONDS = 3600
CLIENT_TIMEOUT_SECONDS = 30.0


def build_assertion(
    key: Dict[str, Any],
    *,
    audience: str,
    scope: str = CHRONICLE_SCOPE,
    issued_at: Optional[int] = None,
) -> str:
    """A signed service-account assertion for *audience* (the token URI)."""
    iat = int(time.time()) if issued_at is None else issued_at
    claims: Dict[str, Any] = {
        "iss": key["client_email"],
        "aud": audience,
        "iat": iat,
        "exp": iat + ASSERTION_LIFETIME_SECONDS,
    }
    if scope:
        claims["scope"] = scope
    return jwt.encode(
        claims,
        key["private_key"],
        algorithm="RS256",
        headers={"kid": key["private_key_id"]},
    )


def request_token(
    coords: "SidecarCoordinates",
    assertion: str,
    *,
    grant_type: str = JWT_BEARER_GRANT,
) -> httpx.Response:
    return httpx.post(
        coords.token_uri,
        data={"grant_type": grant_type, "assertion": assertion},
        timeout=CLIENT_TIMEOUT_SECONDS,
    )


def mint_token(coords: "SidecarCoordinates", key: Dict[str, Any]) -> str:
    """A fresh access token from the sidecar, or RuntimeError (never logged)."""
    resp = request_token(coords, build_assertion(key, audience=coords.token_uri))
    if resp.status_code != 200:
        raise RuntimeError(f"token request failed with HTTP {resp.status_code}")
    token = resp.json()["access_token"]
    if not isinstance(token, str):
        raise RuntimeError("token response carried no string access_token")
    return token


def post_import(
    coords: "SidecarCoordinates",
    token: Optional[str],
    body: bytes,
    *,
    path: Optional[str] = None,
    timeout: float = CLIENT_TIMEOUT_SECONDS,
) -> httpx.Response:
    """POST raw bytes to events:import; redirects are never followed."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return httpx.post(
        coords.harness_endpoint + (path or coords.import_path),
        content=body,
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
    )
