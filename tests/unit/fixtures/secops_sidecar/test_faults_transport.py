"""Transport and post-acceptance faults, the outage switch, and token faults."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from tests.fixtures.secops_sidecar.client import (
    build_assertion,
    mint_token,
    post_import,
    request_token,
)
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm

POLL_DEADLINE_SECONDS = 5.0
POLL_STEP_SECONDS = 0.05


def _queue(sidecar: SidecarHandle, path: str, **spec: Any) -> None:
    resp = sidecar.control.post(path, spec)
    assert resp.status_code == 200, resp.text


def _stored(sidecar: SidecarHandle) -> int:
    count: int = sidecar.control.get("/_control/health").json()["received_count"]
    return count


def _body(*ids: str) -> bytes:
    return batch_body([user_login_udm(i) for i in ids])


@pytest.mark.parametrize(
    "kind, check",
    [
        ("non_json", lambda r: r.headers["content-type"] == "text/html"),
        ("truncated_json", lambda r: r.content.startswith(b'{"error"')),
        ("wrong_shape", lambda r: "error" not in r.json()),
        ("empty", lambda r: r.content == b""),
    ],
)
def test_malformed_error_bodies_store_nothing(
    sidecar: SidecarHandle, kind: str, check: Any
) -> None:
    _queue(
        sidecar, "/_control/faults", mode="malformed_response", status=400, kind=kind
    )
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, _body("m-1"))
    assert resp.status_code == 400
    assert check(resp)
    if kind == "truncated_json":
        with pytest.raises(json.JSONDecodeError):
            json.loads(resp.content)
    assert _stored(sidecar) == 0


def test_malformed_success_body_still_stores(sidecar: SidecarHandle) -> None:
    _queue(
        sidecar,
        "/_control/faults",
        mode="malformed_response",
        status=200,
        kind="non_json",
    )
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, _body("m-2"))
    assert resp.status_code == 200 and resp.headers["content-type"] == "text/html"
    assert _stored(sidecar) == 1


def test_success_body_stores_and_answers_the_given_body(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "/_control/faults", mode="success_body", body='{"note":"x"}')
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, _body("s-1"))
    assert resp.status_code == 200
    assert resp.json() == {"note": "x"}
    assert _stored(sidecar) == 1


def test_redirect_is_answered_and_nothing_is_stored(sidecar: SidecarHandle) -> None:
    _queue(
        sidecar,
        "/_control/faults",
        mode="redirect",
        code=307,
        location="http://127.0.0.1:1/x",
    )
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, _body("r-1"))
    assert resp.status_code == 307
    assert resp.headers["location"] == "http://127.0.0.1:1/x"
    assert _stored(sidecar) == 0


def test_rate_limit_sets_retry_after(sidecar: SidecarHandle) -> None:
    _queue(sidecar, "/_control/faults", mode="rate_limit", retry_after_seconds=120)
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    resp = post_import(sidecar.coords, token, _body("q-1"))
    assert resp.status_code == 429
    assert resp.json()["error"]["status"] == "RESOURCE_EXHAUSTED"
    assert resp.headers["retry-after"] == "120"
    assert _stored(sidecar) == 0


def test_delay_with_accept_stores_before_the_client_times_out(
    sidecar: SidecarHandle,
) -> None:
    _queue(sidecar, "/_control/faults", mode="delay", seconds=3, accept=True)
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    with pytest.raises(httpx.ReadTimeout):
        post_import(sidecar.coords, token, _body("d-1"), timeout=0.5)
    assert _stored(sidecar) == 1


def test_delay_without_accept_is_latency_then_normal_processing(
    sidecar: SidecarHandle,
) -> None:
    _queue(sidecar, "/_control/faults", mode="delay", seconds=1, accept=False)
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    started = time.monotonic()
    resp = post_import(sidecar.coords, token, _body("d-2"))
    assert resp.status_code == 200
    assert time.monotonic() - started >= 1.0
    assert _stored(sidecar) == 1


@pytest.mark.parametrize(
    "mode, resend_status", [("ok_noop", 200), ("already_exists", 409)]
)
def test_accept_then_drop_stores_then_resend_is_per_duplicate_mode(
    sidecar: SidecarHandle, mode: str, resend_status: int
) -> None:
    sidecar.control.post("/_control/config", {"duplicate_mode": mode})
    _queue(sidecar, "/_control/faults", mode="accept_then_drop")
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    body = _body("k1-1", "k1-2")
    with pytest.raises(httpx.RemoteProtocolError):
        post_import(sidecar.coords, token, body)
    assert _stored(sidecar) == 2
    resend = post_import(sidecar.coords, token, body)
    assert resend.status_code == resend_status
    assert _stored(sidecar) == 2
    log = sidecar.control.get("/_control/requests").json()["requests"]
    assert log[0]["http_status_returned"] is None
    assert log[0]["fault_applied"] == "accept_then_drop"
    assert log[0]["body_sha256"] == log[1]["body_sha256"]
    assert log[1]["duplicate"] is True


def test_outage_refuses_connections_and_recovers_on_the_same_port(
    sidecar: SidecarHandle,
) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    _queue(sidecar, "/_control/outage", mode="refuse")
    with pytest.raises(httpx.ConnectError):
        post_import(sidecar.coords, token, _body("o-1"))
    assert sidecar.control.get("/_control/health").json()["ingest_listening"] is False
    _queue(sidecar, "/_control/outage", mode="end")
    assert sidecar.control.get("/_control/health").json()["ingest_listening"] is True
    assert post_import(sidecar.coords, token, _body("o-1")).status_code == 200


def test_outage_mode_must_be_known(sidecar: SidecarHandle) -> None:
    assert (
        sidecar.control.post("/_control/outage", {"mode": "sometimes"}).status_code
        == 400
    )


def _token_resp(sidecar: SidecarHandle) -> httpx.Response:
    key = sidecar.read_key_file()
    return request_token(
        sidecar.coords, build_assertion(key, audience=sidecar.coords.token_uri)
    )


def test_token_faults_reject_unavailable_echo_then_recover(
    sidecar: SidecarHandle,
) -> None:
    _queue(sidecar, "/_control/token-faults", mode="reject")
    _queue(sidecar, "/_control/token-faults", mode="unavailable")
    _queue(sidecar, "/_control/token-faults", mode="echo", marker="TOKEN-MARK-1")
    rejected = _token_resp(sidecar)
    assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_grant"
    assert _token_resp(sidecar).status_code == 503
    echoed = _token_resp(sidecar)
    assert echoed.status_code == 400
    assert "TOKEN-MARK-1" in echoed.json()["error_description"]
    assert _token_resp(sidecar).status_code == 200


def test_invalid_token_fault_is_refused(sidecar: SidecarHandle) -> None:
    assert (
        sidecar.control.post("/_control/token-faults", {"mode": "x"}).status_code == 400
    )
    echo_without_marker = {"mode": "echo"}
    assert (
        sidecar.control.post("/_control/token-faults", echo_without_marker).status_code
        == 400
    )


def test_expired_token_is_unauthenticated(sidecar_scratch_dir: Any) -> None:
    from tests.fixtures.secops_sidecar.harness import start_sidecar

    short = start_sidecar(sidecar_scratch_dir, token_ttl_seconds=1)
    try:
        token = mint_token(short.coords, short.read_key_file())
        assert post_import(short.coords, token, _body("t-1")).status_code == 200
        deadline = time.monotonic() + POLL_DEADLINE_SECONDS
        status = 200
        while status != 401 and time.monotonic() < deadline:
            time.sleep(POLL_STEP_SECONDS)
            status = post_import(
                short.coords, token, _body(f"t-{time.monotonic()}")
            ).status_code
        assert status == 401
    finally:
        short.stop()
