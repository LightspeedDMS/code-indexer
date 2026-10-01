"""Google-style error bodies with FIXED message templates.

Shape: {"error": {"code": C, "message": M, "status": S[, "details": [...]]}},
where a validation rejection carries one google.rpc.BadRequest detail with
fieldViolations.  Only the echo/malformed fault modes ever put caller text
in a body (to test the client's hygiene).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

BAD_REQUEST_TYPE = "type.googleapis.com/google.rpc.BadRequest"

STATUS_FOR_CODE: Dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    411: "INVALID_ARGUMENT",
    413: "INVALID_ARGUMENT",
    415: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
    507: "RESOURCE_EXHAUSTED",
}

MESSAGE_FOR_CODE: Dict[int, str] = {
    400: "Request contains an invalid argument.",
    401: "Request had invalid authentication credentials.",
    403: "The caller does not have permission.",
    404: "Requested entity was not found.",
    409: "The batch was already imported.",
    411: "Content-Length is required.",
    413: "Request payload size exceeds the limit.",
    415: "Unsupported content type.",
    429: "Resource has been exhausted.",
    500: "Internal error encountered.",
    501: "Operation is not implemented, or supported, or enabled.",
    502: "Bad gateway.",
    503: "The service is currently unavailable.",
    504: "Deadline exceeded.",
    507: "Sidecar received-store bound reached.",
}

VIOLATION_DESCRIPTION = "Invalid value for this field."


def field_violation(
    field: str, description: str = VIOLATION_DESCRIPTION
) -> Dict[str, str]:
    return {"field": field, "description": description}


def error_body(
    code: int,
    *,
    message: Optional[str] = None,
    violations: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    if code not in STATUS_FOR_CODE:
        raise ValueError(f"no Google status mapped for HTTP {code}")
    error: Dict[str, Any] = {
        "code": code,
        "message": message if message is not None else MESSAGE_FOR_CODE[code],
        "status": STATUS_FOR_CODE[code],
    }
    if violations:
        error["details"] = [
            {"@type": BAD_REQUEST_TYPE, "fieldViolations": list(violations)}
        ]
    return {"error": error}
