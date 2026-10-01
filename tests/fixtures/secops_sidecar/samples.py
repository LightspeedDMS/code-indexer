"""Neutral sample UDM events and request bodies (no real ids or addresses).

The USER_LOGIN shape follows Google's documented events:import example:
metadata (eventTimestamp, eventType, vendorName, productName, productLogId),
principal.ip[], target.user.userid, securityResult[].action[],
extensions.auth.  Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

SAMPLE_TIMESTAMP = "2026-09-30T12:00:00Z"
SAMPLE_PRINCIPAL_IP = "192.0.2.10"


def user_login_udm(
    product_log_id: str,
    *,
    event_type: str = "USER_LOGIN",
    userid: str = "example-user",
) -> Dict[str, Any]:
    return {
        "metadata": {
            "eventTimestamp": SAMPLE_TIMESTAMP,
            "eventType": event_type,
            "vendorName": "ExampleVendor",
            "productName": "example-product",
            "productEventType": "authentication_success",
            "productLogId": product_log_id,
        },
        "principal": {"ip": [SAMPLE_PRINCIPAL_IP]},
        "target": {"user": {"userid": userid}},
        "securityResult": [{"action": ["ALLOW"]}],
        "extensions": {"auth": {"type": "MACHINE", "mechanism": ["USERNAME_PASSWORD"]}},
        "additional": {"outcome": "success", "schema_version": 1},
    }


def envelope(udms: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"inlineSource": {"events": [{"udm": u} for u in udms]}}


def batch_body(udms: List[Dict[str, Any]]) -> bytes:
    """Canonical JSON bytes: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(
        envelope(udms), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
