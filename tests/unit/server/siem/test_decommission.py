"""Decommissioning a SIEM destination: after delivery is disabled and its
configuration cleared, the rows still bound to the removed destination
(pending, and quarantined) are abandoned through the admin action, and the
SIEM health reasons clear.  SQLite AND PostgreSQL, real SecOps sidecar."""

from __future__ import annotations

import dataclasses
import types
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.config_change_audit import record_config_outcome
from code_indexer.server.services.siem_delivery import admin, capture, stats
from code_indexer.server.services.siem_delivery.admin import SiemAdminError
from code_indexer.server.services.siem_delivery.claim import (
    Claim,
    EngineContext,
    claim_batch,
)
from code_indexer.server.services.siem_delivery.classifier import (
    ACCEPTED,
    Classification,
)
from code_indexer.server.services.siem_delivery.completion import complete
from code_indexer.server.services.siem_delivery.health import siem_health_reasons
from code_indexer.server.services.siem_delivery.probe import run_tick
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness
from .conftest import harness_destination, harness_section, seeded_store
from .test_admin_parity import _arm, _CommittedConfig, _login, _scheduler
from .test_engine import _capture, engine  # noqa: F401  (fixture re-export)


@pytest.fixture()
def wired(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> Iterator[Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle]]:
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    config = _CommittedConfig(dataclasses.asdict(harness_section(siem_sidecar)))
    seeded_store(siem_backend.db, siem_sidecar)
    try:
        yield siem_backend, config, siem_sidecar
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _statuses(b: SiemBackendHarness, key: str) -> Dict[str, int]:
    rows = b.db.read(
        lambda tx: tx.query(
            "SELECT status, COUNT(*) AS n FROM siem_delivery_queue "
            "WHERE destination_key = ? GROUP BY status",
            (key,),
        )
    )
    return {str(r["status"]): int(r["n"]) for r in rows}


def _siem_reasons(scheduler: Any) -> list:
    stats.maybe_refresh_stats(scheduler.db, refresh_seconds=0.0, destination_key=None)
    scheduler.run_cycle()
    return [r for r in siem_health_reasons(scheduler.health_inputs(), None)]


def _disable_and_clear(config: _CommittedConfig, sidecar: SidecarHandle) -> None:
    """The clearing save: its config_changed row is captured for the
    REMOVED destination (record_config_outcome is what every audited save
    calls), then the committed section is the cleared one."""
    before = types.SimpleNamespace(siem_delivery_config=harness_section(sidecar))
    after = types.SimpleNamespace(siem_delivery_config=SiemDeliveryConfig())
    record_config_outcome(
        actor="alice",
        action_type="config_changed",
        target_id="siem_delivery",
        change_kind="update",
        before=before,
        after=after,
        outcome="success",
    )
    config.section = dataclasses.asdict(SiemDeliveryConfig())
    config.version += 1


def test_decommission_sequence_abandons_rows_of_a_removed_destination(
    wired: Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle],
) -> None:
    b, config, sidecar = wired
    old_key = harness_destination(sidecar).key
    scheduler = _scheduler(b, config)
    _arm(scheduler)  # 1. configure (and arm)
    run_tick_ctx = scheduler.engine_context()
    assert run_tick_ctx is not None
    run_tick(run_tick_ctx)  # drain the arming self-reports
    poison = _login(b)  # 2. capture
    sidecar.control.post(
        "/_control/faults", {"mode": "reject_if_contains", "product_log_id": poison}
    )
    run_tick(run_tick_ctx)  # 3. quarantine that one row
    assert _statuses(b, old_key).get("quarantined") == 1

    _disable_and_clear(config, sidecar)  # 4. disable and clear
    assert _statuses(b, old_key).get("pending", 0) >= 1
    reasons = _siem_reasons(scheduler)
    assert any(f"unconfigured destination {old_key}" in r for r in reasons), reasons

    result = admin.abandon_destination(scheduler, "alice", old_key)  # 5. abandon
    assert result["abandoned"] >= 2
    left = _statuses(b, old_key)
    assert not {"pending", "batched", "quarantined"} & set(left), left

    assert [r for r in _siem_reasons(scheduler) if "SIEM" in r] == []  # 6. health


class _ReconfiguredAfterFirstRead(_CommittedConfig):
    """Nothing configured at the abandon precheck; a concurrent save then
    configures the SAME destination again before the rows move."""

    def __init__(self, cleared: Dict[str, Any], restored: Dict[str, Any]) -> None:
        super().__init__(cleared)
        self.restored = restored
        self.reads = 0

    def read_committed_section(self, name: str) -> Tuple[int, Dict[str, Any]]:
        self.reads += 1
        if self.reads > 1:
            self.section, self.version = self.restored, 9
        return super().read_committed_section(name)


def test_abandon_stops_when_the_key_becomes_configured_during_the_move(
    wired: Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle],
) -> None:
    b, config, sidecar = wired
    old_key = harness_destination(sidecar).key
    scheduler = _scheduler(b, config)
    _arm(scheduler)
    _disable_and_clear(config, sidecar)  # leaves the clear's own pending row
    before = _statuses(b, old_key)
    assert before.get("pending", 0) >= 1
    racing = _ReconfiguredAfterFirstRead(
        dataclasses.asdict(SiemDeliveryConfig()),
        dataclasses.asdict(harness_section(sidecar)),
    )
    scheduler._config_service = racing
    with pytest.raises(SiemAdminError) as exc:
        admin.abandon_destination(scheduler, "alice", old_key)
    assert exc.value.status == 409
    assert _statuses(b, old_key) == before  # nothing of the configured key moved


