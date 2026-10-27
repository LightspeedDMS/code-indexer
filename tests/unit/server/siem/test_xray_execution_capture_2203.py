"""Story #2203: an X-Ray evaluator run is audited, never queued for SIEM
delivery (owner decision: audit log only). SQLite and PostgreSQL."""

from __future__ import annotations

from typing import Iterator

import pytest

from code_indexer.server.services.audit_events import build_event
from code_indexer.server.services.siem_delivery import capture
from code_indexer.server.services.siem_delivery.capture import (
    CaptureSnapshot,
    publish_capture_state,
)

from .backends import SiemBackendHarness

DEST = "gsecops:00000000000000aa"
_HASH = "ab" * 32


@pytest.fixture()
def armed() -> Iterator[None]:
    capture.reset_capture_state_for_tests(lambda: 1000.0)
    publish_capture_state(CaptureSnapshot(True, True, DEST, 1000.0))
    yield
    capture.reset_capture_state_for_tests()


def test_armed_xray_execution_is_audited_but_never_queued_for_siem(
    siem_backend: SiemBackendHarness, armed: None
) -> None:
    event = build_event(
        actor="alice",
        action_type="xray_evaluator_executed",
        target_type="repo",
        target_id="example-repo-global",
        outcome="success",
        details={"evaluator_sha256s": [_HASH], "pattern_names": ["catch-rethrow"]},
    )
    siem_backend.audit.insert_events([event])

    audited = siem_backend.db.read(
        lambda tx: tx.query("SELECT event_uuid FROM audit_logs")
    )
    queued = siem_backend.db.read(
        lambda tx: tx.query("SELECT event_uuid FROM siem_delivery_queue")
    )
    assert [r["event_uuid"] for r in audited] == [event.event_uuid]
    assert queued == []
