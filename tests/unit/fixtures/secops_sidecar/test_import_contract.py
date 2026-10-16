"""What Chronicle rejects, the sidecar rejects -- and it stores nothing."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

import pytest

from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, envelope, user_login_udm

MAX_BYTES = 4_000_000


def _three() -> List[Dict[str, Any]]:
    return [user_login_udm(f"u-{i}") for i in range(3)]


def _snake_case_timestamp() -> bytes:
    udms = _three()
    meta = udms[1]["metadata"]
    meta["event_timestamp"] = meta.pop("eventTimestamp")
    return batch_body(udms)


def _unknown_event_type() -> bytes:
    udms = _three()
    udms[2]["metadata"]["eventType"] = "NOT_A_UDM_EVENT_TYPE"
    return batch_body(udms)


def _principal_email() -> bytes:
    udms = _three()
    udms[0]["principal"]["user"] = {"emailAddresses": ["someone@example.com"]}
    return batch_body(udms)


def _principal_without_detail() -> bytes:
    udms = _three()
    udms[1]["principal"] = {}
    return batch_body(udms)


def _bad_timestamp() -> bytes:
    udms = _three()
    udms[2]["metadata"]["eventTimestamp"] = "30/09/2026 12:00"
    return batch_body(udms)


def _events_at_top_level() -> bytes:
    return json.dumps({"events": [{"udm": u} for u in _three()]}).encode()


def _event_wrapper() -> bytes:
    return json.dumps(
        {"inlineSource": {"events": [{"event": u} for u in _three()]}}
    ).encode()


@pytest.mark.parametrize(
    "make_body, expected_field_prefix",
    [
        (_snake_case_timestamp, "inline_source.events[1].udm.metadata.event_timestamp"),
        (_unknown_event_type, "inline_source.events[2].udm.metadata.eventType"),
        (_principal_email, "inline_source.events[0].udm.principal.user.emailAddresses"),
        (_principal_without_detail, "inline_source.events[1].udm.principal"),
        (_bad_timestamp, "inline_source.events[2].udm.metadata.eventTimestamp"),
    ],
)
def test_event_level_defect_rejects_whole_request_naming_the_event(
    sidecar: SidecarHandle, make_body: Any, expected_field_prefix: str
) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, make_body())
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    fields = [v["field"] for d in error["details"] for v in d["fieldViolations"]]
    assert expected_field_prefix in fields
    assert sidecar.control.get("/_control/received").json()["events"] == []


@pytest.mark.parametrize("make_body", [_events_at_top_level, _event_wrapper])
def test_envelope_defect_is_request_level_without_an_event_index(
    sidecar: SidecarHandle, make_body: Any
) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, make_body())
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    fields = [v["field"] for d in error["details"] for v in d["fieldViolations"]]
    assert fields and all("[" not in f for f in fields)
    assert sidecar.control.get("/_control/received").json()["events"] == []


def _padded_body(total_bytes: int) -> bytes:
    udm = user_login_udm("pad-1")
    udm["additional"]["pad"] = ""
    base = len(batch_body([udm]))
    udm = copy.deepcopy(udm)
    udm["additional"]["pad"] = "x" * (total_bytes - base)
    body = batch_body([udm])
    assert len(body) == total_bytes
    return body


def test_request_bound_is_enforced_without_reading_past_it(
    sidecar: SidecarHandle,
) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    oversize = b"x" * (MAX_BYTES + 1)
    resp = post_import(sidecar.coords, token, oversize)
    assert resp.status_code == 400
    fields = [
        v["field"]
        for d in resp.json()["error"]["details"]
        for v in d["fieldViolations"]
    ]
    assert all("[" not in f for f in fields)
    log = sidecar.control.get("/_control/requests").json()["requests"]
    assert log[-1]["body_bytes"] <= MAX_BYTES + 1
    accepted = post_import(sidecar.coords, token, _padded_body(3_999_000))
    assert accepted.status_code == 200
    assert sidecar.control.get("/_control/health").json()["received_count"] == 1


@pytest.mark.parametrize(
    "mode, second_status", [("ok_noop", 200), ("already_exists", 409)]
)
def test_identical_batches_are_deduplicated_per_mode(
    sidecar: SidecarHandle, mode: str, second_status: int
) -> None:
    assert (
        sidecar.control.post("/_control/config", {"duplicate_mode": mode}).status_code
        == 200
    )
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    body = batch_body([user_login_udm("dup-1")])
    assert post_import(sidecar.coords, token, body).status_code == 200
    second = post_import(sidecar.coords, token, body)
    assert second.status_code == second_status
    if second_status == 409:
        assert second.json()["error"]["status"] == "ALREADY_EXISTS"
    assert sidecar.control.get("/_control/health").json()["received_count"] == 1
    log = sidecar.control.get("/_control/requests").json()["requests"]
    assert [r["duplicate"] for r in log] == [False, True]


def test_unknown_duplicate_mode_is_refused(sidecar: SidecarHandle) -> None:
    resp = sidecar.control.post("/_control/config", {"duplicate_mode": "maybe"})
    assert resp.status_code == 400


def test_envelope_helper_shape() -> None:
    assert envelope([{"a": 1}]) == {"inlineSource": {"events": [{"udm": {"a": 1}}]}}
