"""Bug #2018 guards, each tested so that ONLY its own guard can make the test
pass (disable that guard alone and the test fails): the canary run order
within one configuration lifetime, the configuration lifetime itself, and
the forward-only capture snapshot.  SQLite AND PostgreSQL."""

from __future__ import annotations

import dataclasses
from typing import Any, Dict

import pytest

from code_indexer.server.services.siem_delivery import capture
from code_indexer.server.services.siem_delivery import state_store as ss
from code_indexer.server.services.siem_delivery.destination import resolve_destination
from code_indexer.server.services.siem_delivery.scheduler import CycleView
from code_indexer.server.services.siem_delivery.udm import MAPPING_VERSION
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness
from .conftest import harness_section
from .test_admin_parity import _CommittedConfig, _scheduler

DEST = "harness:00000000000000aa"
EPOCH = "example-epoch"
_EXPECTED = {
    "product_log_id": "u1",
    "action_type": "user_created",
    "event_type": "USER_CREATION",
}


def _db_now(b: SiemBackendHarness) -> Any:
    return b.db.read(lambda tx: tx.ts(tx.now()))


def _record(
    b: SiemBackendHarness,
    run_id: str,
    run_seq: int,
    started_at: Any,
    *,
    config_epoch: str = EPOCH,
) -> Any:
    return ss.record_canary(
        b.db,
        run_id=run_id,
        run_seq=run_seq,
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        expected=[_EXPECTED],
        result="accepted",
        signature=None,
        actor="alice",
        config_epoch=config_epoch,
        credential_id=None,  # no credential stored: identical for both runs
        started_at=started_at,
        committed_epoch=lambda: EPOCH,
    )


@pytest.mark.parametrize("clock", ["equal", "earlier"])
def test_older_run_completing_last_never_replaces_a_newer_confirmed_run(
    siem_backend: SiemBackendHarness, clock: str
) -> None:
    """Same lifetime, same credential: only the run ORDER can refuse A
    (issued first, completing last), whatever the two start clocks say."""
    a_seq = ss.issue_canary_run(siem_backend.db)
    a_started = _db_now(siem_backend)
    b_seq = ss.issue_canary_run(siem_backend.db)
    b_started = a_started if clock == "equal" else _db_now(siem_backend)
    assert a_seq < b_seq
    assert _record(siem_backend, "run-b", b_seq, b_started) is None
    outcome = ss.confirm_canary(
        siem_backend.db,
        run_id="run-b",
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        visible_ids=["u1"],
        actor="bob",
        config_epoch=EPOCH,
    )
    assert outcome.confirmed
    before = ss.read_state(siem_backend.db)

    refused = _record(siem_backend, "run-a", a_seq, a_started)  # A completes last

    assert refused == ss.CANARY_SUPERSEDED
    assert ss.read_state(siem_backend.db) == before


def test_canary_of_an_ended_lifetime_is_refused_without_a_later_run(
    siem_backend: SiemBackendHarness,
) -> None:
    """No other run exists: only the lifetime check can refuse it."""
    run_seq = ss.issue_canary_run(siem_backend.db)
    before = ss.read_state(siem_backend.db)
    refused = _record(
        siem_backend,
        "run-old",
        run_seq,
        _db_now(siem_backend),
        config_epoch="example-ended",
    )
    assert refused == ss.CANARY_STALE_LIFETIME
    assert ss.read_state(siem_backend.db) == before


def test_capture_snapshot_publish_is_forward_only(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> None:
    """A slower cycle that read an OLDER committed version (here: another
    destination) never replaces the snapshot of a newer one."""
    newer = harness_section(siem_sidecar, enabled=False)
    older = dataclasses.replace(newer, instance_id="example-older-instance")
    scheduler = _scheduler(siem_backend, _CommittedConfig(dataclasses.asdict(newer)))
    views: Dict[str, CycleView] = {
        name: CycleView(
            version, section, resolve_destination(section, harness_active=True)
        )
        for name, version, section in (("newer", 2, newer), ("older", 1, older))
    }
    capture.reset_capture_state_for_tests()
    try:
        scheduler._apply_arming(views["newer"], capture.monotonic_now())
        published = capture.capture_state()
        scheduler._apply_arming(views["older"], capture.monotonic_now())

        assert capture.capture_state() == published
        assert scheduler.view is views["newer"]
    finally:
        capture.reset_capture_state_for_tests()
