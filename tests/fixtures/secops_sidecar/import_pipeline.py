"""Processing of one events:import request, independent of the HTTP layer.

Order: size bound, auth, path, envelope, dedup, accept.  Every request is
recorded in the request log exactly once; a rejected request stores NOTHING.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .fault_replies import MALFORMED_BODIES, MALFORMED_CONTENT_TYPES, rejection_for
from .google_errors import field_violation
from .replies import Reply, accepted_reply, error_reply
from .state import CapacityExceeded, RequestRecord
from .udm_rules import REQUEST_LEVEL_FIELD, parse_envelope, validate_events

if TYPE_CHECKING:
    from .server import Sidecar

logger = logging.getLogger("secops_sidecar")


@dataclass(frozen=True)
class ImportRequest:
    """One import request as read from the wire (body already bounded)."""

    seq: int
    path: str
    version: str
    parent: str
    authorization: Optional[str]
    body: bytes


def batch_sha256(instance: str, events: List[Dict[str, Any]]) -> str:
    """Batch identity for dedup: sha256(instance + canonical(events))."""
    if not instance:
        raise ValueError("instance is required")
    if not isinstance(events, list):
        raise TypeError("events must be a list")
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((instance + canonical).encode("utf-8")).hexdigest()


class ImportProcessor:
    """Decides the reply for one import request and records it exactly once."""

    def __init__(self, sidecar: "Sidecar", req: ImportRequest) -> None:
        self.sidecar = sidecar
        self.req = req
        self.auth_ok = False
        self.event_count: Optional[int] = None
        self.duplicate = False
        self.fault: Optional[Dict[str, Any]] = None  # the fault consumed, if any

    def _record(self, reply: Reply) -> Reply:
        fault_mode = self.fault["mode"] if self.fault else None
        self.sidecar.state.record_request(
            RequestRecord(
                seq=self.req.seq,
                path=self.req.path,
                http_status_returned=None if reply.drop else reply.status,
                body_bytes=len(self.req.body),
                event_count=self.event_count,
                body_sha256=hashlib.sha256(self.req.body).hexdigest(),
                auth_ok=self.auth_ok,
                fault_applied=fault_mode,
                duplicate=self.duplicate,
            ),
            self.req.body,
        )
        logger.info(
            "import seq=%d status=%s events=%s fault=%s duplicate=%s",
            self.req.seq, None if reply.drop else reply.status, self.event_count,
            fault_mode, self.duplicate,
        )  # fmt: skip
        return reply

    def _token_ok(self) -> bool:
        scheme, _, token = (self.req.authorization or "").partition(" ")
        if scheme != "Bearer" or not token:
            return False
        return self.sidecar.state.token_valid(token)

    def run(self) -> Reply:
        config = self.sidecar.config
        if len(self.req.body) > config.max_request_bytes:
            reply = error_reply(
                HTTPStatus.BAD_REQUEST,
                violations=[field_violation(REQUEST_LEVEL_FIELD, "Request too large.")],
            )
            reply.close = True  # the rest of the body is never read
            return self._record(reply)
        self.auth_ok = self._token_ok()
        if not self.auth_ok:
            return self._record(error_reply(HTTPStatus.UNAUTHORIZED))
        if self.req.version != config.api_version or self.req.parent != config.parent:
            return self._record(error_reply(HTTPStatus.NOT_FOUND))
        self.fault = self.sidecar.state.pop_fault()
        return self._record(self._after_routing())

    def _after_routing(self) -> Reply:
        envelope = parse_envelope(self.req.body, self.sidecar.config.parent)
        self.event_count = envelope.event_count
        if self.fault is not None:
            rejection = rejection_for(self.fault, self.event_count)
            if rejection is not None:
                return rejection
            if self.fault["mode"] == "delay" and not self.fault["accept"]:
                self.sidecar.shutdown_event.wait(self.fault["seconds"])  # latency
        if envelope.events is None:
            return error_reply(
                HTTPStatus.BAD_REQUEST,
                message=envelope.message,
                violations=envelope.violations,
            )
        poison = self.sidecar.state.poison_ids()
        if any(
            e["udm"].get("metadata", {}).get("productLogId") in poison
            for e in envelope.events
        ):
            self.fault = {"mode": "reject_if_contains"}
            return error_reply(HTTPStatus.BAD_REQUEST)  # unindexed
        state = self.sidecar.state
        if state.self_test_defect == "store_before_validate":
            self._dedup_and_accept(envelope.events)  # negative control ONLY
        violations = validate_events(envelope.events)
        if violations:  # all-or-nothing: one bad event rejects the request
            state.note_rejected(self.req.seq, envelope.events)  # defect-only no-op
            return error_reply(HTTPStatus.BAD_REQUEST, violations=violations)
        return self._post_accept(self._dedup_and_accept(envelope.events))

    def _dedup_and_accept(self, events: List[Dict[str, Any]]) -> Reply:
        state = self.sidecar.state
        digest = batch_sha256(self.sidecar.config.instance, events)
        with state.lock:  # dedup check and accept are one atomic step
            if state.is_duplicate(digest):
                self.duplicate = True
                if state.get_duplicate_mode() == "already_exists":
                    return error_reply(HTTPStatus.CONFLICT)
                return accepted_reply()  # ok_noop: not stored again
            try:
                state.accept_batch(self.req.seq, digest, events)
            except CapacityExceeded:
                logger.warning("import seq=%d refused: store bound", self.req.seq)
                return error_reply(HTTPStatus.INSUFFICIENT_STORAGE)
        return accepted_reply()

    def _post_accept(self, reply: Reply) -> Reply:
        """Faults that act only AFTER the batch was stored (2xx replies)."""
        if self.fault is None or reply.status != HTTPStatus.OK:
            return reply
        mode = self.fault["mode"]
        if mode == "success_body":
            return Reply(HTTPStatus.OK, self.fault["body"].encode("utf-8"))
        if mode == "malformed_response":  # status < 400: stored, odd body
            kind = self.fault["kind"]
            content_type = MALFORMED_CONTENT_TYPES.get(kind, "application/json")
            return Reply(
                self.fault["status"], MALFORMED_BODIES[kind], content_type=content_type
            )
        if mode == "delay" and self.fault["accept"]:
            reply.hold_seconds = self.fault["seconds"]
        if mode == "accept_then_drop":
            reply.drop = True
        return reply


def process_import(sidecar: "Sidecar", req: ImportRequest) -> Reply:
    return ImportProcessor(sidecar, req).run()
