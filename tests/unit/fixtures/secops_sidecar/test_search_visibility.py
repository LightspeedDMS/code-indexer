"""Search stand-in and accepted-but-not-visible control (the canary gate)."""

from __future__ import annotations

from typing import Any, Dict, List

from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm


def _accept(sidecar: SidecarHandle, *ids: str, event_type: str = "USER_LOGIN") -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    body = batch_body([user_login_udm(i, event_type=event_type) for i in ids])
    assert post_import(sidecar.coords, token, body).status_code == 200


def _search(sidecar: SidecarHandle, **params: str) -> List[Dict[str, Any]]:
    resp = sidecar.control.get("/_control/search", params=params)
    assert resp.status_code == 200, resp.text
    results: List[Dict[str, Any]] = resp.json()["results"]
    return results


def _ids(results: List[Dict[str, Any]]) -> List[str]:
    return [r["udm"]["metadata"]["productLogId"] for r in results]


def test_search_by_product_log_id_returns_exactly_that_event(
    sidecar: SidecarHandle,
) -> None:
    _accept(sidecar, "U1", "U2")
    found = _search(sidecar, product_log_id="U1")
    assert _ids(found) == ["U1"]
    stored = sidecar.control.get("/_control/received").json()["events"][0]
    for key in ("seq", "batch_sha256", "received_at", "event_index"):
        assert found[0][key] == stored[key]
    assert _ids(_search(sidecar, event_type="USER_LOGIN")) == ["U1", "U2"]
    assert _ids(_search(sidecar, product_event_type="authentication_success")) == [
        "U1",
        "U2",
    ]
    assert _search(sidecar, product_log_id="never-sent") == []


def test_search_requires_a_filter(sidecar: SidecarHandle) -> None:
    assert sidecar.control.get("/_control/search").status_code == 400


def test_hidden_event_is_stored_but_not_searchable(sidecar: SidecarHandle) -> None:
    resp = sidecar.control.post("/_control/visibility", {"hide_product_log_id": "U2"})
    assert resp.status_code == 200
    _accept(sidecar, "U1", "U2")
    received = sidecar.control.get("/_control/received").json()["events"]
    assert _ids(received) == ["U1", "U2"]
    assert _search(sidecar, product_log_id="U2") == []
    assert _ids(_search(sidecar, product_log_id="U1")) == ["U1"]


def test_hide_rule_applies_to_already_stored_events(sidecar: SidecarHandle) -> None:
    _accept(sidecar, "U1")
    sidecar.control.post("/_control/visibility", {"hide_product_log_id": "U1"})
    assert _search(sidecar, product_log_id="U1") == []
    assert sidecar.control.get("/_control/health").json()["received_count"] == 1


def test_hide_by_event_type(sidecar: SidecarHandle) -> None:
    _accept(sidecar, "U1", event_type="USER_LOGIN")
    _accept(sidecar, "U2", event_type="USER_CHANGE_PERMISSIONS")
    sidecar.control.post("/_control/visibility", {"hide_event_type": "USER_LOGIN"})
    assert _search(sidecar, product_log_id="U1") == []
    assert _ids(_search(sidecar, product_log_id="U2")) == ["U2"]


def test_visibility_rule_must_name_exactly_one_target(sidecar: SidecarHandle) -> None:
    assert sidecar.control.post("/_control/visibility", {}).status_code == 400
    both = {"hide_product_log_id": "a", "hide_event_type": "USER_LOGIN"}
    assert sidecar.control.post("/_control/visibility", both).status_code == 400


def test_rejected_event_is_never_searchable(sidecar: SidecarHandle) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    bad = user_login_udm("U3")
    bad["metadata"]["eventType"] = "NOT_A_TYPE"
    resp = post_import(sidecar.coords, token, batch_body([user_login_udm("U4"), bad]))
    assert resp.status_code == 400
    assert _search(sidecar, product_log_id="U3") == []
    assert _search(sidecar, product_log_id="U4") == []


def test_reset_clears_hide_rules(sidecar: SidecarHandle) -> None:
    sidecar.control.post("/_control/visibility", {"hide_product_log_id": "U1"})
    sidecar.control.reset()
    _accept(sidecar, "U1")
    assert _ids(_search(sidecar, product_log_id="U1")) == ["U1"]
