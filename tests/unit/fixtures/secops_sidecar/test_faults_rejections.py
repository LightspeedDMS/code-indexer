"""Scripted rejections: every rejecting fault answers as specified, stores nothing."""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm


def _queue(sidecar: SidecarHandle, mode: str, **args: Any) -> None:
    resp = sidecar.control.post("/_control/faults", {"mode": mode, **args})
    assert resp.status_code == 200, resp.text


def _post(sidecar: SidecarHandle, n: int, prefix: str = "e") -> httpx.Response:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    body = batch_body([user_login_udm(f"{prefix}-{i}") for i in range(n)])
    return post_import(sidecar.coords, token, body)


def _fields(resp: httpx.Response) -> List[str]:
    error: Dict[str, Any] = resp.json()["error"]
    return [v["field"] for d in error.get("details", []) for v in d["fieldViolations"]]


def _stored(sidecar: SidecarHandle) -> int:
    count: int = sidecar.control.get("/_control/health").json()["received_count"]
    return count


def test_reject_event_names_the_event_and_is_consumed_once(
    sidecar: SidecarHandle,
) -> None:
    _queue(sidecar, "reject_event", event_index=3)
    resp = _post(sidecar, 8)
    assert resp.status_code == 400
    assert _fields(resp) == ["inline_source.events[3].udm.metadata.eventType"]
    assert _stored(sidecar) == 0
    assert _post(sidecar, 8, prefix="next").status_code == 200
    assert _stored(sidecar) == 8
    log = sidecar.control.get("/_control/requests").json()["requests"]
    assert [r["fault_applied"] for r in log] == ["reject_event", None]


def test_reject_event_accepts_any_caller_supplied_path(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "reject_event", event_index=0, path="udm.x1.unknownThing")
    resp = _post(sidecar, 1)
    assert _fields(resp) == ["inline_source.events[0].udm.x1.unknownThing"]


def test_reject_unindexed_carries_no_violation(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "reject_unindexed")
    resp = _post(sidecar, 3)
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert _fields(resp) == []
    assert _stored(sidecar) == 0


def test_reject_request_names_parent(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "reject_request")
    resp = _post(sidecar, 3)
    assert resp.status_code == 400
    assert _fields(resp) == ["parent"]
    assert _stored(sidecar) == 0


def test_reject_if_contains_is_persistent_until_reset(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "reject_if_contains", product_log_id="poison")
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    with_poison = batch_body([user_login_udm("a"), user_login_udm("poison")])
    without = batch_body([user_login_udm("b")])
    for _ in range(3):
        rejected = post_import(sidecar.coords, token, with_poison)
        assert rejected.status_code == 400
        assert _fields(rejected) == []
    assert post_import(sidecar.coords, token, without).status_code == 200
    assert _stored(sidecar) == 1
    sidecar.control.reset()
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    assert post_import(sidecar.coords, token, with_poison).status_code == 200


@pytest.mark.parametrize(
    "code, google_status",
    [
        (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED"), (404, "NOT_FOUND"),
        (409, "ALREADY_EXISTS"), (413, "INVALID_ARGUMENT"), (415, "INVALID_ARGUMENT"),
        (429, "RESOURCE_EXHAUSTED"), (500, "INTERNAL"), (501, "UNIMPLEMENTED"),
        (502, "UNAVAILABLE"), (503, "UNAVAILABLE"), (504, "DEADLINE_EXCEEDED"),
    ],
)  # fmt: skip
def test_status_fault(sidecar: SidecarHandle, code: int, google_status: str) -> None:
    _queue(sidecar, "status", code=code)
    resp = _post(sidecar, 2)
    assert resp.status_code == code
    assert resp.json()["error"]["status"] == google_status
    assert _stored(sidecar) == 0


def test_reject_mixed_names_an_event_and_parent(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "reject_mixed", event_index=1)
    resp = _post(sidecar, 4)
    assert resp.status_code == 400
    fields = _fields(resp)
    assert "parent" in fields
    assert any(f.startswith("inline_source.events[1]") for f in fields)
    assert _stored(sidecar) == 0


def test_reject_out_of_range_names_an_index_the_request_lacks(
    sidecar: SidecarHandle,
) -> None:
    _queue(sidecar, "reject_out_of_range")
    resp = _post(sidecar, 4)
    assert resp.status_code == 400
    assert _fields(resp) == ["inline_source.events[9].udm"]
    assert _stored(sidecar) == 0


def test_echo_marker_puts_the_marker_in_message_and_description(
    sidecar: SidecarHandle,
) -> None:
    _queue(sidecar, "echo_marker", marker="MARKER-7f3a")
    resp = _post(sidecar, 1)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert "MARKER-7f3a" in error["message"]
    assert "MARKER-7f3a" in error["details"][0]["fieldViolations"][0]["description"]
    assert _stored(sidecar) == 0


def test_count_and_fifo_order(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "status", code=503, count=2)
    _queue(sidecar, "reject_request")
    statuses = [_post(sidecar, 1, prefix=f"p{i}").status_code for i in range(4)]
    assert statuses == [503, 503, 400, 200]


def test_fault_queue_is_listed_and_cleared_by_reset(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "status", code=500, count=3)
    listed = sidecar.control.get("/_control/faults").json()
    assert listed["queued"] == [{"mode": "status", "code": 500, "remaining": 3}]
    sidecar.control.reset()
    assert sidecar.control.get("/_control/faults").json() == {
        "queued": [],
        "persistent": [],
    }


@pytest.mark.parametrize(
    "spec",
    [
        {"mode": "no_such_mode"},
        {"mode": "status", "code": 418},
        {"mode": "reject_event"},
        {"mode": "reject_event", "event_index": -1},
        {"mode": "redirect", "code": 307, "location": "http://192.0.2.1/x"},
        {"mode": "malformed_response", "status": 400, "kind": "other"},
        {"mode": "delay", "seconds": -1},
        {"mode": "status", "code": 500, "count": 0},
    ],
)
def test_invalid_fault_spec_is_refused(
    sidecar: SidecarHandle, spec: Dict[str, Any]
) -> None:
    assert sidecar.control.post("/_control/faults", spec).status_code == 400
    assert sidecar.control.get("/_control/faults").json()["queued"] == []
