"""Version fence and the atomic arming compare-and-set (SQLite and PostgreSQL)."""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.services.siem_delivery import state_store as ss
from code_indexer.server.services.siem_delivery.udm import MAPPING_VERSION

from .backends import SiemBackendHarness

DEST = "harness:00000000000000aa"
OTHER = "harness:00000000000000bb"
TTL = 180.0
FRESH = 600.0
EPOCH = "example-epoch"  # the committed configuration lifetime (Bug #2018)
_EXPECTED_U1 = {
    "product_log_id": "u1",
    "action_type": "user_created",
    "event_type": "USER_CREATION",
}
_EXPECTED_U2 = {
    "product_log_id": "u2",
    "action_type": "user_role_changed",
    "event_type": "USER_CHANGE_PERMISSIONS",
}


def _db_now(b: SiemBackendHarness) -> Any:
    """The SIEM database clock, as record_canary's started_at."""
    return b.db.read(lambda tx: tx.ts(tx.now()))


def _confirmed_canary(b: SiemBackendHarness, dest: str = DEST) -> None:
    ss.record_canary(
        b.db,
        run_id="run-1",
        run_seq=ss.issue_canary_run(b.db),
        destination_key=dest,
        mapping_version=MAPPING_VERSION,
        expected=[_EXPECTED_U1],
        result="accepted",
        signature=None,
        actor="alice",
        config_epoch=EPOCH,
        credential_id=None,  # no credential stored in these tests
        started_at=_db_now(b),
        committed_epoch=lambda: EPOCH,
    )
    outcome = ss.confirm_canary(
        b.db,
        run_id="run-1",
        destination_key=dest,
        mapping_version=MAPPING_VERSION,
        visible_ids=["u1"],
        actor="alice",
        config_epoch=EPOCH,
    )
    assert outcome.confirmed


def _ready_process(
    b: SiemBackendHarness, pid: str, result: str = "ok", dest: str = DEST
) -> None:
    ss.register_process(b.db, pid, node_id="solo", ttl_seconds=TTL)
    ss.record_probe(b.db, pid, destination_key=dest, result=result)


def _cycle(
    b: SiemBackendHarness, version: int, enabled: bool = True, dest: Any = DEST
) -> Dict[str, Any]:
    return ss.fence_and_arm(
        b.db,
        version=version,
        enabled=enabled,
        destination_key=dest,
        mapping_version=MAPPING_VERSION,
        config_epoch=EPOCH,
        probe_fresh_seconds=FRESH,
    )


