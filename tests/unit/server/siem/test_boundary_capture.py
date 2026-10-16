"""Configuration boundary events are captured from the committed change."""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Iterator, Optional

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.config_change_audit import record_config_outcome
from code_indexer.server.services.siem_delivery import capture
from code_indexer.server.services.siem_delivery.boundary import siem_boundary_target
from code_indexer.server.services.siem_delivery.capture import CaptureSnapshot
from code_indexer.server.services.siem_delivery.destination import destination_key
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

from .backends import SiemBackendHarness

A = SiemDeliveryConfig(
    enabled=True,
    region="us",
    project_id="example-project",
    location="us",
    instance_id="instance-a",
    source_instance_label="lbl",
)
B = dataclasses.replace(A, instance_id="instance-b")
EMPTY = SiemDeliveryConfig()


def _cfg(section: SiemDeliveryConfig) -> ServerConfig:
    config = ServerConfig(server_dir="/nonexistent-example")
    config.siem_delivery_config = copy.deepcopy(section)
    return config


@pytest.mark.parametrize(
    "before,after,change_kind,outcome,kind,dest",
    [
        (dataclasses.replace(A, enabled=False), A, "update", "success", "enable", A),
        (A, dataclasses.replace(A, enabled=False), "update", "success", "disable", A),
        (A, B, "update", "success", "destination_change", B),
        (A, EMPTY, "update", "success", "clear", A),
        (A, EMPTY, "reset_to_defaults", "success", "reset", A),
        (
            A,
            dataclasses.replace(A, max_batch_events=5),
            "update",
            "failure",
            "other_siem_change",
            A,
        ),
        (
            A,
            dataclasses.replace(A, max_batch_events=5),
            "update",
            "success",
            "other_siem_change",
            A,
        ),
    ],
)
def test_boundary_kind_and_destination(
    before: SiemDeliveryConfig,
    after: SiemDeliveryConfig,
    change_kind: str,
    outcome: str,
    kind: str,
    dest: SiemDeliveryConfig,
) -> None:
    target_id = "*" if change_kind == "reset_to_defaults" else "siem_delivery"
    details = {"changed_keys": ["siem_delivery_config.enabled"]}
    is_siem, target = siem_boundary_target(
        _cfg(before),
        _cfg(after),
        target_id=target_id,
        change_kind=change_kind,
        details=details,
        outcome=outcome,
    )
    assert is_siem
    assert target is not None
    assert target.boundary_kind == kind
    assert target.destination_key == destination_key(dest)


def test_no_destination_before_or_after_is_not_captured() -> None:
    is_siem, target = siem_boundary_target(
        _cfg(EMPTY),
        _cfg(dataclasses.replace(EMPTY, max_batch_events=7)),
        target_id="siem_delivery",
        change_kind="update",
        details={},
    )
    assert is_siem and target is None


def test_other_sections_are_not_siem() -> None:
    assert siem_boundary_target(
        _cfg(A), _cfg(A), target_id="server", change_kind="update", details={}
    ) == (False, None)


@pytest.fixture()
def bound_sink(siem_backend: SiemBackendHarness) -> Iterator[SiemBackendHarness]:
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    try:
        yield siem_backend
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _queue(b: SiemBackendHarness) -> Any:
    return b.db.read(
        lambda tx: tx.query(
            "SELECT action_type, destination_key, boundary_kind FROM siem_delivery_queue"
        )
    )


@pytest.mark.parametrize(
    "snapshot",
    [None, CaptureSnapshot(True, False, None, 0.0)],
    ids=["not_loaded", "loaded_inactive"],
)
def test_record_config_outcome_captures_the_enable_row_whatever_the_snapshot(
    bound_sink: SiemBackendHarness, snapshot: Optional[CaptureSnapshot]
) -> None:
    if snapshot is not None:
        capture.publish_capture_state(snapshot)
    record_config_outcome(
        actor="alice",
        action_type="config_changed",
        target_id="siem_delivery",
        change_kind="update",
        before=_cfg(dataclasses.replace(A, enabled=False)),
        after=_cfg(A),
        outcome="success",
    )
    assert _queue(bound_sink) == [
        {
            "action_type": "config_changed",
            "destination_key": destination_key(A),
            "boundary_kind": "enable",
        }
    ]


def test_non_siem_config_rows_are_not_captured(bound_sink: SiemBackendHarness) -> None:
    record_config_outcome(
        actor="alice",
        action_type="config_changed",
        target_id="server",
        change_kind="update",
        before=_cfg(A),
        after=_cfg(A),
        outcome="success",
    )
    assert _queue(bound_sink) == []
    assert capture.capture_failures_since_boot() == {}
