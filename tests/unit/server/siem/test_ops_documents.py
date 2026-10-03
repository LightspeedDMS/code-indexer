"""The operator documents behind the Web arming and recovery panels (SQLite
AND PostgreSQL, real SecOps sidecar): the scheduler lookup shared by both
doors, the abandon confirmation word, and the committed-config view."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, Tuple

import pytest

from code_indexer.server.services.siem_delivery import admin, ops_documents
from code_indexer.server.services.siem_delivery.admin import SiemAdminError

from . import test_admin_parity as _parity
from .conftest import harness_destination
from .test_ops_pages import _destination, _seed_batch, _seed_queue

wired = _parity.wired  # the shared scheduler + sidecar fixture
_scheduler = _parity._scheduler


def test_find_scheduler_returns_never_raises() -> None:
    marker = object()
    present = SimpleNamespace(siem_delivery_scheduler=marker)
    assert admin.find_scheduler(present) == (marker, None)
    absent = SimpleNamespace(
        siem_delivery_scheduler=None, siem_delivery_startup_error="RuntimeError"
    )
    assert admin.find_scheduler(absent) == (None, "RuntimeError")
    assert admin.find_scheduler(SimpleNamespace()) == (None, None)


@pytest.mark.parametrize(
    "value", ["abandon", " ABANDON", "ABANDON\n", "ABANDON ", "", None, b"ABANDON"]
)
def test_anything_but_the_exact_word_is_refused(value: Any) -> None:
    with pytest.raises(SiemAdminError) as exc:
        admin.require_abandon_confirmation(value)
    assert (exc.value.status, exc.value.message) == (400, "type ABANDON to confirm")


def test_the_exact_word_confirms() -> None:
    admin.require_abandon_confirmation("ABANDON")  # raises on anything else


def test_committed_view_reads_the_committed_config_now(
    wired: Tuple[Any, ...],
) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    scheduler.register_process()
    scheduler.run_cycle()
    before = harness_destination(sidecar).key
    config.version = 2
    config.section = {**config.section, "instance_id": "instance-new"}
    view = scheduler.committed_view()
    assert view.version == 2 and view.destination is not None
    assert view.destination.key != before
    assert view.section.instance_id == "instance-new"
    assert scheduler.view is not None and scheduler.view.version == 1
    ctx = scheduler.committed_context()
    assert ctx is not None and ctx.destination.key == view.destination.key
    assert dataclasses.is_dataclass(view)


def test_arming_document_walks_to_armed(wired: Tuple[Any, ...]) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    scheduler.register_process()
    scheduler.run_cycle()
    key = harness_destination(sidecar).key
    doc = ops_documents.arming_document(scheduler)
    assert doc["committed"] == {
        "config_version": 1,
        "enabled": True,
        "destination_key": key,
        "mapping_version": scheduler.mapping_version,
    }
    assert doc["config_error"] is None and doc["credential"] is not None
    assert doc["canary"]["run_id"] is None and doc["canary"]["expected"] == []
    assert doc["canary_matches"] is False and doc["armed"] is False
    assert doc["readiness"]["total_live"] == 1
    assert doc["local_process"]["liveness"]["scope"] == "this_process"

    run = admin.run_canary(scheduler, "alice")
    doc = ops_documents.arming_document(scheduler)
    assert doc["canary_event_count"] == run["event_count"]
    expected = doc["canary"]["expected"]
    assert doc["canary"]["run_id"] == run["canary_run_id"]
    assert [e["product_log_id"] for e in expected] == run["expected_product_log_ids"]
    assert {"action_type", "event_type"} <= set(expected[0])
    assert not any(e["confirmed"] for e in expected)
    assert doc["canary_matches"] is True and doc["canary"]["actor"] == "alice"
    assert doc["canary"]["sent_at"] and doc["canary"]["result"] == "accepted"

    hidden = expected[-1]
    ids = [e["product_log_id"] for e in expected[:-1]]
    admin.confirm_visible(scheduler, "alice", run["canary_run_id"], ids)
    doc = ops_documents.arming_document(scheduler)
    assert len(doc["canary"]["confirmed_ids"]) == len(ids)
    assert doc["canary"]["missing_action_types"] == [hidden["action_type"]]
    assert doc["armed"] is False

    admin.confirm_visible(
        scheduler, "bob", run["canary_run_id"], ids + [hidden["product_log_id"]]
    )
    scheduler.run_cycle()
    doc = ops_documents.arming_document(scheduler)
    assert doc["armed"] is True and doc["armed_at"]
    assert doc["canary"]["visible_confirmed_by"] == "bob"
    assert doc["canary"]["missing_action_types"] == []
    assert doc["readiness"]["all_ready"] is True


def test_arming_document_reads_the_committed_config(wired: Tuple[Any, ...]) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    scheduler.register_process()
    scheduler.run_cycle()
    old_key = harness_destination(sidecar).key
    config.version = 2
    config.section = {**config.section, "instance_id": "instance-new"}
    doc = ops_documents.arming_document(scheduler)  # no cycle in between
    assert doc["committed"]["config_version"] == 2
    assert doc["committed"]["destination_key"] != old_key
    assert doc["canary_matches"] is False and doc["armed"] is False
    assert scheduler.view is not None and scheduler.view.destination is not None
    assert scheduler.view.destination.key == old_key  # the per-process view


def test_arming_document_reports_an_invalid_configuration(
    wired: Tuple[Any, ...],
) -> None:
    b, config, _sidecar = wired
    config.section = {**config.section, "harness_endpoint": "", "region": "nowhere"}
    scheduler = _scheduler(b, config)
    doc = ops_documents.arming_document(scheduler)
    assert doc["config_error"] == "region"
    assert doc["committed"] is None and doc["readiness"] is None
    assert doc["armed"] is False


def _halt_on(b: Any, batch_id: str) -> None:
    b.raw(
        "UPDATE siem_delivery_state SET halted_class = 'duplicate_response', "
        "halted_signature = '409|ALREADY_EXISTS|duplicate_response|-', "
        "halted_batch_id = ? WHERE id = 1",
        (batch_id,),
    )


def test_recovery_document_always_shows_the_halted_batch(
    wired: Tuple[Any, ...],
) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    key = harness_destination(sidecar).key
    for i in range(51):
        _seed_batch(b, f"batch-{i:03d}", seconds=i, dest=key)
    _halt_on(b, "batch-050")  # sorts LAST: outside the first page
    _destination(b, key)
    _seed_queue(b, 2, dest=key)
    doc = ops_documents.recovery_document(scheduler, "", "", "")
    assert doc["halt"]["class"] == "duplicate_response"
    assert doc["halted_batch"]["batch_id"] == "batch-050"
    assert doc["halted_batch"]["event_count"] == 3
    assert doc["open_batches"]["has_more"] is True
    assert "batch-050" not in [r["batch_id"] for r in doc["open_batches"]["rows"]]
    assert doc["configured_key"] == key and doc["stranded"]["rows"] == []
    more = ops_documents.recovery_document(
        scheduler, "", "", doc["open_batches"]["next_after"]
    )
    assert [r["batch_id"] for r in more["open_batches"]["rows"]] == ["batch-050"]


@pytest.mark.parametrize(
    "cursors",
    [
        ("x", "", ""),
        ("-1", "", ""),
        ("", "", "garbage"),
        (str(2**63), "", ""),  # just past the signed 64-bit id range
        ("9" * 19, "", ""),
    ],
)
def test_recovery_document_refuses_malformed_cursors(
    wired: Tuple[Any, ...], cursors: Tuple[str, str, str]
) -> None:
    b, config, _sidecar = wired
    with pytest.raises(SiemAdminError) as exc:
        ops_documents.recovery_document(_scheduler(b, config), *cursors)
    assert exc.value.status == 400


def test_destination_document_names_the_key_and_the_configured_destination(
    wired: Tuple[Any, ...],
) -> None:
    b, config, sidecar = wired
    other = "harness:0000000000000777"
    _destination(b, other)
    _seed_queue(b, 4, dest=other)
    doc = ops_documents.destination_document(_scheduler(b, config), other)
    assert doc["configured_key"] == harness_destination(sidecar).key
    assert doc["summary"]["pending"] == 4
    assert doc["summary"]["instance_id"] == "instance-777"
