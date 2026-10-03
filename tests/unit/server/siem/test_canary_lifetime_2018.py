"""Bug #2018: a canary confirmation is valid only for the configuration
lifetime that produced it.

Disabling or clearing the destination, removing or replacing the
service-account credential, changing the trusted CA, or moving the
destination away invalidates it: re-enabling then needs a fresh canary plus
confirm.  Real ConfigService (the committed-config change path), real
scheduler, real SecOps sidecar, SQLite AND PostgreSQL.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.siem_delivery import admin, capture, state_store
from code_indexer.server.services.siem_delivery.scheduler import (
    SiemDeliveryScheduler,
)
from code_indexer.server.services.siem_delivery.trust import set_trusted_ca
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness
from .conftest import harness_section, seeded_store
from .test_admin_parity import _scheduler
from .tls_fixtures import make_ca

SECTION = "siem_delivery_config"
_FORM_FIELDS = (
    "harness_endpoint",
    "api_version",
    "project_id",
    "location",
    "instance_id",
    "source_instance_label",
)
CLEARED = {name: "" for name in _FORM_FIELDS if name != "api_version"}


@pytest.fixture()
def lifetime(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle, tmp_path: Path
) -> Iterator[Tuple[ConfigService, SiemDeliveryScheduler, SidecarHandle]]:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    svc = ConfigService(server_dir_path=str(server_dir))
    svc.load_config()
    if siem_backend.name == "postgres":
        svc.set_connection_pool(siem_backend.pool)
    else:
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = server_dir / "cidx_server.db"
        DatabaseSchema(str(db_path)).initialize_database()
        svc.initialize_runtime_db(str(db_path))
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    seeded_store(siem_backend.db, siem_sidecar)
    scheduler = _scheduler(siem_backend, svc)  # type: ignore[arg-type]
    scheduler.register_process()
    try:
        yield svc, scheduler, siem_sidecar
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _save(svc: ConfigService, values: Dict[str, Any]) -> None:
    svc.update_settings_atomic(
        [("siem_delivery", key, value) for key, value in values.items()]
    )


def _destination(sidecar: SidecarHandle, **overrides: Any) -> Dict[str, Any]:
    section = dataclasses.asdict(harness_section(sidecar))
    values = {name: section[name] for name in _FORM_FIELDS}
    values.update(overrides)
    return values


def _canary_and_confirm(scheduler: SiemDeliveryScheduler) -> None:
    canary = admin.run_canary(scheduler, "alice")
    outcome = admin.confirm_visible(
        scheduler, "alice", canary["canary_run_id"], canary["expected_product_log_ids"]
    )
    assert outcome["confirmed"]


def _armed(scheduler: SiemDeliveryScheduler) -> bool:
    scheduler.run_cycle()
    return bool(capture.capture_state().active)


def _status(scheduler: SiemDeliveryScheduler) -> str:
    state = state_store.read_state(scheduler.db)
    return str(admin.capture_status(scheduler, state)["state"])


def _arm_first_time(
    svc: ConfigService, scheduler: SiemDeliveryScheduler, sidecar: SidecarHandle
) -> None:
    _save(svc, {**_destination(sidecar), "enabled": "true"})
    scheduler.run_cycle()
    _canary_and_confirm(scheduler)
    assert _armed(scheduler)


def _key_json(sidecar: SidecarHandle) -> str:
    return json.dumps(dict(sidecar.read_key_file()))


Lifetime = Tuple[ConfigService, SiemDeliveryScheduler, SidecarHandle]


def test_first_time_flow_canary_while_disabled_then_enable_arms(
    lifetime: Lifetime,
) -> None:
    svc, scheduler, sidecar = lifetime
    _save(svc, {**_destination(sidecar), "enabled": "false"})
    scheduler.run_cycle()
    _canary_and_confirm(scheduler)  # a canary runs while disabled
    assert not _armed(scheduler)
    _save(svc, {"enabled": "true"})
    assert _armed(scheduler)


def test_decommission_then_reenable_same_destination_needs_a_new_canary(
    lifetime: Lifetime,
) -> None:
    svc, scheduler, sidecar = lifetime
    _arm_first_time(svc, scheduler, sidecar)
    _save(svc, {**CLEARED, "enabled": "false"})  # disable AND clear
    admin.remove_credential(scheduler, "alice")
    assert not _armed(scheduler)

    admin.set_credential(scheduler, "alice", _key_json(sidecar))
    _save(svc, {**_destination(sidecar), "enabled": "true"})  # SAME destination
    assert not _armed(scheduler), "re-armed from the old canary confirmation"
    assert _status(scheduler) == "awaiting canary"

    _canary_and_confirm(scheduler)
    assert _armed(scheduler)


def test_disable_then_reenable_needs_a_new_canary(lifetime: Lifetime) -> None:
    svc, scheduler, sidecar = lifetime
    _arm_first_time(svc, scheduler, sidecar)
    _save(svc, {"enabled": "false"})
    assert not _armed(scheduler)
    _save(svc, {"enabled": "true"})
    assert not _armed(scheduler)
    assert _status(scheduler) == "awaiting canary"
    _canary_and_confirm(scheduler)
    assert _armed(scheduler)


def test_credential_replaced_disarms_and_requires_a_new_canary(
    lifetime: Lifetime,
) -> None:
    svc, scheduler, sidecar = lifetime
    _arm_first_time(svc, scheduler, sidecar)
    admin.set_credential(scheduler, "alice", _key_json(sidecar))  # replaced
    assert state_store.read_state(scheduler.db)["armed_destination_key"] is None
    assert not _armed(scheduler)
    assert _status(scheduler) == "awaiting canary"
    _canary_and_confirm(scheduler)
    assert _armed(scheduler)


def test_trusted_ca_change_disarms_and_requires_a_new_canary(
    lifetime: Lifetime,
) -> None:
    svc, scheduler, sidecar = lifetime
    _arm_first_time(svc, scheduler, sidecar)
    set_trusted_ca(svc, "alice", make_ca("Example CA One").pem)
    assert not _armed(scheduler)
    assert _status(scheduler) == "awaiting canary"
    _canary_and_confirm(scheduler)
    assert _armed(scheduler)


def test_destination_moved_away_and_back_needs_a_new_canary(
    lifetime: Lifetime,
) -> None:
    svc, scheduler, sidecar = lifetime
    _arm_first_time(svc, scheduler, sidecar)
    _save(svc, {"instance_id": "example-other-instance"})  # retargeted away
    assert not _armed(scheduler)
    _save(svc, {"instance_id": _destination(sidecar)["instance_id"]})  # and back
    assert not _armed(scheduler), "re-armed from the old canary confirmation"
    _canary_and_confirm(scheduler)
    assert _armed(scheduler)


def test_a_change_outside_the_lifetime_keeps_arming(lifetime: Lifetime) -> None:
    svc, scheduler, sidecar = lifetime
    _arm_first_time(svc, scheduler, sidecar)
    epoch = svc.read_committed_section(SECTION)[1]["arming_epoch"]
    _save(svc, {"max_batch_events": "500", "source_instance_label": "example-two"})
    _save(svc, {"enabled": "true"})
    assert svc.read_committed_section(SECTION)[1]["arming_epoch"] == epoch
    assert _armed(scheduler)


def test_confirming_a_canary_of_an_earlier_lifetime_is_refused(
    lifetime: Lifetime,
) -> None:
    svc, scheduler, sidecar = lifetime
    _save(svc, {**_destination(sidecar), "enabled": "false"})
    scheduler.run_cycle()
    canary = admin.run_canary(scheduler, "alice")
    set_trusted_ca(svc, "alice", make_ca("Example CA Two").pem)  # new lifetime
    with pytest.raises(admin.SiemAdminError) as refused:
        admin.confirm_visible(
            scheduler,
            "alice",
            canary["canary_run_id"],
            canary["expected_product_log_ids"],
        )
    assert refused.value.status == 409


def test_canary_recorded_after_a_credential_change_is_refused(
    lifetime: Lifetime,
) -> None:
    """The canary is sent with the credential read BEFORE the send; a
    replacement committed meanwhile makes the record refuse it."""
    svc, scheduler, sidecar = lifetime
    _save(svc, {**_destination(sidecar), "enabled": "false"})
    scheduler.run_cycle()
    before = scheduler.credential_store.credential_id()
    scheduler.credential_store.set(dict(sidecar.read_key_file()), actor="bob")
    recorded = state_store.record_canary(
        scheduler.db,
        run_id="run-stale",
        destination_key="harness:0000000000000000",
        mapping_version=scheduler.mapping_version,
        expected=[],
        result="accepted",
        signature=None,
        actor="alice",
        config_epoch="",
        credential_id=before,
    )
    assert recorded is False
    assert state_store.read_state(scheduler.db)["canary_run_id"] is None


def test_sqlite_schema_adds_the_epoch_column_to_an_existing_state_table(
    tmp_path: Path,
) -> None:
    """An upgraded groups.db: the pre-#2018 state table gains the column
    (empty lifetime), keeping an armed row armed; re-running is a no-op."""
    import sqlite3

    from code_indexer.server.services.siem_delivery.db import ensure_sqlite_schema

    path = tmp_path / "groups.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE siem_delivery_state (id INTEGER PRIMARY KEY, "
            "armed_destination_key TEXT, canary_result TEXT)"
        )
        conn.execute(
            "INSERT INTO siem_delivery_state VALUES (1, 'harness:00aa', 'accepted')"
        )
        conn.commit()
        ensure_sqlite_schema(conn)
        ensure_sqlite_schema(conn)
        conn.commit()
        row = conn.execute(
            "SELECT armed_destination_key, canary_config_epoch "
            "FROM siem_delivery_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("harness:00aa", "")
