"""events:import happy path, inspection, and request-level auth/path checks."""

from __future__ import annotations

import hashlib

from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm


def test_well_formed_batch_is_accepted_and_inspectable(sidecar: SidecarHandle) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    body = batch_body([user_login_udm("u-1"), user_login_udm("u-2")])
    resp = post_import(sidecar.coords, token, body)
    assert resp.status_code == 200
    assert resp.json() == {}

    received = sidecar.control.get("/_control/received").json()["events"]
    assert [e["udm"]["metadata"]["productLogId"] for e in received] == ["u-1", "u-2"]
    assert [e["event_index"] for e in received] == [0, 1]
    assert received[0]["seq"] == received[1]["seq"]
    assert received[0]["batch_sha256"] == received[1]["batch_sha256"]

    seq = received[0]["seq"]
    raw = sidecar.control.get(f"/_control/requests/{seq}/body")
    assert raw.status_code == 200
    assert raw.content == body
    log = sidecar.control.get("/_control/requests").json()["requests"]
    assert log[-1]["seq"] == seq
    assert log[-1]["http_status_returned"] == 200
    assert log[-1]["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert log[-1]["event_count"] == 2
    assert log[-1]["auth_ok"] is True
    assert sidecar.control.get("/_control/health").json()["received_count"] == 2


def test_missing_or_unknown_token_is_unauthenticated(sidecar: SidecarHandle) -> None:
    body = batch_body([user_login_udm("u-1")])
    for token in (None, "not-an-issued-token"):
        resp = post_import(sidecar.coords, token, body)
        assert resp.status_code == 401
        assert resp.json()["error"]["status"] == "UNAUTHENTICATED"
    assert sidecar.control.get("/_control/received").json()["events"] == []
    log = sidecar.control.get("/_control/requests").json()["requests"]
    assert [r["auth_ok"] for r in log] == [False, False]


def test_wrong_parent_or_version_is_not_found(sidecar: SidecarHandle) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    body = batch_body([user_login_udm("u-1")])
    good = sidecar.coords.import_path
    for path in (
        good.replace("/v1/", "/v1alpha/"),
        good.replace("example-project", "other-project"),
    ):
        resp = post_import(sidecar.coords, token, body, path=path)
        assert resp.status_code == 404
        assert resp.json()["error"]["status"] == "NOT_FOUND"
    assert sidecar.control.get("/_control/received").json()["events"] == []


def test_unknown_request_body_seq_is_not_found(sidecar: SidecarHandle) -> None:
    assert sidecar.control.get("/_control/requests/999999/body").status_code == 404
