"""POST /token: Google's service-account JWT-bearer grant, emulated.

Issued tokens are random opaque strings kept in memory with an expiry; they
are never logged and never returned by the control API.  Error descriptions
are fixed templates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import parse_qs

import jwt

from .http_util import content_length, send_json
from .server import ALLOWED_TOKEN_SCOPES

if TYPE_CHECKING:
    from .ingest_api import IngestHandler

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN_FORM_LIMIT = 64 * 1024
INVALID_GRANT_DESCRIPTION = "Invalid JWT assertion."
UNAVAILABLE_DESCRIPTION = "The token service is temporarily unavailable."
logger = logging.getLogger("secops_sidecar")


def _form(handler: "IngestHandler") -> Optional[Dict[str, str]]:
    declared = content_length(handler)
    if declared is None or declared > TOKEN_FORM_LIMIT:
        handler.close_connection = True
        return None
    raw = handler.rfile.read(declared).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(raw).items()}


def _assertion_ok(handler: "IngestHandler", assertion: str) -> bool:
    config = handler.sidecar.config
    try:
        claims = jwt.decode(
            assertion,
            config.public_key_pem,
            algorithms=["RS256"],
            audience=config.token_audience,
            issuer=config.token_issuer,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError:
        return False
    scope = claims.get("scope")
    if not isinstance(scope, str):
        return False
    requested = scope.split()
    return bool(requested) and all(s in ALLOWED_TOKEN_SCOPES for s in requested)


def _invalid_grant(handler: "IngestHandler") -> None:
    send_json(
        handler,
        400,
        {"error": "invalid_grant", "error_description": INVALID_GRANT_DESCRIPTION},
    )


def _send_token_fault(handler: "IngestHandler", fault: Dict[str, Any]) -> None:
    """reject -> 400 invalid_grant; unavailable -> 503; echo -> 400 + marker."""
    if fault["mode"] == "unavailable":
        send_json(
            handler,
            503,
            {
                "error": "temporarily_unavailable",
                "error_description": UNAVAILABLE_DESCRIPTION,
            },
        )
        return
    description = INVALID_GRANT_DESCRIPTION
    if fault["mode"] == "echo":
        description = f"{INVALID_GRANT_DESCRIPTION} {fault['marker']}"
    send_json(
        handler, 400, {"error": "invalid_grant", "error_description": description}
    )


def handle_token(handler: "IngestHandler") -> None:
    form = _form(handler)
    if form is None:
        _invalid_grant(handler)
        return
    assertion = form.get("assertion", "")
    if form.get("grant_type") != JWT_BEARER_GRANT or not assertion:
        _invalid_grant(handler)
        return
    if not _assertion_ok(handler, assertion):
        logger.info("token: assertion rejected")
        _invalid_grant(handler)
        return
    fault = handler.sidecar.state.pop_token_fault()
    if fault is not None:
        logger.info("token: fault %s applied", fault["mode"])
        _send_token_fault(handler, fault)
        return
    ttl = handler.sidecar.config.token_ttl_seconds
    token = handler.sidecar.state.issue_token(ttl)
    logger.info("token: issued (ttl=%ds)", ttl)
    send_json(
        handler,
        200,
        {"access_token": token, "expires_in": ttl, "token_type": "Bearer"},
    )