class _CapturesDuringPrecheck(_CommittedConfig):
    """Nothing configured throughout; the FIRST committed read (abandon's
    precheck, after its id snapshot) coincides with a new capture for the
    key -- a row the operation must never touch."""

    def __init__(self, section: Dict[str, Any], capture: Any) -> None:
        super().__init__(section)
        self.capture: Any = capture

    def read_committed_section(self, name: str) -> Tuple[int, Dict[str, Any]]:
        if self.capture is not None:
            capture, self.capture = self.capture, None
            capture()
        return super().read_committed_section(name)


def _insert_pending(b: SiemBackendHarness, key: str, event_uuid: str) -> None:
    now = "2026-01-01T00:00:00.000Z" if b.name == "sqlite" else "2026-01-01T00:00:00Z"
    b.raw(
        "INSERT INTO siem_delivery_queue (event_uuid, destination_key, occurred_at, "
        "action_type, event_payload, status, attempts, next_attempt_at, "
        "mapping_version, created_at) VALUES (?, ?, ?, 'authentication_success', "
        "'{}', 'pending', 0, ?, 1, ?)",
        (event_uuid, key, now, now, now),
    )


def _status_of(b: SiemBackendHarness, event_uuid: str) -> str:
    row = b.db.read(
        lambda tx: tx.one(
            "SELECT status FROM siem_delivery_queue WHERE event_uuid = ?",
            (event_uuid,),
        )
    )
    assert row is not None
    return str(row["status"])


def test_rows_captured_after_abandon_starts_are_never_touched(
    wired: Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle],
) -> None:
    b, _config, sidecar = wired
    key = harness_destination(sidecar).key
    _insert_pending(b, key, "00000000-0000-0000-0000-00000000000a")
    late = "00000000-0000-0000-0000-00000000000b"
    racing = _CapturesDuringPrecheck(
        dataclasses.asdict(SiemDeliveryConfig()), lambda: _insert_pending(b, key, late)
    )
    scheduler = _scheduler(b, racing)
    result = admin.abandon_destination(scheduler, "alice", key)
    assert result["abandoned"] == 1
    assert _status_of(b, "00000000-0000-0000-0000-00000000000a") == "abandoned"
    assert _status_of(b, late) == "pending"


def test_abandon_refuses_while_a_send_is_in_flight(
    engine: EngineContext,  # noqa: F811
    siem_backend: SiemBackendHarness,
) -> None:
    """A batch leased by a sender (claims lease under the same state-row
    lock) is in flight: abandon must refuse rather than close it under the
    sender, which would lose the completion and strand its newer member."""
    b = siem_backend
    _capture(b, 1)  # below the snapshot
    claims: List[Claim] = []

    def _send_starts() -> None:
        _capture(b, 1)  # above the snapshot
        claim = claim_batch(engine)
        assert claim is not None
        claims.append(claim)

    racing = _CapturesDuringPrecheck(
        dataclasses.asdict(SiemDeliveryConfig()), _send_starts
    )
    scheduler = _scheduler(b, racing)
    with pytest.raises(SiemAdminError) as exc:
        admin.abandon_destination(scheduler, "alice", engine.destination.key)
    assert exc.value.status == 409 and "in flight" in exc.value.message
    accepted = Classification(ACCEPTED, "200|OK|accepted|")
    assert complete(engine, claims[0], accepted) is True
    assert _statuses(b, engine.destination.key) == {"delivered": 2}


def test_a_stale_process_cache_cannot_abandon_the_configured_destination(
    wired: Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle],
) -> None:
    """This process last applied ANOTHER destination (its cached view); the
    COMMITTED configuration has since made the abandoned key the configured
    one.  The committed read wins: 409, nothing moves."""
    b, config, sidecar = wired
    new_key = harness_destination(sidecar).key
    scheduler = _scheduler(b, config)
    config.section = dataclasses.asdict(
        dataclasses.replace(harness_section(sidecar), instance_id="old-instance")
    )
    scheduler.register_process()
    scheduler.run_cycle()  # the process cache now holds the OLD destination
    view = scheduler.view
    assert view is not None and view.destination is not None
    assert view.destination.key != new_key
    config.section = dataclasses.asdict(harness_section(sidecar))  # committed: NEW
    _insert_pending(b, new_key, "00000000-0000-0000-0000-00000000000c")
    with pytest.raises(SiemAdminError) as exc:
        admin.abandon_destination(scheduler, "alice", new_key)
    assert exc.value.status == 409
    assert _status_of(b, "00000000-0000-0000-0000-00000000000c") == "pending"


def test_abandon_with_nothing_configured_is_a_no_op_for_an_unknown_key(
    wired: Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle],
) -> None:
    """Nothing configured, nothing stored for the key: a no-op, not a 409."""
    b, config, _sidecar = wired
    config.section = dataclasses.asdict(SiemDeliveryConfig())
    scheduler = _scheduler(b, config)
    assert (
        admin.abandon_destination(scheduler, "alice", "harness:0000")["abandoned"] == 0
    )


def test_retarget_with_nothing_configured_refuses_clearly(
    wired: Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle],
) -> None:
    b, config, _sidecar = wired
    config.section = dataclasses.asdict(SiemDeliveryConfig())
    scheduler = _scheduler(b, config)
    with pytest.raises(SiemAdminError) as exc:
        admin.retarget_destination(scheduler, "alice", "harness:0000")
    assert exc.value.status == 409
    assert "no SIEM destination is configured to retarget to" in exc.value.message
    assert "abandon" in exc.value.message
