"""The delivery engine against a real store (SQLite and PostgreSQL) and the
real SecOps sidecar."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.audit_events import AuditEvent, build_event
from code_indexer.server.services.siem_delivery import capture, state_store
from code_indexer.server.services.siem_delivery import claim as claim_mod
from code_indexer.server.services.siem_delivery.capture import CaptureSnapshot
from code_indexer.server.services.siem_delivery.claim import EngineContext, claim_batch
from code_indexer.server.services.siem_delivery.completion import send_and_complete
from code_indexer.server.services.siem_delivery.probe import (
    requeue_after_mapping_change,
    run_tick,
)
from code_indexer.server.services.siem_delivery.sender import CredentialProvider
from code_indexer.server.services.siem_delivery.timings import HARNESS_TIMINGS
from code_indexer.server.services.siem_delivery.udm import UDM_MAPPING
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness
from .conftest import harness_destination, sidecar_loader

_PAST = "2000-01-01T00:00:00.000Z"
FAST = dataclasses.replace(
    HARNESS_TIMINGS,
    backoff_base_seconds=0.0,
    backoff_cap_seconds=0.0,
    probe_interval_seconds=0.0,
    tick_time_budget_seconds=60.0,
)


@pytest.fixture()
def engine(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> Iterator[EngineContext]:
    capture.reset_capture_state_for_tests()
    dest = harness_destination(siem_sidecar)
    factory = HttpClientFactory(fault_injection_service=None)
    ctx = EngineContext(
        db=siem_backend.db,
        timings=FAST,
        http_factory=factory,
        credentials=CredentialProvider(factory, sidecar_loader(siem_sidecar), 5.0),
        process_id="solo:1:test",
        destination=dest,
        max_batch_events=1000,
        source_instance_label="example-label",
        config_epoch="",
    )
    capture.publish_capture_state(
        CaptureSnapshot(True, True, dest.key, capture.monotonic_now())
    )
    yield ctx
    capture.reset_capture_state_for_tests()


def _login(n: int) -> AuditEvent:
    return build_event(
        actor=f"user{n}",
        action_type="authentication_success",
        target_type="auth",
        target_id=f"user{n}",
        outcome="success",
        details={"method": "password", "mfa": "not_enrolled", "flow": "rest_token"},
    )


def _capture(b: SiemBackendHarness, n: int) -> List[str]:
    events = [_login(i) for i in range(n)]
    b.audit.insert_events(events)
    return [e.event_uuid for e in events]


def _statuses(b: SiemBackendHarness) -> Dict[str, int]:
    rows = b.db.read(
        lambda tx: tx.query(
            "SELECT status, COUNT(*) AS n FROM siem_delivery_queue GROUP BY status"
        )
    )
    return {r["status"]: int(r["n"]) for r in rows}


def _requests(sidecar: SidecarHandle) -> List[Dict[str, Any]]:
    requests: List[Dict[str, Any]] = sidecar.control.get("/_control/requests").json()[
        "requests"
    ]
    return requests


def _fault(sidecar: SidecarHandle, spec: Dict[str, Any]) -> None:
    assert sidecar.control.post("/_control/faults", spec).status_code == 200


def _ticks(ctx: EngineContext, n: int = 30) -> None:
    for _ in range(n):
        run_tick(ctx)


def _state(ctx: EngineContext) -> Dict[str, Any]:
    return state_store.read_state(ctx.db)


def test_envelope_is_chronicles_and_rows_go_in_id_order(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    uuids = _capture(siem_backend, 10)
    run_tick(engine)
    reqs = _requests(siem_sidecar)
    assert len(reqs) == 1 and reqs[0]["event_count"] == 10
    assert reqs[0]["path"] == engine.destination.import_path
    body = siem_sidecar.control.get(f"/_control/requests/{reqs[0]['seq']}/body").json()
    assert list(body) == ["inlineSource"]
    ids = [e["udm"]["metadata"]["productLogId"] for e in body["inlineSource"]["events"]]
    assert ids == uuids
    assert _statuses(siem_backend) == {"delivered": 10}
    assert _state(engine)["delivered_total"] == 10


def test_indexed_rejection_quarantines_only_that_row(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    uuids = _capture(siem_backend, 8)
    _fault(
        siem_sidecar,
        {"mode": "echo_marker", "marker": "VENDOR-MARKER-55", "event_index": 3},
    )
    _ticks(engine, 5)
    assert _statuses(siem_backend) == {"delivered": 7, "quarantined": 1}
    row = siem_backend.db.read(
        lambda tx: tx.one(
            "SELECT event_uuid, quarantine_signature FROM siem_delivery_queue "
            "WHERE status = 'quarantined'"
        )
    )
    assert row is not None
    assert row["event_uuid"] == uuids[3]
    assert "VENDOR-MARKER-55" not in row["quarantine_signature"]
    assert "events[*]" in row["quarantine_signature"]
    assert _state(engine)["halted_class"] is None


def test_unindexed_rejection_bisects_within_bound(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    uuids = _capture(siem_backend, 8)
    _fault(siem_sidecar, {"mode": "reject_if_contains", "product_log_id": uuids[5]})
    _ticks(engine, 20)
    assert _statuses(siem_backend) == {"delivered": 7, "quarantined": 1}
    assert len(_requests(siem_sidecar)) <= 2 * 8 - 1


@pytest.mark.parametrize(
    "fault,cls",
    [
        ({"mode": "reject_request"}, "request_rejection"),
        ({"mode": "reject_mixed", "event_index": 1}, "request_rejection"),
        ({"mode": "reject_out_of_range"}, "unclassified"),
        ({"mode": "status", "code": 404}, "request_rejection"),
        ({"mode": "status", "code": 413}, "request_rejection"),
        ({"mode": "status", "code": 415}, "request_rejection"),
        ({"mode": "status", "code": 501}, "request_rejection"),
        ({"mode": "status", "code": 403}, "credential"),
        ({"mode": "status", "code": 409}, "duplicate_response"),
    ],
)
def test_systemic_responses_halt_without_quarantine(
    engine: EngineContext,
    siem_backend: SiemBackendHarness,
    siem_sidecar: SidecarHandle,
    fault: Dict[str, Any],
    cls: str,
) -> None:
    _capture(siem_backend, 4)
    _fault(siem_sidecar, fault)
    run_tick(engine)
    st = _state(engine)
    assert st["halted_class"] == cls
    assert st["halted_batch_id"]
    assert _statuses(siem_backend) == {"batched": 4}


def test_credential_halt_probe_resends_identical_bytes_and_clears(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 2)
    _fault(siem_sidecar, {"mode": "status", "code": 401})
    run_tick(engine)
    assert _state(engine)["halted_class"] == "credential"
    run_tick(engine)  # probe due at once (interval 0)
    reqs = _requests(siem_sidecar)
    bodies = [
        siem_sidecar.control.get(f"/_control/requests/{r['seq']}/body").content
        for r in reqs
    ]
    assert [r["http_status_returned"] for r in reqs] == [401, 200]
    assert bodies[0] == bodies[1]
    assert _state(engine)["halted_class"] is None
    assert _statuses(siem_backend) == {"delivered": 2}


def test_duplicate_halt_is_never_probed(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 1)
    _fault(siem_sidecar, {"mode": "status", "code": 409})
    _ticks(engine, 5)
    assert len(_requests(siem_sidecar)) == 1
    assert _state(engine)["halted_class"] == "duplicate_response"
    assert _state(engine)["delivered_total"] == 0


def test_transient_resends_same_bytes(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 3)
    _fault(siem_sidecar, {"mode": "status", "code": 503})
    _ticks(engine, 3)
    reqs = _requests(siem_sidecar)
    assert [r["http_status_returned"] for r in reqs] == [503, 200]
    assert reqs[0]["body_sha256"] == reqs[1]["body_sha256"]
    assert _statuses(siem_backend) == {"delivered": 3}


def test_throttle_honours_retry_after_in_db_time(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 1)
    _fault(siem_sidecar, {"mode": "rate_limit", "retry_after_seconds": 120})
    run_tick(engine)
    row = siem_backend.db.read(
        lambda tx: (
            tx.one("SELECT next_attempt_at, last_sent_at FROM siem_delivery_batches"),
            tx.dialect,
        )
    )
    batch, dialect = row
    assert batch is not None
    next_at = dialect.parse_ts(batch["next_attempt_at"])
    sent_at = dialect.parse_ts(batch["last_sent_at"])
    assert next_at is not None and sent_at is not None
    gap = next_at - sent_at
    assert gap >= timedelta(seconds=119)
    _ticks(engine, 3)
    assert len(_requests(siem_sidecar)) == 1


def test_throttle_ends_the_tick_for_the_destination(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 6)
    _fault(siem_sidecar, {"mode": "rate_limit", "retry_after_seconds": 120})
    result = run_tick(dataclasses.replace(engine, max_batch_events=2))
    assert result == {"batches_sent": 1}
    assert len(_requests(siem_sidecar)) == 1


def _broken_mapping() -> Dict[str, str]:
    return {**UDM_MAPPING, "authentication_success": "NOT_A_UDM_TYPE"}


@pytest.mark.parametrize("batch", [1, 20])
def test_local_validation_burst_halts_after_five_then_self_heals(
    engine: EngineContext,
    siem_backend: SiemBackendHarness,
    siem_sidecar: SidecarHandle,
    batch: int,
) -> None:
    _capture(siem_backend, 20)
    broken = dataclasses.replace(
        engine, mapping=_broken_mapping(), max_batch_events=batch
    )
    _ticks(broken, 30)
    assert _statuses(siem_backend) == {"quarantined": 5, "pending": 15}
    assert _state(engine)["halted_class"] == "local_validation_burst"
    assert _requests(siem_sidecar) == []
    # still broken: the probe sends nothing and keeps the halt
    _ticks(broken, 2)
    assert _requests(siem_sidecar) == [] and _state(engine)["halted_class"]
    fixed = dataclasses.replace(engine, mapping_version=engine.mapping_version + 1)
    run_tick(fixed)
    assert _state(engine)["halted_class"] is None
    _ticks(fixed, 3)
    assert _statuses(siem_backend) == {"quarantined": 5, "delivered": 15}


def test_local_probe_never_reopens_a_full_quarantine_window(
    engine: EngineContext,
    siem_backend: SiemBackendHarness,
    siem_sidecar: SidecarHandle,
) -> None:
    _capture(siem_backend, 20)
    _ticks(dataclasses.replace(engine, mapping=_broken_mapping()), 30)
    assert _state(engine)["halted_class"] == "local_validation_burst"
    assert _state(engine)["quarantine_window_count"] == 5
    # the original cause is fixed, but the sample holds ANOTHER failure: the
    # full window cannot absorb it, so the halt and the window both stay
    _insert_projection_error_row(
        siem_backend,
        "22222222-2222-2222-2222-222222222222",
        engine.destination.key,
        mapping_version=engine.mapping_version,  # fails under the CURRENT code
    )
    assert run_tick(engine) == {"probe": "still_failing"}
    assert _state(engine)["halted_class"] == "local_validation_burst"
    assert _state(engine)["quarantine_window_count"] == 5
    # nothing failing any more: the halt clears, the window is retained
    siem_backend.raw(
        "DELETE FROM siem_delivery_queue WHERE event_uuid = ?",
        ("22222222-2222-2222-2222-222222222222",),
    )
    assert run_tick(engine) == {"probe": "cleared"}
    assert _state(engine)["halted_class"] is None
    assert _state(engine)["quarantine_window_count"] == 5


@pytest.mark.parametrize(
    "fault",
    [
        {"mode": "reject_event", "event_index": 0, "count": 30},
        {"mode": "reject_unindexed", "count": 30},
    ],
)
def test_remote_rejections_with_singletons_halt_after_five(
    engine: EngineContext,
    siem_backend: SiemBackendHarness,
    siem_sidecar: SidecarHandle,
    fault: Dict[str, Any],
) -> None:
    _capture(siem_backend, 20)
    _fault(siem_sidecar, fault)
    slow_probe = dataclasses.replace(FAST, probe_interval_seconds=3600)
    one = dataclasses.replace(engine, max_batch_events=1, timings=slow_probe)
    _ticks(one, 40)
    assert _statuses(siem_backend) == {"quarantined": 5, "pending": 15}
    assert _state(engine)["halted_class"] == "row_rejection_burst"


def test_k2_stale_completion_updates_nothing(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 1)
    first = claim_batch(engine)
    assert first is not None
    siem_backend.raw(
        "UPDATE siem_delivery_batches SET lease_expires_at = ?",
        (_PAST if siem_backend.name == "sqlite" else "2000-01-01T00:00:00Z",),
    )
    second = claim_batch(engine)
    assert second is not None and second.fence > first.fence
    stale = send_and_complete(engine, first)
    assert not stale.completed
    fresh = send_and_complete(engine, second)
    assert fresh.completed
    assert _statuses(siem_backend) == {"delivered": 1}
    assert _state(engine)["delivered_total"] == 1


def test_k5_row_changed_between_read_and_claim_rolls_back(
    engine: EngineContext, siem_backend: SiemBackendHarness
) -> None:
    _capture(siem_backend, 3)
    rows = claim_mod.read_candidate_rows(engine, 10)
    cand = claim_mod.build_candidates(engine, rows)
    siem_backend.raw(
        "UPDATE siem_delivery_queue SET status = 'abandoned' WHERE id = ?",
        (rows[1]["id"],),
    )
    assert claim_mod._phase4(engine, cand, cand.ok, False) is None
    assert _statuses(siem_backend) == {"pending": 2, "abandoned": 1}
    assert siem_backend.count("SELECT COUNT(*) AS n FROM siem_delivery_batches") == 0


def _insert_projection_error_row(
    b: SiemBackendHarness, event_uuid: str, dest: str, mapping_version: int = 1
) -> None:
    """A row whose projection failed under *mapping_version* (default: an
    OLD version, so a newer engine re-projects it)."""
    now = "2026-01-01T00:00:00.000Z" if b.name == "sqlite" else "2026-01-01T00:00:00Z"
    b.raw(
        "INSERT INTO siem_delivery_queue (event_uuid, destination_key, occurred_at, "
        "action_type, event_payload, projection_error, status, attempts, next_attempt_at, "
        "mapping_version, created_at) VALUES (?, ?, ?, 'authentication_success', NULL, "
        "'details.method', 'pending', 0, ?, ?, ?)",
        (event_uuid, dest, "2026-01-01T00:00:00.000000Z", now, mapping_version, now),
    )


def test_reprojection_recovers_rows_from_their_audit_rows(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    capture.reset_capture_state_for_tests()  # audit rows only, no capture
    events = [_login(i) for i in range(3)]
    siem_backend.audit.insert_events(events)
    for event in events:
        _insert_projection_error_row(
            siem_backend, event.event_uuid, engine.destination.key
        )
    v2 = dataclasses.replace(engine, mapping_version=2)
    _ticks(v2, 3)
    assert _statuses(siem_backend) == {"delivered": 3}
    assert len(siem_sidecar.control.get("/_control/received").json()["events"]) == 3


def test_aged_out_source_becomes_unrecoverable(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _insert_projection_error_row(
        siem_backend, "11111111-1111-1111-1111-111111111111", engine.destination.key
    )
    v2 = dataclasses.replace(engine, mapping_version=2)
    _ticks(v2, 3)
    assert _statuses(siem_backend) == {"unrecoverable": 1}
    assert _state(engine)["unrecoverable_total"] == 1
    assert _requests(siem_sidecar) == []


def test_mapping_change_requeues_quarantined_rows_once(
    engine: EngineContext, siem_backend: SiemBackendHarness
) -> None:
    _capture(siem_backend, 3)
    siem_backend.raw(
        "UPDATE siem_delivery_queue SET status = 'quarantined', mapping_version = 1, "
        "quarantine_reason = 'row_rejection'"
    )
    v2 = dataclasses.replace(engine, mapping_version=2)
    assert requeue_after_mapping_change(v2) == 3
    assert _statuses(siem_backend) == {"pending": 3}
    assert requeue_after_mapping_change(v2) == 0


def test_k1_unknown_outcome_resend_is_counted(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    _capture(siem_backend, 1)
    siem_sidecar.control.post("/_control/config", {"duplicate_mode": "ok_noop"})
    _fault(siem_sidecar, {"mode": "accept_then_drop"})
    _ticks(engine, 3)
    reqs = _requests(siem_sidecar)
    assert len(reqs) == 2 and reqs[1]["duplicate"] is True
    assert reqs[0]["body_sha256"] == reqs[1]["body_sha256"]
    st = _state(engine)
    assert st["resent_after_unknown_outcome"] == 1 and st["delivered_total"] == 1


def test_payload_of_a_delivered_event_has_no_secret(
    engine: EngineContext, siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    event = dataclasses.replace(
        _login(1),
        details_json=json.dumps({"method": "password", "note": "S3CR3T-M4RK"}),
    )
    siem_backend.audit.insert_events([event])
    run_tick(engine)
    raw = json.dumps(siem_sidecar.control.get("/_control/received").json())
    assert "S3CR3T-M4RK" not in raw
    stored = siem_backend.db.read(
        lambda tx: tx.query("SELECT * FROM siem_delivery_queue")
    )
    assert "S3CR3T-M4RK" not in json.dumps(stored, default=str)
