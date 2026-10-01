"""SiemDeliveryScheduler and admin actions against a real ConfigService
(SQLite runtime DB), a real groups.db store and the real SecOps sidecar."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import build_event
from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.siem_delivery import admin, capture, state_store
from code_indexer.server.services.siem_delivery.admin import SiemAdminError
from code_indexer.server.services.siem_delivery.db import SiemDb
from code_indexer.server.services.siem_delivery.health import (
    AlertSignals,
    siem_health_reasons,
)
from code_indexer.server.services.siem_delivery.probe import run_tick
from code_indexer.server.services.siem_delivery.scheduler import SiemDeliveryScheduler
from code_indexer.server.services.siem_delivery.timings import HARNESS_TIMINGS
from code_indexer.server.storage.database_manager import DatabaseSchema
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .conftest import harness_section, seeded_store


class _NoJobs:
    """Background job manager double: records submissions, runs nothing."""

    def __init__(self, raise_duplicate: bool = False) -> None:
        self.submitted: List[str] = []
        self.funcs: List[Any] = []
        self.raise_duplicate = raise_duplicate

    def submit_job(self, operation_type: str, func: Any, **kwargs: Any) -> str:
        if self.raise_duplicate:
            from code_indexer.server.repositories.background_jobs import (
                DuplicateJobError,
            )

            raise DuplicateJobError(operation_type, "siem-delivery", "job-1")
        self.submitted.append(operation_type)
        self.funcs.append(func)
        return "job-1"


def _config_service(server_dir: Path) -> ConfigService:
    db_path = server_dir / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    svc = ConfigService(server_dir_path=str(server_dir))
    svc.load_config()
    svc.initialize_runtime_db(str(db_path))
    return svc


def _save(svc: ConfigService, **fields: Any) -> None:
    svc.update_settings_atomic(
        [("siem_delivery", k, str(v)) for k, v in fields.items()]
    )


def _harness_fields(sidecar: SidecarHandle, **overrides: Any) -> Dict[str, Any]:
    section = harness_section(sidecar, **overrides)
    return {
        "enabled": "true" if section.enabled else "false",
        "harness_endpoint": section.harness_endpoint,
        "api_version": section.api_version,
        "project_id": section.project_id,
        "location": section.location,
        "instance_id": section.instance_id,
        "source_instance_label": section.source_instance_label,
    }


@pytest.fixture()
def env(tmp_path: Path, siem_sidecar: SidecarHandle) -> Iterator[Tuple[Any, ...]]:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    svc = _config_service(server_dir)
    groups = tmp_path / "groups.db"
    audit = AuditLogService(groups)
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(audit, node_id=None)
    jobs = _NoJobs()
    db = SiemDb.sqlite(str(groups))
    scheduler = SiemDeliveryScheduler(
        db=db,
        config_service=svc,
        background_job_manager=jobs,
        http_client_factory=HttpClientFactory(fault_injection_service=None),
        harness_active=True,
        node_id=None,
        credential_store=seeded_store(db, siem_sidecar),
        timings=HARNESS_TIMINGS,
    )
    try:
        yield scheduler, svc, audit, jobs, siem_sidecar
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _login(audit: AuditLogService) -> str:
    event = build_event(
        actor="alice",
        action_type="authentication_success",
        target_type="auth",
        target_id="alice",
        outcome="success",
        details={"method": "password", "mfa": "not_enrolled", "flow": "rest_token"},
    )
    audit.insert_events([event])
    return event.event_uuid


def _queue_count(scheduler: SiemDeliveryScheduler, action_type: str) -> int:
    row = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT COUNT(*) AS n FROM siem_delivery_queue WHERE action_type = ?",
            (action_type,),
        )
    )
    return int(row["n"]) if row else 0


def _arm(
    scheduler: SiemDeliveryScheduler, svc: ConfigService, sidecar: SidecarHandle
) -> None:
    _save(svc, **_harness_fields(sidecar))
    scheduler.register_process()
    scheduler.run_cycle()
    canary = admin.run_canary(scheduler, "alice")
    assert canary["result"] == "accepted"
    admin.confirm_visible(
        scheduler, "alice", canary["canary_run_id"], canary["expected_product_log_ids"]
    )
    scheduler.run_cycle()


def test_tick_jobs_are_hidden_from_the_dashboard() -> None:
    """One tick job per busy cycle must not flood the recent-jobs panel: it
    is hidden the way x-ray search jobs are (both dashboard paths pass this
    list as exclude_operation_types)."""
    from code_indexer.server.services import dashboard_service

    hidden = dashboard_service._DASHBOARD_HIDDEN_OPERATION_TYPES
    assert SiemDeliveryScheduler.OPERATION_TYPE in hidden


def test_start_requires_the_registration_barrier(env: Tuple[Any, ...]) -> None:
    scheduler = env[0]
    with pytest.raises(RuntimeError):
        scheduler.start()
    scheduler.register_process()
    live = state_store.live_processes(scheduler.db)
    assert [p["process_id"] for p in live] == [scheduler.process_id]
    assert live[0]["probe_result"] == "pending"


def test_a_loop_outliving_stop_never_reinserts_its_process_row(
    env: Tuple[Any, ...],
) -> None:
    """stop() joins with a timeout; if the loop is still mid-cycle when the
    shutdown deregisters the process, that cycle must not register it again."""
    scheduler, svc, _audit, _jobs, sidecar = env
    _save(svc, **_harness_fields(sidecar))
    scheduler.register_process()
    scheduler.run_cycle()
    scheduler.deregister_process()
    assert state_store.live_processes(scheduler.db) == []
    scheduler.run_cycle()  # the loop that stop() could not join
    assert state_store.live_processes(scheduler.db) == []


def test_no_capture_before_the_canary(env: Tuple[Any, ...]) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _save(svc, **_harness_fields(sidecar))
    scheduler.register_process()
    scheduler.run_cycle()
    assert capture.capture_state().loaded and not capture.capture_state().active
    _login(audit)
    assert _queue_count(scheduler, "authentication_success") == 0
    assert admin.stats_document(scheduler)["capture"]["state"] == "awaiting canary"


def test_canary_confirmation_arms_and_next_login_is_captured(
    env: Tuple[Any, ...],
) -> None:
    scheduler, svc, audit, jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    assert capture.capture_state().active
    doc = admin.stats_document(scheduler)
    assert doc["capture"]["state"] == "armed"
    _login(audit)
    assert _queue_count(scheduler, "authentication_success") == 1
    requests = sidecar.control.get("/_control/requests").json()["requests"]
    assert len([r for r in requests if r["event_count"] > 2]) == 1  # one canary send
    before = len(jobs.submitted)
    scheduler.run_cycle()
    assert len(jobs.submitted) == before + 1  # due work -> one tick job


def test_mapping_change_requeues_even_when_only_quarantined_rows_remain(
    env: Tuple[Any, ...],
) -> None:
    scheduler, svc, audit, jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    scheduler.db.write(
        lambda tx: tx.execute("UPDATE siem_delivery_queue SET status = 'delivered'")
    )
    uuid = _login(audit)
    scheduler.db.write(
        lambda tx: tx.execute(
            "UPDATE siem_delivery_queue SET status = 'quarantined', "
            "quarantine_reason = 'row_rejection', mapping_version = 0 "
            "WHERE event_uuid = ?",
            (uuid,),
        )
    )
    jobs.submitted.clear()
    jobs.funcs.clear()
    scheduler.run_cycle()  # nothing is due: only a quarantined row remains
    assert jobs.submitted == [SiemDeliveryScheduler.OPERATION_TYPE]
    jobs.funcs[0]()
    row = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT status FROM siem_delivery_queue WHERE event_uuid = ?", (uuid,)
        )
    )
    assert row["status"] in ("pending", "batched", "delivered")
    assert "siem_quarantine_requeued" in _audit_types(audit)
    state = state_store.read_state(scheduler.db)
    assert int(state["requeued_mapping_version"]) == scheduler.mapping_version
    jobs.submitted.clear()
    scheduler.db.write(
        lambda tx: tx.execute("UPDATE siem_delivery_queue SET status = 'delivered'")
    )
    scheduler.run_cycle()  # requeue done and nothing due: no further tick
    assert jobs.submitted == []


def test_admin_actions_see_a_destination_saved_a_moment_ago(
    env: Tuple[Any, ...],
) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    scheduler.register_process()
    scheduler.run_cycle()  # loop view: no destination yet
    _save(svc, **_harness_fields(sidecar))
    assert admin.run_canary(scheduler, "alice")["result"] == "accepted"


def test_partial_visibility_reports_n_of_m_and_never_arms(env: Tuple[Any, ...]) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    _save(svc, **_harness_fields(sidecar))
    scheduler.register_process()
    scheduler.run_cycle()
    canary = admin.run_canary(scheduler, "alice")
    ids = canary["expected_product_log_ids"]
    result = admin.confirm_visible(scheduler, "alice", canary["canary_run_id"], ids[1:])
    assert not result["confirmed"]
    scheduler.run_cycle()
    status = admin.stats_document(scheduler)["capture"]["status"]
    assert f"{len(ids) - 1} of {len(ids)} visible" in status
    with pytest.raises(SiemAdminError) as exc:
        admin.confirm_visible(scheduler, "alice", "stale-run", ids)
    assert exc.value.status == 409


def test_rejected_canary_never_arms(env: Tuple[Any, ...]) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    _save(svc, **_harness_fields(sidecar))
    scheduler.register_process()
    scheduler.run_cycle()
    sidecar.control.post("/_control/faults", {"mode": "reject_request"})
    canary = admin.run_canary(scheduler, "alice")
    assert canary["result"] == "rejected" and canary["result_signature"]
    with pytest.raises(SiemAdminError):
        admin.confirm_visible(
            scheduler,
            "alice",
            canary["canary_run_id"],
            canary["expected_product_log_ids"],
        )
    scheduler.run_cycle()
    assert not capture.capture_state().active
    assert admin.stats_document(scheduler)["capture"]["state"] == "canary rejected"


def test_disable_by_another_worker_is_seen_through_the_committed_read(
    env: Tuple[Any, ...], tmp_path: Path
) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    other_worker = ConfigService(server_dir_path=str(tmp_path / "server"))
    other_worker.load_config()
    other_worker.initialize_runtime_db(str(tmp_path / "server" / "cidx_server.db"))
    _save(other_worker, enabled="false")
    assert svc.get_config().siem_delivery_config.enabled is True  # type: ignore[union-attr]
    scheduler.run_cycle()
    assert not capture.capture_state().active
    _login(audit)
    assert _queue_count(scheduler, "authentication_success") == 0


def test_config_never_loaded_and_last_known_good(
    env: Tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    _save(svc, **_harness_fields(sidecar))
    scheduler.register_process()

    def _boom(section: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "read_committed_section", _boom)
    scheduler.run_cycle()
    assert not capture.capture_state().loaded
    reasons = siem_health_reasons(scheduler.health_inputs(), None)
    assert any("never loaded" in r for r in reasons)
    monkeypatch.undo()
    scheduler.run_cycle()
    monkeypatch.setattr(svc, "read_committed_section", _boom)
    scheduler.run_cycle()
    assert capture.capture_state().loaded
    reasons = siem_health_reasons(scheduler.health_inputs(), None)
    assert any("last-known-good" in r for r in reasons)


def test_stored_harness_endpoint_is_inert_without_the_gate(
    env: Tuple[Any, ...],
) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    _save(svc, **_harness_fields(sidecar))
    gateless = SiemDeliveryScheduler(
        db=scheduler.db,
        config_service=svc,
        background_job_manager=_NoJobs(),
        http_client_factory=HttpClientFactory(fault_injection_service=None),
        harness_active=False,
        node_id=None,
        credential_store=scheduler.credential_store,
        timings=HARNESS_TIMINGS,
    )
    gateless.register_process()
    gateless.run_cycle()
    assert not capture.capture_state().loaded
    assert sidecar.control.get("/_control/requests").json()["requests"] == []
    with pytest.raises(SiemAdminError):
        admin.run_canary(gateless, "alice")


def test_duplicate_tick_is_benign(env: Tuple[Any, ...]) -> None:
    scheduler = env[0]
    scheduler._bgm = _NoJobs(raise_duplicate=True)
    assert scheduler.trigger_now() is None


def _halt_duplicate(
    scheduler: SiemDeliveryScheduler, audit: AuditLogService, sidecar: SidecarHandle
) -> str:
    ctx = scheduler.engine_context()
    assert ctx is not None
    run_tick(ctx)  # drain the config-change and canary self-report rows first
    _login(audit)
    sidecar.control.post("/_control/faults", {"mode": "status", "code": 409})
    run_tick(ctx)
    state = state_store.read_state(scheduler.db)
    assert state["halted_class"] == "duplicate_response"
    return str(state["halted_batch_id"])


def _audit_types(audit: AuditLogService) -> List[str]:
    conn = audit._conn_manager.get_connection()
    rows = conn.execute(
        "SELECT action_type FROM audit_logs WHERE action_type LIKE 'siem_%'"
    ).fetchall()
    return [r[0] for r in rows]


def test_acknowledge_resolves_a_duplicate_halt_and_is_audited(
    env: Tuple[Any, ...],
) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    batch_id = _halt_duplicate(scheduler, audit, sidecar)
    with pytest.raises(SiemAdminError):
        admin.acknowledge_batch(scheduler, "alice", "not-the-batch")
    assert admin.acknowledge_batch(scheduler, "alice", batch_id)["event_count"] == 1
    state = state_store.read_state(scheduler.db)
    assert state["halted_class"] is None
    row = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT delivered_via FROM siem_delivery_queue WHERE batch_id = ?",
            (batch_id,),
        )
    )
    assert row["delivered_via"] == "admin_acknowledged"
    assert "siem_batch_acknowledged" in _audit_types(audit)
    # the self-report row itself was captured for SIEM delivery
    assert _queue_count(scheduler, "siem_batch_acknowledged") == 1


def test_rebatch_resends_in_new_bodies(env: Tuple[Any, ...]) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    batch_id = _halt_duplicate(scheduler, audit, sidecar)
    admin.rebatch_batch(scheduler, "alice", batch_id)
    ctx = scheduler.engine_context()
    assert ctx is not None
    run_tick(ctx)
    delivered = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT COUNT(*) AS n FROM siem_delivery_queue WHERE action_type = "
            "'authentication_success' AND delivered_via = 'accepted'"
        )
    )
    assert delivered["n"] == 1
    assert "siem_batch_rebatched" in _audit_types(audit)


def test_resume_clears_any_halt_and_is_audited(env: Tuple[Any, ...]) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    _halt_duplicate(scheduler, audit, sidecar)
    assert admin.resume(scheduler, "alice")["halted_class"] == "duplicate_response"
    assert admin.resume(scheduler, "alice") == {"resumed": False}
    assert "siem_delivery_resumed" in _audit_types(audit)


def test_destination_change_never_reroutes_until_retarget(env: Tuple[Any, ...]) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    old_key = scheduler.engine_context().destination.key  # type: ignore[union-attr]
    sidecar.control.post("/_control/outage", {"mode": "refuse"})
    try:
        _login(audit)
        _save(svc, instance_id="instance-b")
        scheduler.run_cycle()
    finally:
        sidecar.control.post("/_control/outage", {"mode": "end"})
    pending = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT COUNT(*) AS n FROM siem_delivery_queue WHERE destination_key = ? "
            "AND status = 'pending'",
            (old_key,),
        )
    )
    assert pending["n"] >= 1
    result = admin.retarget_destination(scheduler, "alice", old_key)
    assert result["retargeted"] >= 1
    assert "siem_destination_retargeted" in _audit_types(audit)
    assert admin.abandon_destination(scheduler, "alice", old_key)["abandoned"] == 0


def test_abandon_refuses_the_configured_destination(env: Tuple[Any, ...]) -> None:
    scheduler, svc, audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    key = scheduler.engine_context().destination.key  # type: ignore[union-attr]
    _login(audit)
    with pytest.raises(SiemAdminError) as exc:
        admin.abandon_destination(scheduler, "alice", key)
    assert exc.value.status == 409
    assert _queue_count(scheduler, "authentication_success") == 1
    row = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT COUNT(*) AS n FROM siem_delivery_queue WHERE status = 'abandoned'"
        )
    )
    assert row["n"] == 0


@pytest.mark.parametrize("action", ["retarget", "abandon"])
def test_batches_formed_during_the_move_are_closed(
    env: Tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    _arm(scheduler, svc, sidecar)
    old_key = "harness:00000000000000aa"
    real_move = admin._move_rows
    injected: List[bool] = []

    def _move_then_stale_claim(*a: Any, **k: Any) -> int:
        moved = real_move(*a, **k)
        if injected:
            return moved
        injected.append(True)
        # a stale tick still holding the old view forms a batch meanwhile
        scheduler.db.write(
            lambda tx: tx.execute(
                "INSERT INTO siem_delivery_batches (batch_id, destination_key, body, "
                "body_sha256, event_count, mapping_version, state, created_at, "
                "next_attempt_at) VALUES ('late-batch', ?, X'7B7D', 'h', 1, 1, "
                "'pending_send', ?, ?)",
                (old_key, tx.ts(tx.now()), tx.ts(tx.now())),
            )
        )
        return moved

    monkeypatch.setattr(admin, "_move_rows", _move_then_stale_claim)
    if action == "retarget":
        admin.retarget_destination(scheduler, "alice", old_key)
    else:
        admin.abandon_destination(scheduler, "alice", old_key)
    row = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT state FROM siem_delivery_batches WHERE batch_id = 'late-batch'"
        )
    )
    assert row["state"] == "retargeted"


def test_health_reasons_are_degraded_only_and_quiet_when_disabled(
    env: Tuple[Any, ...],
) -> None:
    scheduler, svc, _audit, _jobs, sidecar = env
    scheduler.register_process()
    scheduler.run_cycle()
    assert siem_health_reasons(scheduler.health_inputs(), None) == []
    assert siem_health_reasons(None, "RuntimeError") == [
        "SIEM delivery not running in this process: RuntimeError"
    ]


def test_alert_signal_logs_once_per_window_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = {"now": 0.0}
    alerts = AlertSignals(HARNESS_TIMINGS, clock=lambda: clock["now"])
    failing: Dict[str, Any] = {"state": {}, "capture_failures": {"insert_failed": 1}}
    with caplog.at_level(logging.INFO):
        alerts.evaluate(failing)
        clock["now"] = 10.0
        alerts.evaluate(failing)
        clock["now"] = HARNESS_TIMINGS.alert_repeat_seconds + 1
        alerts.evaluate(failing)
        alerts.evaluate({"state": {}, "capture_failures": {}})
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    infos = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(errors) == 2 and len(infos) == 1
