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


def _confirmed_canary(b: SiemBackendHarness, dest: str = DEST) -> None:
    ss.record_canary(
        b.db,
        run_id="run-1",
        destination_key=dest,
        mapping_version=MAPPING_VERSION,
        expected=[_EXPECTED_U1],
        result="accepted",
        signature=None,
        actor="alice",
    )
    outcome = ss.confirm_canary(
        b.db,
        run_id="run-1",
        destination_key=dest,
        mapping_version=MAPPING_VERSION,
        visible_ids=["u1"],
        actor="alice",
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
    _ready_process(siem_backend, "solo:2:b", result="key_file_unreadable")
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
    assert not ss.capture_active(stale, version=6, enabled=True, destination_key=DEST)


def test_destination_change_disarms(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    _ready_process(siem_backend, "solo:1:a")
    assert _cycle(siem_backend, 1)["armed_destination_key"] == DEST
    assert _cycle(siem_backend, 2, dest=OTHER)["armed_destination_key"] is None


def test_partial_visibility_never_confirms(siem_backend: SiemBackendHarness) -> None:
    ss.record_canary(
        siem_backend.db,
        run_id="run-2",
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        expected=[_EXPECTED_U1, _EXPECTED_U2],
        result="accepted",
        signature=None,
        actor="alice",
    )
    outcome = ss.confirm_canary(
        siem_backend.db,
        run_id="run-2",
        destination_key=DEST,
        mapping_version=MAPPING_VERSION,
        visible_ids=["u1"],
        actor="alice",
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
    )
    assert stale.stale
