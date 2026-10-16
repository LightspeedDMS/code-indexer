"""Discriminating checks shared by the fidelity tests and their negative controls.

Each check returns True when the sidecar behaves correctly.  The fidelity
tests assert True on a normal sidecar; the negative-control tests assert False
on a sidecar started with a deliberate self-test defect.  Setup calls are
asserted and each check carries a positive control, so a check can never
pass (or fail) vacuously because its setup broke.
"""

from __future__ import annotations

from typing import Any, Dict, List

from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm

# Every ingest fault mode that must store nothing, with its arguments.
REJECTING_FAULTS: List[Dict[str, Any]] = [
    {"mode": "reject_event", "event_index": 0},
    {"mode": "reject_unindexed"},
    {"mode": "reject_request"},
    {"mode": "reject_mixed", "event_index": 0},
    {"mode": "reject_out_of_range"},
    {"mode": "echo_marker", "marker": "M"},
    {"mode": "redirect", "code": 307, "location": "http://127.0.0.1:1/x"},
    {"mode": "rate_limit", "retry_after_seconds": 1},
    *[
        {"mode": "status", "code": c}
        for c in (401, 403, 404, 409, 413, 415, 500, 501, 503)
    ],
    *[
        {"mode": "malformed_response", "status": 400, "kind": k}
        for k in ("non_json", "truncated_json", "wrong_shape", "empty")
    ],
]


def _fresh_session(handle: SidecarHandle) -> str:
    handle.control.reset()
    return mint_token(handle.coords, handle.read_key_file())


def _control_ok(handle: SidecarHandle, path: str, payload: Dict[str, Any]) -> None:
    resp = handle.control.post(path, payload)
    assert resp.status_code == 200, (
        f"setup {path} failed: {resp.status_code} {resp.text}"
    )


def _accepted(handle: SidecarHandle, token: str, product_log_id: str) -> None:
    resp = post_import(
        handle.coords, token, batch_body([user_login_udm(product_log_id)])
    )
    assert resp.status_code == 200, f"positive control rejected: {resp.status_code}"


def _received(handle: SidecarHandle) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = handle.control.get("/_control/received").json()[
        "events"
    ]
    return events


def _search(handle: SidecarHandle, product_log_id: str) -> List[Dict[str, Any]]:
    resp = handle.control.get(
        "/_control/search", params={"product_log_id": product_log_id}
    )
    assert resp.status_code == 200, resp.text
    results: List[Dict[str, Any]] = resp.json()["results"]
    return results


def _invalid_body(product_log_id: str) -> bytes:
    bad = user_login_udm(product_log_id)
    bad["metadata"]["eventType"] = "NOT_A_TYPE"
    return batch_body([user_login_udm(product_log_id + "-ok"), bad])


def rejected_batches_store_nothing(handle: SidecarHandle) -> bool:
    token = _fresh_session(handle)
    for index, fault in enumerate(REJECTING_FAULTS):
        _control_ok(handle, "/_control/faults", fault)
        post_import(handle.coords, token, batch_body([user_login_udm(f"rj-{index}")]))
    post_import(handle.coords, token, _invalid_body("rj-invalid"))
    _control_ok(
        handle,
        "/_control/faults",
        {"mode": "reject_if_contains", "product_log_id": "rj-poison"},
    )
    post_import(handle.coords, token, batch_body([user_login_udm("rj-poison")]))
    _accepted(handle, token, "rj-control")
    stored = [e["udm"]["metadata"]["productLogId"] for e in _received(handle)]
    return stored == ["rj-control"]


def identical_batch_stored_once(handle: SidecarHandle) -> bool:
    token = _fresh_session(handle)
    body = batch_body([user_login_udm("same-1")])
    assert post_import(handle.coords, token, body).status_code == 200
    post_import(handle.coords, token, body)
    return len(_received(handle)) == 1


def hidden_event_not_searchable(handle: SidecarHandle) -> bool:
    token = _fresh_session(handle)
    _control_ok(handle, "/_control/visibility", {"hide_product_log_id": "hid-1"})
    _accepted(handle, token, "hid-1")
    _accepted(handle, token, "hid-control")
    assert _search(handle, "hid-control") != []
    return len(_received(handle)) == 2 and _search(handle, "hid-1") == []


def rejected_event_not_searchable(handle: SidecarHandle) -> bool:
    token = _fresh_session(handle)
    post_import(handle.coords, token, _invalid_body("rej-1"))
    _accepted(handle, token, "rej-control")
    assert _search(handle, "rej-control") != []
    return _search(handle, "rej-1-ok") == [] and _search(handle, "rej-1") == []
