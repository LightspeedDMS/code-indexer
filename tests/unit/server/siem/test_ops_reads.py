"""The operator read model behind the Web arming and recovery panels
(SQLite AND PostgreSQL): the readiness aggregates agree with the arming
statement over the FULL live set."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from code_indexer.server.services.siem_delivery import state_store as ss

from .backends import SiemBackendHarness
from .test_arming import DEST, FRESH, OTHER, TTL, _confirmed_canary, _cycle

_OLD = datetime(2000, 1, 1, tzinfo=timezone.utc)
_DETAIL_CAP = 200  # readiness_view's default detail_limit


def _process(
    b: SiemBackendHarness,
    pid: str,
    result: str = "ok",
    dest: str = DEST,
    node: str = "solo",
) -> None:
    ss.register_process(b.db, pid, node_id=node, ttl_seconds=TTL)
    ss.record_probe(b.db, pid, destination_key=dest, result=result)


def _make_stale(b: SiemBackendHarness, pid: str) -> None:
    b.raw(
        "UPDATE siem_process_status SET probed_at = ? WHERE process_id = ?",
        (b.db.dialect.ts(_OLD), pid),
    )


def _arms(b: SiemBackendHarness) -> bool:
    return bool(_cycle(b, 1)["armed_destination_key"] == DEST)


def _readiness(b: SiemBackendHarness, **kw: Any) -> Dict[str, Any]:
    return ss.readiness_view(b.db, DEST, FRESH, **kw)


def test_empty_fleet_is_not_ready(siem_backend: SiemBackendHarness) -> None:
    _confirmed_canary(siem_backend)
    view = _readiness(siem_backend)
    assert (view["total_live"], view["failing"], view["all_ready"]) == (0, 0, False)
    assert view["processes"] == [] and view["details_truncated"] is False
    assert _arms(siem_backend) is False


@pytest.mark.parametrize("fault", ["stale", "failed", "other_destination"])
def test_one_unready_process_blocks_both(
    siem_backend: SiemBackendHarness, fault: str
) -> None:
    b = siem_backend
    _confirmed_canary(b)
    _process(b, "solo:1:a")
    if fault == "stale":
        _process(b, "solo:2:b")
        _make_stale(b, "solo:2:b")
    elif fault == "failed":
        _process(b, "solo:2:b", result="token_endpoint_unreachable")
    else:
        _process(b, "solo:2:b", dest=OTHER)
    view = _readiness(b)
    assert (view["total_live"], view["failing"], view["all_ready"]) == (2, 1, False)
    assert [p["process_id"] for p in view["processes"] if not p["ready"]] == [
        "solo:2:b"
    ]
    assert view["processes"][0]["process_id"] == "solo:2:b"  # failing first
    assert _arms(b) is False


def test_all_ready_agrees_with_arming(siem_backend: SiemBackendHarness) -> None:
    b = siem_backend
    _confirmed_canary(b)
    _process(b, "solo:1:a")
    _process(b, "solo:2:b")
    view = _readiness(b)
    assert (view["total_live"], view["failing"], view["all_ready"]) == (2, 0, True)
    assert all(p["ready"] for p in view["processes"])
    assert _arms(b) is True


def test_bad_row_past_the_detail_cap_is_counted_from_the_full_live_set(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    _confirmed_canary(b)
    for i in range(_DETAIL_CAP + 1):
        _process(b, f"solo:{i:03d}:x")
    last = f"solo:{_DETAIL_CAP:03d}:x"  # sorts LAST by process_id
    _process(b, last, result="credential_missing")
    view = _readiness(b)
    assert view["total_live"] == _DETAIL_CAP + 1 and view["failing"] == 1
    assert view["all_ready"] is False and view["details_truncated"] is True
    assert len(view["processes"]) == _DETAIL_CAP
    assert view["processes"][0]["process_id"] == last
    assert _arms(b) is False
    ss.record_probe(b.db, last, destination_key=DEST, result="ok")
    view = _readiness(b)
    assert (view["failing"], view["all_ready"]) == (0, True)
    assert _arms(b) is True


def test_failing_count_never_comes_from_the_capped_details(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    for i in range(5):
        _process(b, f"solo:{i}:x", result="ok" if i % 2 else "pending")
    view = _readiness(b, detail_limit=1)
    assert (view["total_live"], view["failing"]) == (5, 3)
    assert len(view["processes"]) == 1 and view["details_truncated"] is True
    row = view["processes"][0]
    assert row["ready"] is False and row["node_id"] == "solo"
    assert datetime.fromisoformat(row["probed_at"]).tzinfo is not None


def test_pg_node_without_a_process_blocks_both(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    if b.name != "postgres":
        view = _readiness(b)
        assert view["nodes_without_process"] == 0 and view["missing_nodes"] == []
        return
    _confirmed_canary(b)
    _process(b, "node-a:1:x", node="node-a")
    b.raw(
        "INSERT INTO cluster_nodes (node_id, hostname, status, last_heartbeat) "
        "VALUES ('node-a', 'host-a', 'online', now()), "
        "('node-b', 'host-b', 'online', now())"
    )
    try:
        view = _readiness(b)
        assert (view["nodes_without_process"], view["missing_nodes"]) == (
            1,
            ["node-b"],
        )
        assert view["all_ready"] is False and view["failing"] == 0
        assert _arms(b) is False
        _process(b, "node-b:1:x", node="node-b")
        assert _readiness(b)["all_ready"] is True
        assert _arms(b) is True
    finally:
        b.raw("DELETE FROM cluster_nodes WHERE node_id IN ('node-a', 'node-b')")


def test_the_readiness_predicates_are_defined_once() -> None:
    """``_arm`` and ``readiness_view`` share ONE definition of each
    predicate: neither carries its own copy."""
    source = inspect.getsource(ss)
    assert source.count("p.probe_result = 'ok'") == 1
    assert source.count("n.status = 'online'") == 1
    assert "NOT_READY_FOR_DESTINATION" in inspect.getsource(ss._arm)
    assert "NODE_WITHOUT_LIVE_PROCESS" in inspect.getsource(ss._arm)
    assert "NOT_READY_FOR_DESTINATION" in inspect.getsource(ss._readiness_aggregates)
    assert "NODE_WITHOUT_LIVE_PROCESS" in inspect.getsource(ss._readiness_aggregates)
