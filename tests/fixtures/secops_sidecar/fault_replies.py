"""Replies for the REJECTING fault modes (nothing is ever stored for them)."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Dict, Optional

from .google_errors import field_violation
from .replies import Reply, error_reply

OUT_OF_RANGE_OFFSET = 5  # reject_out_of_range names events[event_count + 5]
EVENT_TYPE_PATH = "udm.metadata.eventType"
REQUEST_LEVEL_PARENT = "parent"
MALFORMED_BODIES: Dict[str, bytes] = {
    "non_json": b"<html><body>Service error</body></html>",
    "truncated_json": b'{"error":{"code":400,"message":"Request contains an inv',
    "wrong_shape": b'{"unexpected":"shape"}',
    "empty": b"",
}
MALFORMED_CONTENT_TYPES = {"non_json": "text/html"}


def _event_field(index: int, path: str) -> str:
    return f"inline_source.events[{index}].{path}"


def rejection_for(fault: Dict[str, Any], event_count: Optional[int]) -> Optional[Reply]:
    """The reply for a rejecting fault, or None when *fault* does not reject."""
    mode = fault["mode"]
    bad = HTTPStatus.BAD_REQUEST
    if mode == "reject_event":
        field = _event_field(fault["event_index"], fault["path"])
        return error_reply(bad, violations=[field_violation(field)])
    if mode in ("reject_unindexed", "reject_if_contains"):
        return error_reply(bad)
    if mode == "reject_request":
        return error_reply(bad, violations=[field_violation(REQUEST_LEVEL_PARENT)])
    if mode == "status":
        return error_reply(fault["code"])
    if mode == "reject_mixed":
        indexed = _event_field(fault["event_index"], EVENT_TYPE_PATH)
        violations = [field_violation(indexed), field_violation(REQUEST_LEVEL_PARENT)]
        return error_reply(bad, violations=violations)
    if mode == "reject_out_of_range":
        index = (event_count or 0) + OUT_OF_RANGE_OFFSET
        return error_reply(
            bad, violations=[field_violation(_event_field(index, "udm"))]
        )
    if mode == "malformed_response" and fault["status"] >= bad:
        kind = fault["kind"]
        content_type = MALFORMED_CONTENT_TYPES.get(kind, "application/json")
        return Reply(fault["status"], MALFORMED_BODIES[kind], content_type=content_type)
    if mode == "redirect":
        return Reply(fault["code"], b"", headers={"Location": fault["location"]})
    if mode == "rate_limit":
        reply = error_reply(HTTPStatus.TOO_MANY_REQUESTS)
        reply.headers["Retry-After"] = str(fault["retry_after_seconds"])
        return reply
    if mode == "echo_marker":
        marker = fault["marker"]
        field = _event_field(fault["event_index"], "udm.metadata")
        violation = field_violation(field, f"Rejected value near {marker}")
        return error_reply(
            bad, message=f"Invalid event: {marker}", violations=[violation]
        )
    return None