def test_empty_fleet_never_arms(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    state = _cycle(siem_backend, 1)
    assert state["armed_destination_key"] is None


def test_all_ready_arms_once(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    _ready_process(siem_backend, "solo:2:b")
    first = _cycle(siem_backend, 1)
    assert first["armed_destination_key"] == DEST
    assert first["armed_config_version"] == 1
    second = _cycle(siem_backend, 1)
    assert second["armed_at"] == first["armed_at"]


def test_one_live_process_not_ok_blocks_arming(
    siem_backend: SiemBackendHarness,
) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    _ready_process(siem_backend, "solo:2:b", result="credential_missing")
    assert _cycle(siem_backend, 1)["armed_destination_key"] is None
    ss.register_process(siem_backend.db, "solo:3:c", node_id="solo", ttl_seconds=TTL)
    ss.record_probe(siem_backend.db, "solo:2:b", destination_key=DEST, result="ok")
    # solo:3:c is registered but still pending: blocks arming
    assert _cycle(siem_backend, 1)["armed_destination_key"] is None
    ss.record_probe(siem_backend.db, "solo:3:c", destination_key=DEST, result="ok")
    assert _cycle(siem_backend, 1)["armed_destination_key"] == DEST


def test_probe_for_another_destination_blocks(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a", dest=OTHER)
    assert _cycle(siem_backend, 1)["armed_destination_key"] is None


def test_expired_process_no_longer_blocks(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    ss.register_process(
        siem_backend.db, "solo:dead:x", node_id="solo", ttl_seconds=-1.0
    )
    assert _cycle(siem_backend, 1)["armed_destination_key"] == DEST


def test_canary_for_another_destination_never_arms(
    siem_backend: SiemBackendHarness,
) -> None:
    _confirmed_canary(siem_backend, dest=OTHER)
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 1)["armed_destination_key"] is None


def test_newer_disable_disarms_and_older_version_cannot_undo_it(
    siem_backend: SiemBackendHarness,
) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 6)["armed_destination_key"] == DEST
    disabled = _cycle(siem_backend, 7, enabled=False)
    assert disabled["armed_destination_key"] is None
    assert disabled["seen_config_version"] == 7
    stale = _cycle(siem_backend, 6, enabled=True)
    assert stale["armed_destination_key"] is None
    assert stale["seen_config_version"] == 7
    assert not ss.capture_active(
        stale, version=6, enabled=True, destination_key=DEST, config_epoch=EPOCH
    )


def test_armed_state_of_another_lifetime_is_not_active(
    siem_backend: SiemBackendHarness,
) -> None:
    """Fail closed before any fence: the committed lifetime decides."""
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    armed = _cycle(siem_backend, 1)
    assert armed["armed_destination_key"] == DEST
    kwargs: Dict[str, Any] = {"version": 2, "enabled": True, "destination_key": DEST}
    assert ss.capture_active(armed, config_epoch=EPOCH, **kwargs)
    assert not ss.capture_active(armed, config_epoch="example-new-lifetime", **kwargs)


def test_destination_change_disarms(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 1)["armed_destination_key"] == DEST
    assert _cycle(siem_backend, 2, dest=OTHER)["armed_destination_key"] is None


def test_newer_version_of_another_lifetime_disarms(
    siem_backend: SiemBackendHarness,
) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 1)["armed_destination_key"] == DEST
    later = ss.fence_and_arm(
        siem_backend.db,
        version=2,
        enabled=True,
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        config_epoch="example-later-epoch",
        probe_fresh_seconds=FRESH,
    )
    assert later["armed_destination_key"] is None  # and cannot re-arm


def test_canary_of_a_new_lifetime_ends_the_old_arming(
    siem_backend: SiemBackendHarness,
) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 1)["armed_destination_key"] == DEST
    refused = ss.record_canary(
        siem_backend.db,
        run_id="run-new",
        run_seq=ss.issue_canary_run(siem_backend.db),
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        expected=[_EXPECTED_U1],
        result="accepted",
        signature=None,
        actor="alice",
        config_epoch="example-later-epoch",
        credential_id=None,
        started_at=_db_now(siem_backend),
        committed_epoch=lambda: "example-later-epoch",
    )
    assert refused is None  # recorded
    assert ss.read_state(siem_backend.db)["armed_destination_key"] is None


def test_partial_visibility_never_confirms(siem_backend: SiemBackendHarness) -> None:
    ss.record_canary(
        siem_backend.db,
        run_id="run-2",
        run_seq=ss.issue_canary_run(siem_backend.db),
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        expected=[_EXPECTED_U1, _EXPECTED_U2],
        result="accepted",
        signature=None,
        actor="alice",
        config_epoch=EPOCH,
        credential_id=None,
        started_at=_db_now(siem_backend),
        committed_epoch=lambda: EPOCH,
    )
    outcome = ss.confirm_canary(
        siem_backend.db,
        run_id="run-2",
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        visible_ids=["u1"],
        actor="alice",
        config_epoch=EPOCH,
    )
    assert not outcome.confirmed
    assert outcome.missing_action_types == ["user_role_changed"]
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 1)["armed_destination_key"] is None
    stale = ss.confirm_canary(
        siem_backend.db,
        run_id="other",
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        visible_ids=["u1", "u2"],
        actor="alice",
        config_epoch=EPOCH,
    )
    assert stale.stale
