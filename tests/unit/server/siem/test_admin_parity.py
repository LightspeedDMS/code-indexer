"""Scheduler, arming and admin resolution on SQLite AND PostgreSQL, against
the real SecOps sidecar; plus redaction across every SIEM surface."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import build_event
from code_indexer.server.services.siem_delivery import admin, capture, state_store
from code_indexer.server.services.siem_delivery.probe import run_tick
from code_indexer.server.services.siem_delivery.scheduler import SiemDeliveryScheduler
from code_indexer.server.services.siem_delivery.sender import (
    CredentialProvider,
    ProbeResult,
)
from code_indexer.server.services.siem_delivery.timings import HARNESS_TIMINGS
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness
from .conftest import harness_destination, harness_section

MARKER = "S3CR3T-PARITY-MARKER-4d2"


class _CommittedConfig:
    """Committed SIEM section for the scheduler (the config collaborator)."""

    def __init__(self, section: Dict[str, Any]) -> None:
        self.version = 1
        self.section = section

    def read_committed_section(self, name: str) -> Tuple[int, Dict[str, Any]]:
        assert name == "siem_delivery_config"
        return self.version, dict(self.section)


class _NoJobs:
    def submit_job(self, operation_type: str, func: Any, **kwargs: Any) -> str:
        return "job"


def _scheduler(
    b: SiemBackendHarness, config: _CommittedConfig
) -> SiemDeliveryScheduler:
    return SiemDeliveryScheduler(
        db=b.db,
        config_service=config,
        background_job_manager=_NoJobs(),
        http_client_factory=HttpClientFactory(fault_injection_service=None),
        harness_active=True,
        node_id=None,
        timings=HARNESS_TIMINGS,
    )


@pytest.fixture()
def wired(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> Iterator[Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle]]:
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    config = _CommittedConfig(dataclasses.asdict(harness_section(siem_sidecar)))
    try:
        yield siem_backend, config, siem_sidecar
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _arm(scheduler: SiemDeliveryScheduler) -> None:
    scheduler.register_process()
    scheduler.run_cycle()
    canary = admin.run_canary(scheduler, "alice")
    admin.confirm_visible(
        scheduler, "alice", canary["canary_run_id"], canary["expected_product_log_ids"]
    )
    scheduler.run_cycle()
    assert capture.capture_state().active


def _login(b: SiemBackendHarness, actor: str = "alice") -> str:
    event = build_event(
        actor=actor,
        action_type="authentication_failure",
        target_type="auth",
        target_id=actor,
        outcome="failure",
        details={
            "method": "password",
            "stage": "credentials",
            "reason": "bad_credentials",
        },
    )
    b.audit.insert_events([event])
    return event.event_uuid


def _halt_on_duplicate(
    scheduler: SiemDeliveryScheduler, b: SiemBackendHarness, sidecar: SidecarHandle
) -> str:
    ctx = scheduler.engine_context()
    assert ctx is not None
    run_tick(ctx)
    _login(b)
    sidecar.control.post("/_control/faults", {"mode": "status", "code": 409})
    run_tick(ctx)
    state = state_store.read_state(b.db)
    assert state["halted_class"] == "duplicate_response"
    return str(state["halted_batch_id"])


def test_arming_and_acknowledge_parity(wired: Tuple[Any, ...]) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    _arm(scheduler)
    batch_id = _halt_on_duplicate(scheduler, b, sidecar)
    assert admin.acknowledge_batch(scheduler, "alice", batch_id)["event_count"] == 1
    assert state_store.read_state(b.db)["halted_class"] is None
    doc = admin.stats_document(scheduler)
    assert doc["capture"]["state"] == "armed" and doc["halt"]["class"] is None


def test_rebatch_parity(wired: Tuple[Any, ...]) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    _arm(scheduler)
    batch_id = _halt_on_duplicate(scheduler, b, sidecar)
    admin.rebatch_batch(scheduler, "alice", batch_id)
    ctx = scheduler.engine_context()
    assert ctx is not None
    run_tick(ctx)
    row = b.db.read(
        lambda tx: tx.one(
            "SELECT COUNT(*) AS n FROM siem_delivery_queue WHERE delivered_via = 'accepted' "
            "AND action_type = 'authentication_failure'"
        )
    )
    assert int(row["n"]) == 1


def test_concurrent_arming_statements_arm_exactly_once(wired: Tuple[Any, ...]) -> None:
    b, config, sidecar = wired
    first, second = _scheduler(b, config), _scheduler(b, config)
    first.register_process()
    second.register_process()
    first.run_cycle()
    second.run_cycle()
    canary = admin.run_canary(first, "alice")
    admin.confirm_visible(
        first, "alice", canary["canary_run_id"], canary["expected_product_log_ids"]
    )
    dest = harness_destination(sidecar)
    results: List[Dict[str, Any]] = []
    barrier = threading.Barrier(2)

    def _race() -> None:
        barrier.wait()
        results.append(
            state_store.fence_and_arm(
                b.db,
                version=1,
                enabled=True,
                destination_key=dest.key,
                mapping_version=first.mapping_version,
                probe_fresh_seconds=600,
            )
        )

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2
    assert {r["armed_destination_key"] for r in results} == {dest.key}
    assert len({str(r["armed_at"]) for r in results}) == 1


def test_untrusted_values_never_reach_any_siem_surface(
    wired: Tuple[Any, ...], caplog: pytest.LogCaptureFixture
) -> None:
    """A failed login for an unknown account ("(unknown)": a password typed
    as a username is never stored) whose client-supplied correlation id and
    an extra details key carry a marker: neither is projected."""
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    _arm(scheduler)
    event = build_event(
        actor="(unknown)",
        action_type="authentication_failure",
        target_type="auth",
        target_id="(unknown)",
        outcome="failure",
        details={
            "method": "password",
            "stage": "credentials",
            "reason": "bad_credentials",
        },
    )
    event = dataclasses.replace(
        event,
        correlation_id=f"client header {MARKER}",
        details_json=json.dumps({"method": "password", "typed_password": MARKER}),
    )
    with caplog.at_level(logging.DEBUG):
        b.audit.insert_events([event])
        ctx = scheduler.engine_context()
        assert ctx is not None
        run_tick(ctx)
    received = sidecar.control.get(
        "/_control/search", {"product_log_id": event.event_uuid}
    ).json()["results"]
    assert len(received) == 1
    udm = received[0]["udm"]
    assert udm["additional"]["principal_unknown"] is True
    assert "user" not in udm["principal"]
    queue = b.db.read(lambda tx: tx.query("SELECT * FROM siem_delivery_queue"))
    batches = b.db.read(lambda tx: tx.query("SELECT * FROM siem_delivery_batches"))
    surfaces = [
        json.dumps(sidecar.control.get("/_control/received").json()),
        json.dumps(queue, default=str),
        json.dumps(batches, default=str),
        json.dumps(admin.stats_document(scheduler), default=str),
        caplog.text,
    ]
    assert all(MARKER not in s for s in surfaces)


def test_token_echo_marker_never_reaches_results_or_logs(
    wired: Tuple[Any, ...], caplog: pytest.LogCaptureFixture
) -> None:
    b, config, sidecar = wired
    sidecar.control.post("/_control/token-faults", {"mode": "echo", "marker": MARKER})
    factory = HttpClientFactory(fault_injection_service=None)
    with caplog.at_level(logging.DEBUG):
        result = CredentialProvider(factory, token_timeout=5.0).probe(
            harness_destination(sidecar)
        )
    assert result is ProbeResult.TOKEN_REJECTED
    assert MARKER not in caplog.text
    scheduler = _scheduler(b, config)
    scheduler.register_process()
    scheduler.run_cycle()
    dumped = json.dumps(admin.stats_document(scheduler), default=str)
    assert MARKER not in dumped
    key_material = sidecar.read_key_file()["private_key"]
    assert key_material.splitlines()[1] not in dumped
